"""Construction, path helpers, and state loading."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Callable

from pydantic import ValidationError

from ...models.task_mailbox import TaskEvent
from ..chat_message_inbox import ChatMessageInbox
from ..legacy_state_migration import (
    LEGACY_TASK_KEYS,
    index_has_legacy_keys,
    migrate_raw_workspace_state,
    state_has_legacy_orchestration_keys,
)
from ..task_lifecycle import TaskNotifier
from ..task_mailbox import TaskMailbox
from ._base import _WorkspaceManagerBase
from ._constants import (
    INDEX_FILE,
    LEGACY_STATE_FILE,
    AgentReport,
    Any,
    FeedbackLessonStore,
    ManagedSession,
    ManagedSessionStatus,
    Path,
    ScheduledChatDispatch,
    ScheduledChatLiveness,
    ScheduledChatReadiness,
    ScheduledTask,
    ScheduledTaskRun,
    Workspace,
    WorkspaceTask,
    asyncio,
    datetime,
    json,
    logger,
)


class _UnknownTaskFieldsError(ValueError):
    """A persisted task contains fields this build cannot interpret."""


class _StateMixin(_WorkspaceManagerBase):
    scheduled_task_runs: dict[str, ScheduledTaskRun]
    _scheduled_chat_recovery_pending: bool

    def __init__(self) -> None:
        self._feedback_started_at = self._runtime_now()
        # Fail-closed startup signals from quarantined durable state stores;
        # surfaced through state_recovery_warnings() / /health while a marker
        # remains on disk.
        self._state_recovery_warnings: list[str] = []
        self.workspaces: dict[str, Workspace] = {}
        self.tasks: dict[str, WorkspaceTask] = {}
        self._task_activity_cursors: dict[str, tuple[int, int]] = {}
        self._task_execution_operations: dict[str, int] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.reports: dict[str, AgentReport] = {}
        self.scheduled_tasks: dict[str, ScheduledTask] = {}
        self.scheduled_task_runs: dict[str, ScheduledTaskRun] = {}
        # Per-task fire locks: serialize the 5s tick and a manual run-now so
        # the same task cannot be stamped / fired twice concurrently.
        self._sched_fire_locks: dict[str, asyncio.Lock] = {}
        # Per-internal-task locks serializing orphan convergence for a WORKING
        # scheduled hub_task whose worker session died. Keyed by the internal
        # WorkspaceTask id (distinct from the ScheduledTask fire lock above).
        self._hubtask_orphan_locks: dict[str, asyncio.Lock] = {}
        # Injected by the API layer: delivers Task notices (e.g. to Feishu).
        self._task_notifier: TaskNotifier | None = None
        # One background job per workspace delivers Task notices.
        self._task_notice_jobs: dict[str, asyncio.Task[None]] = {}
        self._scheduled_legacy_run_ids: set[str] = set()
        self._scheduled_chat_recovery_pending = False
        self._chat_delivery_getter: Callable[[], ChatMessageInbox] | None = None
        self._chat_delivery_instance: ChatMessageInbox | None = None
        self._scheduled_chat_dispatch: ScheduledChatDispatch | None = None
        # Injected readiness probe: ensures the target Chat's native runtime
        # (tailer + provider) is spawned and reports whether it is ready to
        # accept a turn. A cold/idle Chat is lazily spawned here and the run is
        # parked WAITING with a bounded redrain instead of delivered into a
        # runtime that does not exist yet.
        self._scheduled_chat_readiness: ScheduledChatReadiness | None = None
        # Injected live liveness probe for a dispatched Chat turn. Returns
        # "active" (provider still owns the turn), "terminalized" (a dead
        # orphan was just terminalized and its completion observer will fire),
        # "dead" (no start edge and the provider is idle), or "unknown".
        self._scheduled_chat_liveness: ScheduledChatLiveness | None = None
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        self._feedback_summary_locks: dict[str, asyncio.Lock] = {}
        # Per-session pump locks: serialize _pump_session_messages so two
        # concurrent pump cycles cannot both send the same pending call_id
        # to tmux (which would duplicate the model turn).
        self._pump_locks: dict[str, asyncio.Lock] = {}
        # Per-workspace mutation lock shared by report intake and TaskMailbox
        # writes. A report may snapshot then restore the whole workspace; a
        # concurrent mailbox persist in that window would be durable until
        # restore erased it.
        self._report_intake_locks: dict[str, asyncio.Lock] = {}
        # Ephemeral commit markers used only to distinguish a pre-commit
        # exception (restore the full report-intake snapshot) from a
        # post-commit side-effect exception (leave durable state intact so an
        # idempotent retry can finish the side effects).
        self._report_intake_committed: set[str] = set()
        # Re-entrancy for workspace_mutation_lock: create_report already holds
        # the lock when it ACKs mailbox state.
        self._workspace_mutation_held: ContextVar[tuple[str, asyncio.Task[Any] | None] | None] = (
            ContextVar(
                f"workspace_mutation_held_{id(self)}",
                default=None,
            )
        )
        self._monitor_task: asyncio.Task[None] | None = None
        # Cache of resolved git worktree roots per workspace id: (timestamp, roots).
        # Used by artifact preview to resolve markdown produced inside a worktree.
        self._worktree_root_cache: dict[str, tuple[float, list[Path]]] = {}
        self.task_mailbox = TaskMailbox(self)
        # Schedule loading only parses records; reference resolution and firing
        # happen later. Reject unknown fields before core recovery can save.
        self._load_scheduled_tasks()
        self._load_state()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return self._runtime_state_root / workspace_id

    def _workspace_state_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "state.json"

    def _workspace_mutation_lock(self, workspace_id: str) -> asyncio.Lock:
        lock = self._report_intake_locks.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._report_intake_locks[workspace_id] = lock
        return lock

    @asynccontextmanager
    async def workspace_mutation_lock(self, workspace_id: str) -> AsyncIterator[None]:
        """Serialize report intake with TaskMailbox workspace mutations.

        Report rollback restores a workspace snapshot. spawn / send /
        followup / interrupt / ack persist the same state.json; they wait
        on this lock so a concurrent tree write cannot land between
        snapshot and restore. Re-entrant for the owning coroutine.
        """
        owner = self._workspace_mutation_held.get()
        current_task = asyncio.current_task()
        if owner is not None and owner == (workspace_id, current_task):
            yield
            return
        async with self._workspace_mutation_lock(workspace_id):
            token = self._workspace_mutation_held.set((workspace_id, current_task))
            try:
                yield
            finally:
                self._workspace_mutation_held.reset(token)

    def _workspace_task_records_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "task_records"

    def _workspace_attachments_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "attachments"

    def _feedback_store(self) -> FeedbackLessonStore:
        return FeedbackLessonStore(self._runtime_state_root)

    def snapshot_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "snapshot.md"

    def _load_state(self) -> None:
        if INDEX_FILE.exists() or self._backup_path_for(INDEX_FILE).exists():
            # An orphan backup with no primary is an anomaly; the durable
            # loader recovers/quarantines it instead of silently starting empty.
            self._load_nested_state()
        elif LEGACY_STATE_FILE.exists():
            self._load_legacy_state()

        for session_id, session in list(self.sessions.items()):
            for task_id in (session.task_id, session.current_task_id):
                linked = self.tasks.get(task_id or "")
                if linked is not None and not self._workspace_owns_task(linked):
                    raise ValueError("managed session references an initiator task")
            if session.current_task_id is None and session.task_id is not None:
                self.sessions[session_id] = session.model_copy(
                    update={"current_task_id": session.task_id}
                )

        # Cold-start delivery recovery: a call_id in processing_call_ids at
        # startup means the Hub crashed while the message was in-flight.
        #
        # For LIVE sessions we do NOT immediately move processing call_ids to
        # uncertain. Instead we leave them in processing so the monitor's
        # receipt-based reconciliation (_recover_processing_via_receipt) can
        # distinguish:
        #   * receipt present  -> keep processing, no repaste (await worker ACK)
        #   * receipt absent   -> move to pending for one safe re-delivery
        #   * session gone     -> move to uncertain (fail-closed)
        #
        # For STOPPED sessions the tmux inbox is gone and we cannot query the
        # receipt, so we fail closed: move processing call_ids to uncertain.
        self._recover_uncertain_deliveries()

        # A snapshot may be absent/stale after a crash or a derived-output failure.
        # Rebuild from committed files only; never save mutable state to refresh a cache.
        for workspace_id in self.workspaces:
            if self._workspace_state_file(workspace_id).exists():
                self._refresh_snapshot_best_effort(workspace_id)

    def _recover_uncertain_deliveries(self) -> None:
        """Cold-start delivery recovery.

        For STOPPED sessions (tmux inbox gone, receipt unqueryable) move all
        in-flight (processing) call_ids to uncertain (fail-closed: no
        auto-resend, no silent delivered).

        For LIVE sessions leave processing call_ids in place so the monitor's
        ``_recover_processing_via_receipt`` can reconcile against the
        tmux-server receipt (present -> keep processing; absent -> pending).
        """
        for session_id in list(self.sessions.keys()):
            session = self.sessions.get(session_id)
            if session is None:
                continue
            if not session.processing_call_ids:
                continue
            # Only fail-closed immediately for sessions whose tmux inbox is
            # gone. Live sessions get receipt-based reconciliation in the
            # monitor tick.
            if session.status != ManagedSessionStatus.STOPPED:
                continue
            self._mark_processing_as_uncertain(session_id, list(session.processing_call_ids))

    @staticmethod
    def _validate_task_record_fields(items: Any, *, allow_legacy: bool = False) -> None:
        known = set(WorkspaceTask.model_fields)
        if allow_legacy:
            known.update(LEGACY_TASK_KEYS)
        for item in items:
            if isinstance(item, dict) and item.keys() - known:
                raise _UnknownTaskFieldsError("Workspace task record contains unknown fields")

    def _payload_needs_legacy_migration(
        self, raw: dict[str, Any], index_item: dict[str, Any] | None
    ) -> bool:
        return state_has_legacy_orchestration_keys(raw) or (
            index_item is not None and index_has_legacy_keys(index_item)
        )

    def _validate_index_payload(self, raw: Any) -> dict[str, Any]:
        """Structural validation for index.json (run on primary and backup)."""

        if not isinstance(raw, dict):
            raise ValueError("index payload must be a JSON object")
        items = raw.get("workspaces")
        if not isinstance(items, list):
            raise ValueError("index payload 'workspaces' must be a list")
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError("index workspace entries must be objects with an 'id'")
            # Full structural parse; an entry this build cannot model means the
            # file must not be loaded and later rewritten as-is.
            Workspace(**self._normalize_workspace_item(item))
        return raw

    def _validate_workspace_state_payload(
        self,
        raw: Any,
        workspace_id: str,
        index_item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Structural validation for one workspace state.json (primary/backup)."""

        if not isinstance(raw, dict):
            raise ValueError("workspace state payload must be a JSON object")
        for key in ("tasks", "sessions", "reports"):
            if not isinstance(raw.get(key, []), list):
                raise ValueError(f"workspace state {key!r} must be a list")
        needs_migration = self._payload_needs_legacy_migration(raw, index_item)
        self._validate_task_record_fields(raw.get("tasks", []), allow_legacy=needs_migration)
        # Legacy records contain keys this build's models reject (e.g.
        # ``agent_run_id``); they are only valid after the migration mapping.
        candidate = raw
        if needs_migration:
            candidate = migrate_raw_workspace_state(
                workspace_id=workspace_id,
                raw=raw,
                index_item=dict(index_item) if index_item is not None else None,
            ).state
        for item in candidate.get("tasks", []):
            WorkspaceTask(**self._normalize_task_item(item))
        for item in candidate.get("sessions", []):
            ManagedSession(**self._normalize_session_item(item))
        for item in candidate.get("reports", []):
            AgentReport(**self._normalize_report_item(item))
        for item in candidate.get("task_events") or []:
            if not isinstance(item, dict):
                raise ValueError("workspace state 'task_events' entries must be objects")
            TaskEvent(**item)
        return raw

    def _migrate_workspace_state_payload(
        self,
        workspace_id: str,
        state_file: Path,
        raw: dict[str, Any],
        index_item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Rewrite a legacy payload in place (validated before calling)."""

        index_copy = dict(index_item) if index_item is not None else None
        result = migrate_raw_workspace_state(
            workspace_id=workspace_id,
            raw=raw,
            index_item=index_copy,
        )
        migrated = result.state
        backup = state_file.with_suffix(".json.pre-migration-backup")
        if not backup.exists():
            backup.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self._atomic_write_text(state_file, json.dumps(migrated, indent=2))
        if index_copy is not None and index_item is not None:
            index_item.clear()
            index_item.update(index_copy)
        if result.discarded_runs or result.discarded_events:
            logger.info(
                "Migrated legacy Agent Tree state workspace_id=%s discarded_runs=%s "
                "discarded_events=%s warnings=%s",
                workspace_id,
                result.discarded_runs,
                result.discarded_events,
                result.warnings,
            )
        return migrated

    def _workspace_state_validator(
        self, workspace_id: str, index_item: dict[str, Any] | None
    ) -> Callable[[Any], dict[str, Any]]:
        """Bind the index item to one workspace state validator."""

        def validate(raw: Any) -> dict[str, Any]:
            return self._validate_workspace_state_payload(raw, workspace_id, index_item)

        return validate

    def _load_nested_state(self) -> None:
        index_result = self._load_durable_json(
            INDEX_FILE,
            label="workspace index",
            validator=self._validate_index_payload,
        )
        index = index_result.data if isinstance(index_result.data, dict) else {}
        index_items: dict[str, dict[str, Any]] = {}
        self.workspaces = {}
        index_dirty = False
        for item in index.get("workspaces", []):
            workspace_id = str(item["id"])
            index_items[workspace_id] = item
            if index_has_legacy_keys(item):
                index_dirty = True
            self.workspaces[workspace_id] = Workspace(**self._normalize_workspace_item(item))

        migrated_any = False
        for workspace_id in self.workspaces:
            state_file = self._workspace_state_file(workspace_id)
            index_item = index_items.get(workspace_id)
            result = self._load_durable_json(
                state_file,
                label=f"workspace state ({workspace_id})",
                validator=self._workspace_state_validator(workspace_id, index_item),
            )
            data = result.data
            if not isinstance(data, dict):
                # Quarantined without a usable backup: the workspace row stays
                # indexed but its tasks/sessions/reports start empty; evidence
                # and the marker prevent silent evidence loss.
                continue
            if self._payload_needs_legacy_migration(data, index_item):
                data = self._migrate_workspace_state_payload(
                    workspace_id, state_file, data, index_item
                )
            if state_has_legacy_orchestration_keys(data):
                raise ValueError(
                    f"Legacy Agent Tree keys remain after migration workspace_id={workspace_id}"
                )
            if index_item is not None and index_has_legacy_keys(index_item):
                index_dirty = True
            if state_file.with_suffix(".json.pre-migration-backup").exists():
                migrated_any = True
            for item in data.get("tasks", []):
                task = WorkspaceTask(**self._normalize_task_item(item))
                self.tasks[task.id] = task
            for item in data.get("sessions", []):
                session = ManagedSession(**self._normalize_session_item(item))
                self.sessions[session.id] = session
            for item in data.get("reports", []):
                report = AgentReport(**self._normalize_report_item(item))
                self.reports[report.id] = report
            from ..task_graph import materialize_loaded_task_graph

            materialize_loaded_task_graph(self.tasks, workspace_id)
            self.task_mailbox.load_from_dict(workspace_id, data)

        if index_dirty:
            index_payload = {
                "workspaces": [
                    self._workspace_index_item(item) for item in self.workspaces.values()
                ]
            }
            self._atomic_save_state_text(INDEX_FILE, json.dumps(index_payload, indent=2))
        elif migrated_any:
            pass

    def _load_legacy_state(self) -> None:
        try:
            data = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
            self._validate_task_record_fields(data.get("tasks", []))
            self.workspaces = {
                item["id"]: Workspace(**self._normalize_workspace_item(item))
                for item in data.get("workspaces", [])
            }
            self.tasks = {
                item["id"]: WorkspaceTask(**self._normalize_task_item(item))
                for item in data.get("tasks", [])
            }
            self.sessions = {
                item["id"]: ManagedSession(**self._normalize_session_item(item))
                for item in data.get("sessions", [])
            }
            self.reports = {
                item["id"]: AgentReport(**self._normalize_report_item(item))
                for item in data.get("reports", [])
            }
            from ..task_graph import materialize_loaded_task_graph

            for workspace_id in self.workspaces:
                materialize_loaded_task_graph(self.tasks, workspace_id)
            self._save_state()
        except Exception as e:
            if isinstance(e, _UnknownTaskFieldsError) or (
                isinstance(e, ValidationError)
                and any(error["type"] == "extra_forbidden" for error in e.errors())
            ):
                raise
            logger.error(f"Failed to load legacy workspace state: {e}")
