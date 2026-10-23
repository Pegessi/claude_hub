"""Cross-responsibility execution methods supplied by WorkspaceManager."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any, Optional

    import claude_hub.services.managed_chat as managed_chat

    from .._constants import *  # noqa: F403


class _RuntimeHostContract(ABC):
    @abstractmethod
    def _can_dispatch_to(self, session: ManagedSession) -> bool: ...

    @abstractmethod
    def _is_holding_unresolved_review_task(self, session: ManagedSession) -> bool: ...

    @abstractmethod
    async def _send_dispatch_message(
        self, session_id: str, dispatch_call_id: str, prompt: str
    ) -> None: ...

    @abstractmethod
    async def _start_task_workspace(
        self, task_id: str, payload: StartTaskRequest | None = None
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def _apply_dispatch_decision_workspace(
        self, task_id: str, payload: DispatchDecisionRequest
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def _continue_task_workspace(
        self, task_id: str, payload: ContinueTaskRequest | None = None, call_id: str | None = None
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def _request_task_review_workspace(
        self, task_id: str, payload: RequestTaskReviewRequest | None = None
    ) -> WorkspaceTask: ...

    @abstractmethod
    def _map_runtime_status(self, status: TerminalAgentStatus) -> ManagedSessionStatus: ...

    @abstractmethod
    def _is_spawn_grace_period(self, session: ManagedSession) -> bool: ...

    @abstractmethod
    async def _auto_continue_stopped_task(
        self, session: ManagedSession, task: WorkspaceTask, sampled_at: datetime
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def _latest_report_has_risk(self, task_id: str, risk_level: str) -> bool: ...

    @abstractmethod
    def _current_monitor_entities(
        self, session: ManagedSession, task: WorkspaceTask
    ) -> tuple[ManagedSession, WorkspaceTask] | None: ...

    @abstractmethod
    def _sealed_cycle_verdict_state(self, task_id: str, cycle: int) -> AgentReportState | None: ...

    @abstractmethod
    def _task_has_post_cycle_report(self, task_id: str, cycle: int) -> bool: ...

    @abstractmethod
    def _auto_continue_output_looks_busy(self, output: str) -> bool: ...

    @abstractmethod
    def _auto_continue_interruption_reason(self, output: str) -> str | None: ...

    @abstractmethod
    def _auto_continue_completion_reason(self, output: str) -> str | None: ...

    @abstractmethod
    def _latest_report_for_task(self, task_id: str) -> AgentReport | None: ...

    @abstractmethod
    async def _review_dispatch_failed(self, task: WorkspaceTask) -> bool: ...

    @abstractmethod
    def _reviewer_has_active_task_binding(self, session: ManagedSession) -> bool: ...

    @abstractmethod
    def _with_assignment_summary(self, session: ManagedSession) -> ManagedSession: ...

    @abstractmethod
    def _cleanup_stale_orchestrator_assignments(self, workspace_id: str) -> bool: ...

    @abstractmethod
    def latest_reports_per_task_for_workspace(self, workspace_id: str) -> list[AgentReport]: ...

    @abstractmethod
    def sessions_for_workspace(
        self, workspace_id: str, *, include_archived: bool = False
    ) -> list[ManagedSession]: ...

    @abstractmethod
    def _emit_delivery_uncertain(
        self, workspace_id: str, session_id: str, call_id: str
    ) -> None: ...

    @abstractmethod
    def _chat_delivery_state(
        self, session: ManagedSession, call_id: str
    ) -> managed_chat.DeliveryState: ...

    @abstractmethod
    async def _pump_session_messages_locked(self, session_id: str) -> None: ...

    @abstractmethod
    def _compute_persisted_payload_fingerprint(
        self, message: str, attachments: list[WorkspaceAttachment]
    ) -> str: ...

    @abstractmethod
    def _safe_attachment_owner_id(self, session_id: str, call_id: str) -> str: ...

    @abstractmethod
    async def _ensure_session_ready_for_send(self, session: ManagedSession) -> None: ...

    @abstractmethod
    def _rollback_chat_processing_to_pending(
        self, session_id: str, call_ids: list[str]
    ) -> None: ...

    @staticmethod
    @abstractmethod
    def _normalize_periodic_tasks(
        tasks: Optional[list["ResidentPeriodicTask"]],
    ) -> list["ResidentPeriodicTask"]: ...

    @staticmethod
    @abstractmethod
    def _resident_launch_config_changed(
        workspace: "Workspace", update_kwargs: dict[str, Any]
    ) -> bool: ...

    @abstractmethod
    def _resident_next_run_at(
        self, workspace: "Workspace", last_run: Optional[datetime]
    ) -> Optional[datetime]: ...

    @abstractmethod
    async def _delete_session_locked(self, session: ManagedSession) -> None: ...

    @abstractmethod
    async def _archive_session_locked(self, session: ManagedSession) -> None: ...

    @abstractmethod
    async def _unarchive_session_locked(self, session: ManagedSession) -> None: ...

    @abstractmethod
    def _clear_retired_session_pointers(self, session: ManagedSession) -> None: ...
