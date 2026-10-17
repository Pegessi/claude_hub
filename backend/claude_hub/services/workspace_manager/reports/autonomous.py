"""Report processing split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from .acknowledgements import _ReportAcknowledgementsMixin


class _AutonomousReportMixin(_ReportAcknowledgementsMixin):
    def _autonomous_run_after_worker_report(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
        *,
        now: datetime,
    ) -> AutonomousRun | None:
        phase = state_policy.autonomous_phase_after_worker_report(report.state)
        if phase is None:
            return None
        run = task.autonomous_run or self._default_autonomous_run(
            task.id,
            task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
        )
        # ------------------------------------------------------------------
        # Post-PASS idempotency guard.
        #
        # Once the autonomous run has reached PASSED for the current verdict
        # round, further worker completed/working/ready_for_review reports are
        # stale echoes — the worker re-emitting after human acceptance was
        # already requested. Re-running evaluation here would flip the phase
        # off PASSED (PASSED → EVALUATING/WORKING); once a later review_cycle
        # bump makes ``current_round_has_verdict`` lapse, those stale reports
        # go on to reopen review and clear ``human_acceptance_requested_at``,
        # stranding the task in REVIEW with no usable Done button. Ignore them
        # so the run stays PASSED and acceptance-able.
        #
        # A genuine reopen (continue_task after a FAIL/REVISING verdict) sets
        # phase = WORKING and bumps review_cycle *before* the worker reports
        # again, so report_opens_review_round is True there and legitimate
        # revision rounds proceed untouched.
        # ------------------------------------------------------------------
        if run.phase == AutonomousRunPhase.PASSED and not state_policy.report_opens_review_round(
            report.review_cycle, task.reviewed_cycle
        ):
            logger.info(
                "Ignoring stale post-PASS worker report in _autonomous_run_after_worker_report "
                "workspace_id=%s task_id=%s session_id=%s report_state=%s "
                "report_cycle=%s review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                session.id,
                report.state.value,
                report.review_cycle,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return None
        active_session_ids = list(dict.fromkeys([*run.active_session_ids, session.id]))
        iterations = list(run.iterations)
        if report.state in {AgentReportState.READY_FOR_REVIEW, AgentReportState.COMPLETED}:
            iterations.append(
                AutonomousIteration(
                    iteration=run.iteration,
                    worker_session_id=session.id,
                    worker_report_id=report.id,
                    controller_decision="worker_ready_for_evaluation",
                    started_at=run.iterations[-1].started_at if run.iterations else task.started_at,
                    completed_at=now,
                )
            )
        return run.model_copy(
            update={
                "phase": phase,
                "status_summary": self._autonomous_phase_label(phase),
                "active_session_ids": active_session_ids,
                "next_action": self._autonomous_next_action(phase),
                "iterations": iterations,
            }
        )

    def _autonomous_phase_label(self, phase: AutonomousRunPhase) -> str:
        return phase.value.replace("_", " ").title()

    def _autonomous_next_action(self, phase: AutonomousRunPhase) -> str:
        return {
            AutonomousRunPhase.INTAKE: "Derive Goal Packet and begin work",
            AutonomousRunPhase.DISPATCHING: "Select or queue a workspace agent",
            AutonomousRunPhase.WORKING: "Worker is executing the current iteration",
            AutonomousRunPhase.EVALUATING: "Evaluator is reviewing the latest worker output",
            AutonomousRunPhase.REVISING: "Send targeted revision feedback to the worker",
            AutonomousRunPhase.WAITING_FOR_HUMAN: "Waiting for human input or product judgment",
            AutonomousRunPhase.PASSED: "Autonomous evaluation passed; awaiting human acceptance",
            AutonomousRunPhase.EXHAUSTED: "Iteration budget exhausted; awaiting human review",
            AutonomousRunPhase.FAILED: "Autonomous run failed; awaiting human review",
            AutonomousRunPhase.CANCELLED: "Autonomous run cancelled",
        }.get(phase, "Continue autonomous run")

    def _autonomous_run_after_evaluation(
        self,
        task: WorkspaceTask,
        evaluator: ManagedSession,
        report: AgentReport,
        *,
        now: datetime,
    ) -> tuple[AutonomousRun | None, AutonomousRunPhase | None]:
        decision = state_policy.autonomous_decision_from_review_state(report.state)
        if decision is None:
            return task.autonomous_run, None
        run = task.autonomous_run or self._default_autonomous_run(
            task.id,
            task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
        )
        evaluation_report = self._evaluation_report_from_review(
            task=task,
            run=run,
            evaluator=evaluator,
            report=report,
            decision=decision,
            now=now,
        )
        next_phase = state_policy.autonomous_phase_from_evaluation_decision(
            decision=decision,
            current_iteration=run.iteration,
            max_iterations=run.max_iterations,
        )
        next_iteration = run.iteration
        if next_phase == AutonomousRunPhase.REVISING:
            next_iteration += 1
        iterations = list(run.iterations)
        iterations.append(
            AutonomousIteration(
                iteration=run.iteration,
                worker_session_id=task.session_id,
                evaluator_session_id=evaluator.id,
                evaluation_report_id=evaluation_report.id,
                controller_decision=next_phase.value,
                completed_at=now,
            )
        )
        return (
            run.model_copy(
                update={
                    "phase": next_phase,
                    "iteration": next_iteration,
                    "status_summary": self._autonomous_phase_label(next_phase),
                    "active_session_ids": list(
                        dict.fromkeys([*run.active_session_ids, evaluator.id])
                    ),
                    "current_score": evaluation_report.overall_score,
                    "next_action": self._autonomous_next_action(next_phase),
                    "exhausted_at": now if next_phase == AutonomousRunPhase.EXHAUSTED else None,
                    "completed_at": now if next_phase == AutonomousRunPhase.PASSED else None,
                    "evaluation_reports": [*run.evaluation_reports, evaluation_report],
                    "iterations": iterations,
                }
            ),
            next_phase,
        )

    def _evaluation_report_from_review(
        self,
        *,
        task: WorkspaceTask,
        run: AutonomousRun,
        evaluator: ManagedSession,
        report: AgentReport,
        decision: EvaluationDecision,
        now: datetime,
    ) -> EvaluationReport:
        if report.evaluation_report is not None:
            return report.evaluation_report.model_copy(
                update={
                    "run_id": report.evaluation_report.run_id or run.id,
                    "task_id": report.evaluation_report.task_id or task.id,
                    "iteration": report.evaluation_report.iteration or run.iteration,
                    "evaluator_session_id": (
                        report.evaluation_report.evaluator_session_id or evaluator.id
                    ),
                    "created_at": report.evaluation_report.created_at or now,
                }
            )
        score = 1.0 if decision == EvaluationDecision.PASS else None
        blocking_issues = [report.message] if decision == EvaluationDecision.REVISE else []
        suggested_fixes = [report.message] if decision == EvaluationDecision.REVISE else []
        return EvaluationReport(
            id=str(uuid.uuid4()),
            run_id=run.id,
            task_id=task.id,
            iteration=run.iteration,
            evaluator_session_id=evaluator.id,
            overall_score=score,
            decision=decision,
            profile_results=report.profile_results,
            blocking_issues=blocking_issues,
            suggested_fixes=suggested_fixes,
            artifact_refs=report.artifact_refs,
            validation_reviewed=report.validation,
            risks=report.risks,
            confidence=report.confidence,
            requires_human_judgment=report.requires_human_judgment,
            created_at=now,
        )
