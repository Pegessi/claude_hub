"""Construction, path helpers, and state loading."""

from contextvars import ContextVar

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..agent_tree import AgentTreeManager
from ._constants import *  # noqa: F401,F403


class _StateMixin:
    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.tasks: dict[str, WorkspaceTask] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.reports: dict[str, AgentReport] = {}
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        self._feedback_summary_locks: dict[str, asyncio.Lock] = {}
        # Per-session pump locks: serialize _pump_session_messages so two
        # concurrent pump cycles cannot both send the same pending call_id
        # to tmux (which would duplicate the model turn).
        self._pump_locks: dict[str, asyncio.Lock] = {}
        # Per-workspace report-intake locks.  A report mutates more than its
        # own call-id index: it replaces the owning ManagedSession, may mutate
        # the shared task (worker and reviewer use different sessions), ACKs
        # durable mailbox state, and can append Agent Tree events/cursors.
        # Consequently a per-(session, call_id) lock is too narrow: two
        # different call_ids can derive replacements from the same stale
        # session/task snapshot and lose one report_call_ids entry.  The state
        # file is also written per workspace, so workspace scope is the
        # smallest simple lock that covers every object in the transaction.
        self._report_intake_locks: dict[str, asyncio.Lock] = {}
        # Ephemeral commit markers used only to distinguish a pre-commit
        # exception (restore the full report-intake snapshot) from a
        # post-commit side-effect exception (leave durable state intact so an
        # idempotent retry can finish the side effects).
        self._report_intake_committed: set[str] = set()
        # Keep report-intake persistence routing task-local. Reports for two
        # different workspaces may run concurrently; a plain manager attribute
        # could make one coroutine persist the other's workspace.
        self._report_intake_workspace: ContextVar[str | None] = ContextVar(
            f"report_intake_workspace_{id(self)}",
            default=None,
        )
        self._monitor_task: asyncio.Task[None] | None = None
        # Cache of resolved git worktree roots per workspace id: (timestamp, roots).
        # Used by artifact preview to resolve markdown produced inside a worktree.
        self._worktree_root_cache: dict[str, tuple[float, list[Path]]] = {}
        # Unified agent tree + durable mailbox coordination layer. Owns the
        # parent/child run tree, the append-only event stream, and call_id
        # idempotency. Managed tasks are bridged into this layer via
        # context_ref (task id) so reports surface as agent events.
        self.agent_tree = AgentTreeManager(self)  # type: ignore[arg-type]
        self._load_state()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return _wm.STATE_ROOT / workspace_id

    def _workspace_state_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "state.json"

    def _workspace_task_records_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "task_records"

    def _workspace_attachments_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "attachments"

    def _feedback_store(self) -> FeedbackLessonStore:
        return FeedbackLessonStore(_wm.STATE_ROOT)

    def snapshot_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "snapshot.md"

    def _load_state(self) -> None:
        if INDEX_FILE.exists():
            self._load_nested_state()
        elif LEGACY_STATE_FILE.exists():
            self._load_legacy_state()

        for session_id, session in list(self.sessions.items()):
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

    def _load_nested_state(self) -> None:
        try:
            index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            self.workspaces = {
                item["id"]: Workspace(**self._normalize_workspace_item(item))
                for item in index.get("workspaces", [])
            }
            for workspace_id in self.workspaces:
                state_file = self._workspace_state_file(workspace_id)
                if not state_file.exists():
                    continue
                data = json.loads(state_file.read_text(encoding="utf-8"))
                for item in data.get("tasks", []):
                    task = WorkspaceTask(**self._normalize_task_item(item))
                    self.tasks[task.id] = task
                for item in data.get("sessions", []):
                    session = ManagedSession(**self._normalize_session_item(item))
                    self.sessions[session.id] = session
                for item in data.get("reports", []):
                    report = AgentReport(**self._normalize_report_item(item))
                    self.reports[report.id] = report
                # Load the agent tree (runs + event stream) for this workspace.
                self.agent_tree.load_from_dict(workspace_id, data)
        except Exception as e:
            logger.error(f"Failed to load nested workspace state: {e}")

    def _load_legacy_state(self) -> None:
        try:
            data = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
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
            self._save_state()
        except Exception as e:
            logger.error(f"Failed to load legacy workspace state: {e}")
