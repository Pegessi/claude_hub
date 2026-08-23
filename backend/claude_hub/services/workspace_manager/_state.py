"""Construction, path helpers, and state loading."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..agent_tree import AgentTreeManager
from ..task_mailbox import TaskMailbox
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
        # Per-workspace mutation lock shared by report intake and Agent Tree
        # writes (spawn/send/followup/interrupt/ack). A report may snapshot
        # then restore the whole workspace; a concurrent tree persist in that
        # window would be durable until restore erased it. Workspace scope is
        # also the smallest simple lock that covers session/task/report and
        # Agent Tree event/cursor state written to the same state.json.
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
        # Re-entrancy for workspace_mutation_lock: create_report already holds
        # the lock when it ACKs mailbox state, and Agent Tree adapters may
        # nest start_task / persist under spawn/followup.
        self._workspace_mutation_held: ContextVar[str | None] = ContextVar(
            f"workspace_mutation_held_{id(self)}",
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
        self.task_mailbox = TaskMailbox(self)
        self._load_state()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return _wm.STATE_ROOT / workspace_id

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
        """Serialize report intake with Agent Tree workspace mutations.

        Report rollback restores a workspace snapshot. spawn / send /
        followup / interrupt / ack persist the same state.json; they wait
        on this lock so a concurrent tree write cannot land between
        snapshot and restore. Re-entrant for the owning coroutine.
        """
        if self._workspace_mutation_held.get() == workspace_id:
            yield
            return
        async with self._workspace_mutation_lock(workspace_id):
            token = self._workspace_mutation_held.set(workspace_id)
            try:
                yield
            finally:
                self._workspace_mutation_held.reset(token)

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
            legacy_resident_ack_by_workspace: dict[str, int] = {}
            missing_resident_ack_ids: set[str] = set()
            self.workspaces = {}
            for item in index.get("workspaces", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                workspace_id = str(item["id"])
                if "resident_ack_sequence" not in item:
                    missing_resident_ack_ids.add(workspace_id)
                else:
                    legacy_resident_ack_by_workspace[workspace_id] = int(
                        item.get("resident_ack_sequence", 0)
                    )
                self.workspaces[workspace_id] = Workspace(**self._normalize_workspace_item(item))
            for workspace_id in self.workspaces:
                state_file = self._workspace_state_file(workspace_id)
                if not state_file.exists():
                    continue
                data = json.loads(state_file.read_text(encoding="utf-8"))
                missing_parent_ids: set[str] = set()
                missing_ack_ids: set[str] = set()
                for item in data.get("tasks", []):
                    if isinstance(item, dict) and item.get("id"):
                        if "parent_task_id" not in item:
                            missing_parent_ids.add(str(item["id"]))
                        if "consumer_ack_sequence" not in item:
                            missing_ack_ids.add(str(item["id"]))
                    task = WorkspaceTask(**self._normalize_task_item(item))
                    self.tasks[task.id] = task
                for item in data.get("sessions", []):
                    session = ManagedSession(**self._normalize_session_item(item))
                    self.sessions[session.id] = session
                for item in data.get("reports", []):
                    report = AgentReport(**self._normalize_report_item(item))
                    self.reports[report.id] = report
                self.agent_tree.load_from_dict(workspace_id, data)
                from ..task_graph import materialize_loaded_task_graph
                from ..task_migration import migrate_pre_unification_graph

                self.workspaces[workspace_id] = migrate_pre_unification_graph(
                    tasks=self.tasks,
                    runs=self.agent_tree._runs,
                    workspace=self.workspaces[workspace_id],
                    missing_parent_ids=missing_parent_ids,
                    missing_ack_ids=missing_ack_ids,
                    missing_resident_ack=workspace_id in missing_resident_ack_ids,
                    legacy_resident_ack=legacy_resident_ack_by_workspace.get(workspace_id, 0),
                )
                materialize_loaded_task_graph(self.tasks, workspace_id)
                self.task_mailbox.load_from_dict(workspace_id, data)
        except ValueError:
            raise
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
            from ..task_graph import materialize_loaded_task_graph

            for workspace_id in self.workspaces:
                materialize_loaded_task_graph(self.tasks, workspace_id)
            self._save_state()
        except Exception as e:
            logger.error(f"Failed to load legacy workspace state: {e}")
