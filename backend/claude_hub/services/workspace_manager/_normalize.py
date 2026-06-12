"""Model normalizers for persisted workspace items."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _NormalizeMixin:
    def _normalize_workspace_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.pop("agent_session_id", None)
        normalized.setdefault("dispatcher_session_id", None)
        normalized.setdefault("target", ExecutionTarget.LOCAL.value)
        normalized.setdefault("remote_profile_id", None)
        normalized.setdefault("remote_cwd", None)
        normalized.setdefault("remote_reconnect", True)
        return normalized

    def _normalize_task_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        if normalized.get("status") == "assigned":
            normalized["status"] = WorkspaceTaskStatus.QUEUED.value
            normalized.setdefault("queued_at", normalized.get("updated_at"))
        if normalized.get("task_mode") not in {"direct", "reviewed", "autonomous"}:
            normalized["task_mode"] = WorkspaceTaskMode.REVIEWED.value
        if normalized.get("execution_complexity") not in {"auto", "simple", "complex"}:
            normalized["execution_complexity"] = WorkspaceTaskExecutionComplexity.AUTO.value
        normalized.setdefault("related_task_id", None)
        normalized.setdefault("attachments", [])
        normalized["review_profiles"] = self._normalize_review_profiles(
            normalized.get("review_profiles")
        )
        normalized.setdefault("clear_context", None)
        normalized.setdefault("dispatch_reason", None)
        normalized.setdefault("dispatch_pending", False)
        normalized.setdefault("system_internal", False)
        normalized.setdefault("internal_kind", None)
        normalized["feedback_lesson_ids"] = self._normalize_string_list(
            normalized.get("feedback_lesson_ids")
        )
        normalized.setdefault("review_session_id", None)
        normalized.setdefault("review_attempts", 0)
        # Review-cycle ordinals. Derive from existing verdict state so tasks
        # persisted before this field existed still judge correctly: a parked
        # task (verdict present) migrates to 1/1 so stale echoes suppress, while
        # an in-flight task (no verdict) migrates to 1/0 so a live resubmit still
        # dispatches a reviewer.
        has_verdict = normalized.get("review_completed_at") is not None
        normalized.setdefault("reviewed_cycle", 1 if has_verdict else 0)
        normalized.setdefault("review_cycle", max(1, normalized["reviewed_cycle"]))
        normalized.setdefault("review_requested_at", None)
        normalized.setdefault("review_completed_at", None)
        normalized.setdefault("review_skipped_at", None)
        normalized.setdefault("review_skip_reason", None)
        normalized.setdefault("human_acceptance_requested_at", None)
        normalized.setdefault("human_accepted_at", None)
        normalized.setdefault("queued_at", None)
        normalized.setdefault("started_at", None)
        normalized.setdefault("reviewed_at", None)
        normalized.setdefault("completed_at", None)
        normalized["goal_packet"] = self._normalize_goal_packet(normalized.get("goal_packet"))
        normalized["autonomy_policy"] = self._normalize_autonomy_policy(
            normalized.get("autonomy_policy"),
            task_mode=normalized["task_mode"],
        )
        normalized["autonomous_run"] = self._normalize_autonomous_run(
            normalized.get("autonomous_run"),
            task_id=normalized.get("id"),
            task_mode=normalized["task_mode"],
            policy=normalized["autonomy_policy"],
        )
        return normalized

    def _normalize_report_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized["acceptance_check"] = self._normalize_acceptance_check(
            normalized.get("acceptance_check")
        )
        normalized["review_profiles"] = self._normalize_review_profiles(
            normalized.get("review_profiles")
        )
        normalized["profile_results"] = self._normalize_review_profile_results(
            normalized.get("profile_results")
        )
        normalized["artifact_refs"] = self._normalize_string_list(normalized.get("artifact_refs"))
        if not isinstance(normalized.get("confidence"), (int, float)):
            normalized["confidence"] = None
        normalized["requires_human_judgment"] = bool(
            normalized.get("requires_human_judgment", False)
        )
        normalized["evaluation_report"] = self._normalize_evaluation_report(
            normalized.get("evaluation_report"),
            task_id=normalized.get("task_id"),
            session_id=normalized.get("session_id"),
        )
        # Legacy reports have no stamped cycle; default 0 so they rank below any
        # post-migration round (reviewed_cycle starts at >=1 once a verdict lands).
        normalized.setdefault("review_cycle", 0)
        return normalized

    def _normalize_goal_packet(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        objective = value.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return None
        normalized = dict(value)
        normalized["objective"] = objective.strip()
        for field in (
            "acceptance_criteria",
            "validation_plan",
            "assumptions",
            "out_of_scope",
            "handoff_requirements",
        ):
            items = normalized.get(field)
            if isinstance(items, list):
                normalized[field] = [str(item) for item in items if str(item).strip()]
            elif isinstance(items, str) and items.strip():
                normalized[field] = [items.strip()]
            else:
                normalized[field] = []
        normalized.setdefault("source", "agent_generated")
        if normalized.get("status") not in {status.value for status in GoalPacketStatus}:
            normalized["status"] = "draft"
        normalized.setdefault("created_at", None)
        normalized.setdefault("updated_at", None)
        return normalized

    def _normalize_autonomy_policy(
        self,
        value: Any,
        *,
        task_mode: str,
    ) -> dict[str, Any] | None:
        if task_mode != WorkspaceTaskMode.AUTONOMOUS.value:
            return None
        if not isinstance(value, dict):
            return AutonomyPolicy().model_dump(mode="json")
        normalized = dict(value)
        max_iterations = normalized.get("max_iterations", 3)
        if not isinstance(max_iterations, int) or max_iterations < 1:
            normalized["max_iterations"] = 3
        if normalized.get("evaluation_strictness") not in {"lenient", "balanced", "strict"}:
            normalized["evaluation_strictness"] = "balanced"
        normalized.setdefault("allow_web_research", False)
        normalized.setdefault("require_artifact_review", False)
        normalized["review_profiles"] = self._normalize_review_profiles(
            normalized.get("review_profiles")
        )
        if normalized.get("human_checkpoint_policy") not in {
            "final_only",
            "after_rubric",
            "every_iteration",
        }:
            normalized["human_checkpoint_policy"] = "final_only"
        allowed = normalized.get("allowed_agent_types")
        normalized["allowed_agent_types"] = allowed if isinstance(allowed, list) else []
        normalized.setdefault("stop_on_repeated_failure", True)
        return normalized

    def _normalize_evaluation_report(
        self,
        value: Any,
        *,
        task_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        normalized = dict(value)
        normalized.setdefault("id", str(uuid.uuid4()))
        normalized.setdefault("run_id", None)
        normalized.setdefault("task_id", task_id)
        normalized.setdefault("iteration", 1)
        normalized.setdefault("evaluator_session_id", session_id)
        if normalized.get("decision") not in {"pass", "revise", "needs_input", "fail", "escalate"}:
            normalized["decision"] = "needs_input"
        for field in ("criterion_results", "blocking_issues", "suggested_fixes"):
            if not isinstance(normalized.get(field), list):
                normalized[field] = []
        normalized["profile_results"] = self._normalize_review_profile_results(
            normalized.get("profile_results")
        )
        normalized["artifact_refs"] = self._normalize_string_list(normalized.get("artifact_refs"))
        normalized.setdefault("overall_score", None)
        normalized.setdefault("validation_reviewed", None)
        normalized.setdefault("risks", None)
        if not isinstance(normalized.get("confidence"), (int, float)):
            normalized["confidence"] = None
        normalized["requires_human_judgment"] = bool(
            normalized.get("requires_human_judgment", False)
        )
        normalized.setdefault("created_at", None)
        return normalized

    def _normalize_autonomous_run(
        self,
        value: Any,
        *,
        task_id: str | None,
        task_mode: str,
        policy: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if task_mode != WorkspaceTaskMode.AUTONOMOUS.value:
            return None
        max_iterations_value = (policy or {}).get("max_iterations") or 3
        max_iterations = max_iterations_value if isinstance(max_iterations_value, int) else 3
        if not isinstance(value, dict):
            return self._default_autonomous_run(task_id, max_iterations).model_dump(mode="json")
        normalized = dict(value)
        normalized.setdefault("id", str(uuid.uuid4()))
        normalized.setdefault("task_id", task_id)
        if normalized.get("phase") not in {phase.value for phase in AutonomousRunPhase}:
            normalized["phase"] = AutonomousRunPhase.INTAKE.value
        if not isinstance(normalized.get("iteration"), int) or normalized["iteration"] < 1:
            normalized["iteration"] = 1
        if (
            not isinstance(normalized.get("max_iterations"), int)
            or normalized["max_iterations"] < 1
        ):
            normalized["max_iterations"] = max_iterations
        normalized.setdefault("status_summary", "Intake")
        if not isinstance(normalized.get("active_session_ids"), list):
            normalized["active_session_ids"] = []
        normalized.setdefault("pass_threshold", 0.8)
        normalized.setdefault("current_score", None)
        normalized.setdefault("next_action", "Derive Goal Packet and begin work")
        normalized.setdefault("paused_at", None)
        normalized.setdefault("exhausted_at", None)
        normalized.setdefault("completed_at", None)
        if not isinstance(normalized.get("rubric"), list):
            normalized["rubric"] = []
        evaluation_reports = normalized.get("evaluation_reports", [])
        if not isinstance(evaluation_reports, list):
            evaluation_reports = []
        normalized["evaluation_reports"] = [
            item
            for item in (
                self._normalize_evaluation_report(
                    item,
                    task_id=task_id,
                    session_id=item.get("evaluator_session_id") if isinstance(item, dict) else None,
                )
                for item in evaluation_reports
            )
            if item is not None
        ]
        if not isinstance(normalized.get("iterations"), list):
            normalized["iterations"] = []
        return normalized

    def _default_autonomous_run(
        self,
        task_id: str | None,
        max_iterations: int = 3,
    ) -> AutonomousRun:
        return AutonomousRun(
            id=str(uuid.uuid4()),
            task_id=task_id,
            max_iterations=max_iterations,
            status_summary="Intake",
            next_action="Derive Goal Packet and begin work",
        )

    def _normalize_acceptance_check(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            criterion = item.get("criterion")
            evidence = item.get("evidence")
            if not isinstance(criterion, str) or not criterion.strip():
                continue
            if not isinstance(evidence, str) or not evidence.strip():
                evidence = "No evidence provided."
            status = item.get("status", "not_checked")
            if status not in {"passed", "failed", "partial", "not_checked"}:
                status = "not_checked"
            normalized.append(
                {
                    "criterion": criterion.strip(),
                    "status": status,
                    "evidence": evidence.strip(),
                }
            )
        return normalized

    def _normalize_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _normalize_review_profiles(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        profiles: list[str] = []
        allowed = {profile.value for profile in ReviewProfile}
        for item in value:
            profile = item.value if isinstance(item, ReviewProfile) else str(item)
            if profile in allowed and profile not in profiles:
                profiles.append(profile)
        return profiles

    def _normalize_review_profile_results(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        results: list[dict[str, Any]] = []
        allowed_profiles = {profile.value for profile in ReviewProfile}
        allowed_statuses = {"passed", "failed", "partial", "not_checked"}
        for item in value:
            if not isinstance(item, dict):
                continue
            profile = item.get("profile")
            if isinstance(profile, ReviewProfile):
                profile = profile.value
            if profile not in allowed_profiles:
                continue
            status = item.get("status", "not_checked")
            if status not in allowed_statuses:
                status = "not_checked"
            results.append(
                {
                    "profile": profile,
                    "status": status,
                    "evidence": str(item.get("evidence") or "").strip(),
                    "blocking_findings": self._normalize_string_list(item.get("blocking_findings")),
                    "non_blocking_findings": self._normalize_string_list(
                        item.get("non_blocking_findings")
                    ),
                }
            )
        return results

    def _normalize_session_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.setdefault("runtime_status", self._runtime_from_managed_status(normalized))
        normalized.setdefault("current_task_id", normalized.get("task_id"))
        normalized.setdefault("queued_count", 0)
        normalized.setdefault(
            "target",
            (
                ExecutionTarget.REMOTE.value
                if normalized.get("remote_forward_port")
                else ExecutionTarget.LOCAL.value
            ),
        )
        normalized.setdefault("remote_profile_id", None)
        normalized.setdefault("remote_cwd", None)
        normalized.setdefault("remote_reconnect", True)
        normalized.setdefault("solo_mode", True)
        normalized.setdefault("ephemeral", False)
        normalized.setdefault("remote_forward_port", None)
        normalized.setdefault("auto_continue_task_id", None)
        normalized.setdefault("auto_continue_attempts", 0)
        normalized.setdefault("last_auto_continue_at", None)
        normalized.setdefault("prompt_retry_task_id", None)
        normalized.setdefault("prompt_retry_attempted_at", None)
        return normalized

    def _runtime_from_managed_status(self, item: dict[str, Any]) -> str:
        status = item.get("status")
        if status == ManagedSessionStatus.WORKING.value:
            return AgentRuntimeStatus.WORKING.value
        if status == ManagedSessionStatus.NEEDS_INPUT.value:
            return AgentRuntimeStatus.ATTENTION.value
        if status == ManagedSessionStatus.STOPPED.value:
            return AgentRuntimeStatus.OFFLINE.value
        return AgentRuntimeStatus.IDLE.value
