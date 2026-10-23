"""Static cross-mixin host contract shared by WorkspaceManager mixins."""

from __future__ import annotations

from abc import abstractmethod
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from ...models import AgentReportState, AgentType
from .contracts.runtime import _RuntimeHostContract

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextvars import ContextVar
    from typing import Any

    from ...models.schemas import TaskLifecycle
    from ..chat_message_inbox import ChatMessageInbox
    from ..task_lifecycle import TaskNotifier
    from ..task_mailbox import TaskMailbox
    from ._attachments import _PreparedWorkspaceAttachment
    from ._constants import (
        AgentReport,
        AgentRuntimeStatus,
        AutonomousRun,
        AutonomousRunPhase,
        ContinueTaskRequest,
        EnsureWorkspaceAgentRequest,
        ExecutionTarget,
        FeedbackLessonStore,
        FeedbackSummaryRequest,
        FeedbackSummaryRun,
        ManagedSession,
        ManagedSessionStatus,
        Optional,
        Path,
        ScheduledChatDispatch,
        ScheduledChatLiveness,
        ScheduledChatReadiness,
        ScheduledTask,
        ScheduledTaskRun,
        SessionKind,
        StartTaskRequest,
        Workspace,
        WorkspaceAttachment,
        WorkspaceAttachmentCreate,
        WorkspaceMarkdownDocument,
        WorkspaceTask,
        WorkspaceTaskCreate,
        WorkspaceTaskStatus,
        asyncio,
        datetime,
    )


