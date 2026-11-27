"""Independent, short-lived Chat reviewers for workspace task results."""

import asyncio
import json
import re
import uuid

from claude_hub.models.subagent import SubagentCreate, SubagentTurnStatus
from claude_hub.services import subagents as subagents_mod
from claude_hub.services.cross_reviewer import select_cross_reviewer_candidates
from claude_hub.services.task_transitions import apply_task_update

from ._constants import *  # noqa: F401,F403
from ._task_execution import TaskExecutionConflict
from .prompts.review import BLIND_REVIEW_RULES


class _SubagentTaskReviewMixin:
    def _subagent_review_is_active(self: Any, task: WorkspaceTask) -> bool:
        review = getattr(self, "_subagent_review_tasks", {}).get(task.id)
        return bool(task.review_session_id is None and review and not review.done())

    def _get_live_subagent_service(self: Any) -> Any:
        service = subagents_mod.get_subagent_service()
        if getattr(self, "_subagent_task_review_enabled", False) or getattr(
            service, "_started", False
        ):
            return service
        return None

    def _should_use_subagent_task_review(
        self: Any, workspace: Workspace, task: WorkspaceTask, trigger_report: AgentReport
    ) -> bool:
        reviewer = self.sessions.get(task.review_session_id) if task.review_session_id else None
        return bool(
            workspace.target == ExecutionTarget.LOCAL
            and not self._is_goal_packet_approval_review(task, trigger_report)
            and not (
                reviewer
                and reviewer.role == WorkspaceSessionRole.REVIEWER
                and not reviewer.ephemeral
                and not reviewer.archived
                and reviewer.status != ManagedSessionStatus.STOPPED
            )
            and self._get_live_subagent_service()
        )

    def _start_subagent_task_review(
        self: Any, workspace: Workspace, task: WorkspaceTask, trigger_report: AgentReport
    ) -> None:
        if not self._liveness_review_claim_valid(task):
            return
        now = self._runtime_now()
        lessons = self._lesson_context_payload(
            workspace, f"{task.title}\n{task.prompt}\n{trigger_report.message}"
        )
        lesson_ids = self._record_inlined_lesson_hits(task, lessons, now=now)
        run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            run = run or self._default_autonomous_run(
                task.id, task.autonomy_policy.max_iterations if task.autonomy_policy else 3
            )
            phase = AutonomousRunPhase.EVALUATING
            run = run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": None,
                "review_attempts": task.review_attempts + 1,
                "review_requested_at": now,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "completed_at": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "autonomous_run": run,
                "feedback_lesson_ids": list(
                    dict.fromkeys([*task.feedback_lesson_ids, *lesson_ids])
                ),
                "updated_at": now,
            },
            cause="review_request",
        )
        self._save_state()
        tasks: dict[str, asyncio.Task[None]] | None = getattr(self, "_subagent_review_tasks", None)
        if tasks is None:
            tasks = {}
            self._subagent_review_tasks = tasks
        review = asyncio.create_task(
            self._execute_subagent_task_review(
                workspace, self.tasks[task.id], trigger_report, lessons
            )
        )
        tasks[task.id] = review
        review.add_done_callback(
            lambda done: tasks.pop(task.id, None) if tasks.get(task.id) is done else None
        )

    async def _execute_subagent_task_review(
        self: Any,
        workspace: Workspace,
        task: WorkspaceTask,
        trigger_report: AgentReport,
        lessons: list[dict[str, Any]],
    ) -> None:
        worker = self.sessions.get(task.session_id) if task.session_id else None
        cwd = task.worktree_path or (worker.workspace_path if worker else None) or workspace.path
        marker = "subagent-reviewer:error"
        state = AgentReportState.REVIEW_NEEDS_INPUT
        message = "Blind review failed to run; human intervention is required."
        validation = risks = None
        try:

            async def git(*args: str) -> str:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    cwd,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), 10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return "Git command timed out"
                return out.decode(errors="replace")[:30000]

            status = await git("status", "--short")
            diff = await git("diff", "HEAD", "--stat")
            guidance = self._review_guidance_block(workspace, trigger_report)
            failures = [
                r
                for r in self.reports.values()
                if r.task_id == task.id
                and r.state == AgentReportState.REVIEW_FAILED
                and r.review_cycle < task.review_cycle
            ]
            previous = max(failures, key=lambda r: r.created_at).message if failures else ""
            prompt = (
                f"Independently review the implementation in {cwd}. Inspect the actual diff and code.\n"
                f"Task: {task.title}\n{task.prompt}\nGoal packet: {task.goal_packet}\n"
                f"{guidance}\nWorkspace lessons: {lessons}\nGit status:\n{status}\nGit diff summary:\n{diff}\n"
                f"Changed files: {trigger_report.changed_files}\nValidation: {trigger_report.validation}\n"
                f"Previous blocking review: {previous[:8000]}\nWorker response: {trigger_report.message if previous else ''}\n"
                f'{BLIND_REVIEW_RULES}\nEnd with <task-review>{{"verdict":"review_passed|review_failed|review_needs_input","summary":"...","validation":"...","risks":"...","findings":[{{"severity":"P0|P1|P2","file":"path:line","summary":"...","fix":"..."}}]}}</task-review>'
            )
            candidates = select_cross_reviewer_candidates(
                worker.agent_type if worker else task.agent_type, worker.env if worker else {}
            )
            errors = []
            service = self._get_live_subagent_service()
            for index, candidate in enumerate(candidates):
                record_id = None
                try:
                    record, turn = await service.create(
                        SubagentCreate(
                            agent_type=AgentType(candidate.provider),
                            model=candidate.model,
                            effort=candidate.effort,
                            env=dict(candidate.env or {}),
                            parent_tab_id=None,
                            caller_tab_id=None,
                            caller_subagent_id=None,
                            cwd=cwd,
                            prompt=prompt,
                            keep=False,
                            solo_mode=True,
                            call_id=f"task-review:{task.id}:{task.review_cycle}:{task.review_attempts}:{index}",
                        )
                    )
                    record_id = record.id
                    while turn.status == SubagentTurnStatus.RUNNING:
                        record, turn = await service.wait_turn(
                            record.id, turn.turn_id, wait_seconds=30.0
                        )
                    if turn.status != SubagentTurnStatus.COMPLETED or not turn.final_text:
                        raise ValueError(str(turn.error or turn.status))
                    match = re.search(
                        r"<task-review>\s*(.*?)\s*</task-review>", turn.final_text, re.DOTALL
                    )
                    if not match:
                        raise ValueError("missing task-review JSON")
                    data = json.loads(match.group(1))
                    findings = data.get("findings", [])
                    if not isinstance(findings, list) or any(
                        not isinstance(f, dict) for f in findings
                    ):
                        raise ValueError("invalid findings")
                    state = AgentReportState(data["verdict"])
                    if state not in {
                        AgentReportState.REVIEW_PASSED,
                        AgentReportState.REVIEW_FAILED,
                        AgentReportState.REVIEW_NEEDS_INPUT,
                    }:
                        raise ValueError("invalid verdict")
                    if state == AgentReportState.REVIEW_FAILED and not any(
                        str(f.get("severity", "")).upper() in {"P0", "P1"} for f in findings
                    ):
                        state = AgentReportState.REVIEW_PASSED
                    message = (
                        str(data.get("summary", ""))
                        + "\n"
                        + "\n".join(
                            f"{f.get('severity', '')} {f.get('file', '')}: {f.get('summary', '')} Fix: {f.get('fix', '')}"
                            for f in findings
                        )
                    )
                    validation = str(data.get("validation") or "")
                    risks = str(data.get("risks") or "")
                    marker = f"subagent-reviewer:{record.id}"
                    break
                except asyncio.CancelledError:
                    if record_id:
                        await asyncio.shield(
                            service.close(record_id, reason="task review cancelled")
                        )
                    raise
                except Exception as exc:
                    errors.append(f"{candidate.provider}: {exc}")
            else:
                message += " " + ("; ".join(errors) or "No reviewer candidate available.")
        except Exception as exc:
            logger.exception("Subagent task review failed task_id=%s", task.id)
            message += f" {exc}"
        now = self._runtime_now()
        try:
            async with self._workspace_execution_operation(task):
                async with self.workspace_mutation_lock(task.workspace_id):
                    live = self.tasks.get(task.id)
                    if (
                        not live
                        or live.status != WorkspaceTaskStatus.REVIEW
                        or live.review_cycle != task.review_cycle
                        or live.review_attempts != task.review_attempts
                        or live.review_completed_at is not None
                    ):
                        return
                    reviewer = ManagedSession(
                        id=marker,
                        workspace_id=workspace.id,
                        tab_id=marker,
                        role=WorkspaceSessionRole.REVIEWER,
                        agent_type=worker.agent_type if worker else task.agent_type,
                        status=ManagedSessionStatus.IDLE,
                        title="Subagent reviewer",
                        workspace_path=cwd,
                        tmux_session="",
                        created_at=now,
                        updated_at=now,
                    )
                    report = AgentReport(
                        id=str(uuid.uuid4()),
                        workspace_id=workspace.id,
                        task_id=task.id,
                        session_id=marker,
                        state=state,
                        message=message,
                        validation=validation,
                        risks=risks,
                        review_cycle=live.review_cycle,
                        created_at=now,
                    )
                    update = state_policy.compute_reviewer_verdict_task_update(
                        report_state=state,
                        reviewer_session_id=marker,
                        now=now,
                        report_review_cycle=report.review_cycle,
                        existing_review_completed_at=live.review_completed_at,
                        existing_reviewed_at=live.reviewed_at,
                        existing_human_acceptance_requested_at=live.human_acceptance_requested_at,
                        preserve_existing_review_completed_at=True,
                        preserve_existing_reviewed_at=False,
                        preserve_existing_human_acceptance_requested_at=True,
                        human_acceptance_for_passed=True,
                    )
                    phase = None
                    if live.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                        run, phase = self._autonomous_run_after_evaluation(
                            live, reviewer, report, now=now
                        )
                        update["autonomous_run"] = run
                        update["human_acceptance_requested_at"] = (
                            now if phase == AutonomousRunPhase.PASSED else None
                        )
                    self.reports[report.id] = report
                    self.tasks[live.id] = apply_task_update(live, update, cause="review_verdict")
                    self._save_workspace_state(workspace.id)
                await self._handle_review_report(
                    self.tasks[live.id], reviewer, report, autonomous_next_phase_override=phase
                )
        except (KeyError, TaskExecutionConflict):
            # Ownership or the task itself changed while the reviewer was running.
            return