class _WorkspaceManagerBase(_RuntimeHostContract):
    _dispatch_locks: dict[str, asyncio.Lock]
    _feedback_automation_task: asyncio.Task[None] | None
    _feedback_chat_busy: Callable[[], bool] | None
    _feedback_started_at: datetime
    _feedback_summary_locks: dict[str, asyncio.Lock]
    _hubtask_orphan_locks: dict[str, asyncio.Lock]
    _dedicated_agent_attempts: dict[str, datetime]
    _stranded_provisioning_jobs: dict[str, asyncio.Task[None]]
    _task_notifier: TaskNotifier | None
    _monitor_task: asyncio.Task[None] | None
    _pump_locks: dict[str, asyncio.Lock]
    _report_intake_committed: set[str]
    _report_intake_locks: dict[str, asyncio.Lock]
    _sched_fire_locks: dict[str, asyncio.Lock]
    _chat_delivery_getter: Callable[[], ChatMessageInbox] | None
    _chat_delivery_instance: ChatMessageInbox | None
    _scheduled_chat_dispatch: ScheduledChatDispatch | None
    _scheduled_chat_liveness: ScheduledChatLiveness | None
    _scheduled_chat_readiness: ScheduledChatReadiness | None
    _scheduled_legacy_run_ids: set[str]
    _scheduled_chat_recovery_pending: bool
    _task_activity_cursors: dict[str, tuple[int, int]]
    _task_execution_operations: dict[str, int]
    _workspace_mutation_held: ContextVar[tuple[str, asyncio.Task[Any] | None] | None]
    _worktree_root_cache: dict[str, tuple[float, list[Path]]]
    reports: dict[str, AgentReport]
    scheduled_task_runs: dict[str, ScheduledTaskRun]
    scheduled_tasks: dict[str, ScheduledTask]
    sessions: dict[str, ManagedSession]
    task_mailbox: TaskMailbox
    tasks: dict[str, WorkspaceTask]
    workspaces: dict[str, Workspace]

    @abstractmethod
    def _runtime_now(self) -> datetime:
        """Read the clock supplied by the public manager entrypoint."""
        ...

    @property
    @abstractmethod
    def _runtime_state_root(self) -> Path:
        """Resolve the caller-owned state root without importing the entrypoint."""
        ...

    @property
    @abstractmethod
    def _runtime_scheduled_tasks_file(self) -> Path: ...

    @abstractmethod
    def _runtime_resident_prompt(
        self, workspace: Workspace, base_url: str, session_id: str
    ) -> str: ...

    @staticmethod
    @abstractmethod
    def _path_looks_like_real_file(raw: str) -> bool: ...

    @abstractmethod
    def markdown_documents_for_workspace(
        self, workspace_id: str
    ) -> list[WorkspaceMarkdownDocument]: ...

    @abstractmethod
    def _prepare_attachments(
        self, workspace_id: str, owner_id: str, attachments: list[WorkspaceAttachmentCreate]
    ) -> list[_PreparedWorkspaceAttachment]: ...

    @abstractmethod
    def _write_prepared_attachments(
        self, prepared: list[_PreparedWorkspaceAttachment], created_paths: list[Path]
    ) -> None: ...

    @abstractmethod
    def _delete_created_attachment_paths(self, paths: list[Path]) -> None: ...

    @abstractmethod
    def _persist_attachments(
        self, workspace_id: str, owner_id: str, attachments: list[WorkspaceAttachmentCreate]
    ) -> list[WorkspaceAttachment]: ...

    @abstractmethod
    def _attachment_prompt_block(self, attachments: list[WorkspaceAttachment]) -> str: ...

    @abstractmethod
    def _append_attachment_block(
        self, message: str, attachments: list[WorkspaceAttachment]
    ) -> str: ...

    @abstractmethod
    async def _resume_chat_session(self, session_id: str) -> None: ...

    @abstractmethod
    async def _reset_session_context(self, session: ManagedSession) -> None: ...

    @abstractmethod
    def _refresh_chat_session_status(self, session: ManagedSession) -> bool: ...

    @abstractmethod
    async def dispatch_workspace(
        self, workspace_id: str, *, refresh_sessions: bool = True
    ) -> None: ...

    @abstractmethod
    async def start_task(
        self, task_id: str, payload: StartTaskRequest | None = None
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def continue_task(
        self,
        task_id: str,
        payload: ContinueTaskRequest | None = None,
        call_id: str | None = None,
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def _dispatch_task_to_session(
        self, task: WorkspaceTask, session: ManagedSession
    ) -> None: ...

    @abstractmethod
    def _kick_feedback_automation(self) -> None: ...

    @abstractmethod
    async def _stop_feedback_automation(self) -> None: ...

    @abstractmethod
    def _build_workspace_feedback_summary_prompt(
        self, workspace: Workspace, summary_input: dict[str, Any]
    ) -> tuple[str, list[str], list[str]]: ...

    @abstractmethod
    async def send_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def _send_session_message_locked(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
        *,
        question_key: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def resume_existing_call(self, session_id: str, call_id: str) -> bool: ...

    @abstractmethod
    async def _pump_session_messages(self, session_id: str) -> None: ...

    @abstractmethod
    def _expire_processing_leases(self, session_id: str, max_age_seconds: float = 300.0) -> int: ...

    @abstractmethod
    async def _recover_processing_via_receipt(self, session_id: str) -> int: ...

    @abstractmethod
    def _mark_processing_as_uncertain(self, session_id: str, call_ids: list[str]) -> None: ...

    @abstractmethod
    def _assert_session_seat(self, session: ManagedSession) -> str: ...

    @abstractmethod
    async def _capture_tmux_output(self, tmux_session: str) -> str: ...

    @abstractmethod
    async def _wait_for_agent_prompt(self, session: ManagedSession) -> None: ...

    @abstractmethod
    def _compute_payload_fingerprint(
        self, message: str, attachments: list[WorkspaceAttachmentCreate]
    ) -> str: ...

    @abstractmethod
    async def _refresh_session_statuses(
        self, workspace_id: Optional[str] = None, *, run_auto_continue: bool = False
    ) -> None: ...

    @abstractmethod
    def _expected_pending_prompt_prefixes(
        self, session: ManagedSession, task: WorkspaceTask
    ) -> list[str]: ...

    @abstractmethod
    def _mark_prompt_dispatch_stalled(
        self,
        *,
        task_id: str,
        session_id: str,
        message: str,
        message_zh: str,
        report_state: AgentReportState,
        sampled_at: datetime,
    ) -> None: ...

    @abstractmethod
    def _reconcile_task_report_statuses(self, workspace_id: str) -> None: ...

    @abstractmethod
    def _latest_report_state(self, task_id: str) -> AgentReportState | None: ...

    @abstractmethod
    def _latest_review_report_state(self, task_id: str) -> AgentReportState | None: ...

    @abstractmethod
    def _status_from_report(
        self, state: AgentReportState, session: ManagedSession
    ) -> ManagedSessionStatus: ...

    @abstractmethod
    def _runtime_from_report(
        self, state: AgentReportState, session: ManagedSession
    ) -> AgentRuntimeStatus: ...

    @abstractmethod
    def _task_status_from_report(
        self, state: AgentReportState
    ) -> Optional[WorkspaceTaskStatus]: ...

    @abstractmethod
    def _is_stale_report_for_aborted_task(
        self, task: WorkspaceTask, session: ManagedSession
    ) -> bool: ...

    @abstractmethod
    def _release_task_session(self, task: WorkspaceTask) -> None: ...

    @abstractmethod
    def _release_reviewer_session(
        self,
        task: WorkspaceTask,
        *,
        status: ManagedSessionStatus,
        runtime_status: AgentRuntimeStatus,
        updated_at: datetime,
        include_stale_assignments: bool = False,
    ) -> None: ...

    @abstractmethod
    async def _cleanup_reviewer_for_terminal_task(
        self, task: WorkspaceTask, *, updated_at: datetime, delete_tabs: bool = True
    ) -> list[str]: ...

    @abstractmethod
    def _rename_task_assignment_tab(self, task: WorkspaceTask, session: ManagedSession) -> None: ...

    @abstractmethod
    def _assign_current_task(
        self, session_id: str, task_id: str, *, rename_tab: bool = True
    ) -> None: ...

    @abstractmethod
    def _normalize_workspace_item(self, item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def _workspace_index_item(self, workspace: Workspace) -> dict[str, Any]: ...

    @abstractmethod
    def _normalize_task_item(self, item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def _normalize_report_item(self, item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def _default_autonomous_run(
        self, task_id: str | None, max_iterations: int = 3
    ) -> AutonomousRun: ...

    @abstractmethod
    def _normalize_session_item(self, item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def _atomic_write_text(self, path: Path, text: str) -> None: ...

    @abstractmethod
    def _workspace_state_payload(self, workspace_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def _save_workspace_state(self, workspace_id: str) -> None: ...

    @abstractmethod
    def _refresh_snapshot_best_effort(
        self, workspace_id: str, *, state_text: str | None = None
    ) -> None: ...

    @abstractmethod
    def _save_state(self) -> None: ...

    @abstractmethod
    def _build_session_bootstrap_prompt(
        self, workspace: Workspace, session: ManagedSession
    ) -> str: ...

    @abstractmethod
    def _report_base_url(self, session: ManagedSession) -> str: ...

    @abstractmethod
    def _report_prompt_call_id(
        self,
        task_id: str,
        purpose: str,
        *,
        attempt: int | str | None = None,
        cycle: int | str | None = None,
    ) -> str: ...

    @abstractmethod
    def _report_endpoint_curl(
        self,
        session: ManagedSession,
        task_id: str | None = None,
        *,
        purpose: str = "working-progress",
        attempt: int | str | None = None,
        state: str = "working",
    ) -> str: ...

    @abstractmethod
    def _build_dispatch_decision_prompt(
        self, workspace: Workspace, task: WorkspaceTask, dispatcher: ManagedSession
    ) -> str: ...

    @abstractmethod
    def _build_task_assignment_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        *,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str: ...

    @abstractmethod
    def _lesson_context_payload(self, workspace: Workspace, query: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _record_system_task_audit(
        self,
        *,
        task: WorkspaceTask,
        message: str,
        message_zh: str,
        validation: str,
        session_id: str = "system",
        state: AgentReportState = AgentReportState.WORKING,
    ) -> None: ...

    @abstractmethod
    def _build_review_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        trigger_report: AgentReport,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str: ...

    @abstractmethod
    def _build_continue_prompt(
        self,
        task: WorkspaceTask,
        payload: ContinueTaskRequest,
        session: ManagedSession,
        *,
        failure_reason: str | None = None,
    ) -> str: ...

    @abstractmethod
    def _build_hard_recovery_worker_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str: ...

    @abstractmethod
    def _build_hard_recovery_reviewer_fallback_prompt(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str: ...

    @abstractmethod
    def _build_hard_recovery_reviewer_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        trigger_report: AgentReport,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str: ...

    @abstractmethod
    def _snapshot_report_intake_workspace(self, workspace_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def _restore_report_intake_workspace(
        self, workspace_id: str, snapshot: dict[str, Any]
    ) -> None: ...

    @abstractmethod
    def _wake_report_intake_runs(self, wake_targets: set[tuple[str, str]]) -> None: ...

    @abstractmethod
    def _rollback_processing_to_pending(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> None: ...

    @abstractmethod
    def _bridge_report_to_agent_event(
        self, report: AgentReport, session: ManagedSession
    ) -> tuple[str, str] | None: ...

    @abstractmethod
    def _is_goal_packet_approval_review(self, task: WorkspaceTask, report: AgentReport) -> bool: ...

    @abstractmethod
    def _autonomous_phase_label(self, phase: AutonomousRunPhase) -> str: ...

    @abstractmethod
    def _autonomous_next_action(self, phase: AutonomousRunPhase) -> str: ...

    @abstractmethod
    def _autonomous_run_after_evaluation(
        self,
        task: WorkspaceTask,
        evaluator: ManagedSession,
        report: AgentReport,
        *,
        now: datetime,
    ) -> tuple[AutonomousRun | None, AutonomousRunPhase | None]: ...

    @abstractmethod
    async def _request_task_review(
        self, task: WorkspaceTask, trigger_report: AgentReport
    ) -> None: ...

    @abstractmethod
    async def _select_or_create_reviewer(
        self, workspace: Workspace, task: WorkspaceTask
    ) -> ManagedSession: ...

    @abstractmethod
    async def _handle_review_report(
        self, task: WorkspaceTask, reviewer: ManagedSession, report: AgentReport
    ) -> None: ...

    @abstractmethod
    def _load_scheduled_tasks(self) -> None: ...

    @abstractmethod
    async def _tick_scheduled_tasks(self) -> None: ...

    @staticmethod
    @abstractmethod
    async def _await_lifecycle_cleanup(task: "asyncio.Task[Any]") -> None: ...

    @abstractmethod
    def _quarantine_failed_session_deletion(self, session_id: str) -> None: ...

    @staticmethod
    @abstractmethod
    async def _retire_session_resources(session: ManagedSession) -> None: ...

    @abstractmethod
    async def _cleanup_deleted_session(
        self, session: ManagedSession, *, provider_fenced: bool
    ) -> None: ...

    @staticmethod
    @abstractmethod
    def _effective_agent_target(
        workspace: Workspace, payload: EnsureWorkspaceAgentRequest
    ) -> ExecutionTarget: ...

    @abstractmethod
    def _observe_task_lifecycles(self, workspace_id: str) -> int: ...

    @abstractmethod
    async def _deliver_task_notices(self, workspace_id: str) -> int: ...

    @abstractmethod
    def task_lifecycles(self, tasks: list[WorkspaceTask]) -> dict[str, TaskLifecycle]: ...

    @abstractmethod
    async def _provision_dedicated_agent(
        self, workspace: Workspace, task: WorkspaceTask
    ) -> ManagedSession: ...

    @abstractmethod
    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        payload_or_agent_type: EnsureWorkspaceAgentRequest | AgentType = AgentType.CODEX,
    ) -> ManagedSession: ...

    @abstractmethod
    def _resolve_remote_cwd(
        self, profile_id: str | None, requested_cwd: str | None, workspace_cwd: str | None
    ) -> str: ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> None: ...

    @abstractmethod
    def _workspace_dir(self, workspace_id: str) -> Path: ...

    @abstractmethod
    def _workspace_state_file(self, workspace_id: str) -> Path: ...

    @asynccontextmanager
    @abstractmethod
    async def workspace_mutation_lock(self, workspace_id: str) -> AsyncIterator[None]:
        yield cast(None, None)

    @abstractmethod
    def _workspace_task_records_dir(self, workspace_id: str) -> Path: ...

    @abstractmethod
    def _workspace_attachments_dir(self, workspace_id: str) -> Path: ...

    @abstractmethod
    def _feedback_store(self) -> FeedbackLessonStore: ...

    @abstractmethod
    def snapshot_path(self, workspace_id: str) -> Path: ...

    @abstractmethod
    def _workspace_owns_task(self, task: WorkspaceTask) -> bool: ...

    @abstractmethod
    def _require_workspace_execution(self, task: WorkspaceTask) -> WorkspaceTask: ...

    @asynccontextmanager
    @abstractmethod
    async def _workspace_execution_operation(
        self, task: WorkspaceTask
    ) -> AsyncIterator[WorkspaceTask]:
        yield cast(WorkspaceTask, None)

    @abstractmethod
    def _validate_task_execution_create(
        self, payload: WorkspaceTaskCreate, *, system_internal: bool, internal_kind: str | None
    ) -> str | None: ...

    @abstractmethod
    def _write_task_record(self, task: WorkspaceTask) -> None: ...

    @abstractmethod
    def _create_task(
        self,
        workspace_id: str,
        payload: WorkspaceTaskCreate,
        *,
        system_internal: bool = False,
        internal_kind: str | None = None,
        creation_request_key: str | None = None,
        creation_actor_key: str | None = None,
        creation_fingerprint: str | None = None,
    ) -> WorkspaceTask: ...

    @abstractmethod
    def require_workspace_task(self, workspace_id: str, task_id: str) -> WorkspaceTask: ...

    @abstractmethod
    def _active_feedback_summary_task(self, workspace_id: str) -> WorkspaceTask | None: ...

    @abstractmethod
    def _mark_feedback_summary_retryable(
        self, task: WorkspaceTask, *, reason: str, validation: str
    ) -> WorkspaceTask: ...

    @abstractmethod
    async def _summarize_workspace_feedback_locked(
        self,
        workspace_id: str,
        payload: FeedbackSummaryRequest,
        *,
        summary_input: dict[str, Any] | None = None,
    ) -> FeedbackSummaryRun: ...

    @abstractmethod
    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]: ...

    @abstractmethod
    async def _send_tmux_message(self, tmux_session: str, message: str) -> None: ...

    @abstractmethod
    def _message_still_in_input(self, output: str, message: str) -> bool: ...

    @abstractmethod
    async def _run_tmux(self, *args: str) -> None: ...

    @abstractmethod
    async def _query_tmux_receipt(self, tmux_session: str, call_id: str) -> bool: ...

    @abstractmethod
    async def _send_tmux_message_with_receipt(
        self, tmux_session: str, message: str, call_id: str
    ) -> None: ...

    @abstractmethod
    async def _ensure_submitted_without_repaste(self, tmux_session: str, message: str) -> None: ...

    @abstractmethod
    async def _interrupt_session(self, session: ManagedSession) -> None: ...

    @abstractmethod
    async def _rename_session_for_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        *,
        updated_at: datetime | None = None,
    ) -> ManagedSession: ...

    @abstractmethod
    def _sessions_for_workspace_raw(self, workspace_id: str) -> list[ManagedSession]: ...

    @abstractmethod
    def _workspace_agents(
        self, workspace_id: str, include_stopped: bool = False
    ) -> list[ManagedSession]: ...

    @abstractmethod
    def _non_terminal_tasks_referencing_session(
        self, session_id: str, *, exclude_task_id: str | None = None
    ) -> list[WorkspaceTask]: ...

    @abstractmethod
    def _session_raw_task_bindings_block_cleanup(
        self, session: ManagedSession, *, exclude_task_id: str | None = None
    ) -> bool: ...

    @abstractmethod
    def _find_compatible_workspace_agent(
        self, workspace: Workspace, payload: EnsureWorkspaceAgentRequest
    ) -> Optional[ManagedSession]: ...

    @abstractmethod
    def _reviewer_is_active(self, task: WorkspaceTask) -> bool: ...

    @abstractmethod
    def _release_stale_reviewer_for_task(
        self, task: WorkspaceTask, *, updated_at: datetime
    ) -> None: ...

    @abstractmethod
    def _cleanup_stale_reviewer_assignments(self, workspace_id: str) -> bool: ...

    @abstractmethod
    async def _prune_orphan_workspace_tabs(self, workspace_id: str) -> int: ...

    @abstractmethod
    async def _reap_stuck_reviews(self, workspace_id: str) -> int: ...

    @abstractmethod
    def _review_submission_block(self, task: WorkspaceTask, reviewer: ManagedSession) -> str: ...

    @abstractmethod
    def _first_available_reviewer(
        self,
        workspace_id: str,
        *,
        target: ExecutionTarget,
        cwd: str,
        remote_profile_id: str | None = None,
        remote_cwd: str | None = None,
        session_kind: SessionKind | None = None,
    ) -> Optional[ManagedSession]: ...

    @abstractmethod
    def _queued_count(self, session_id: str) -> int: ...

    @abstractmethod
    def _sync_session_tab_metadata(self, session: ManagedSession) -> None: ...

    @abstractmethod
    def _reconcile_workspace_session_pointers(self, workspace_id: str) -> None: ...
