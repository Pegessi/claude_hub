"""Unified Agent Tree + Durable Mailbox/Event Stream coordination layer.

The ``AgentTreeManager`` owns:
- The agent run tree (parent/child relationships via ``path``).
- The append-only event stream (per workspace, monotonic ``sequence``).
- Per-run mailboxes (events addressed to a run, or authored within its
  subtree).
- call_id / correlation_id idempotency for all actions, scoped by workspace.

Actions:
- ``spawn``: create a child run, dispatch its initial task.
- ``send``: append a message to the recipient's mailbox (no turn wake).
- ``followup``: append a message AND resume the recipient's turn.
- ``wait``: block until directed events arrive (cursor-based).
- ``ack``: advance a run's acknowledged sequence cursor.
- ``interrupt``: stop a run, preserving context.
- ``list_runs``: return the tree with status and last_task_message.

The Hub owns lifecycle state. Agents report progress by emitting events
(via the executor adapter), not by mutating ``AgentRun.status`` directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

from claude_hub.models.agent_tree import (
    TERMINAL_EVENT_TYPES,
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
    ExecutorKind,
    FollowupRequest,
    InterruptRequest,
    ListRunsRequest,
    ManagedExecutorConfig,
    SendRequest,
    SpawnRequest,
    WaitRequest,
)

from .agent_tree_adapters import (
    ExecutorAdapter,
    ExternalJobAdapter,
    ManagedTaskAdapter,
    NativeSubagentAdapter,
    ResidentRootAdapter,
)
from .workspace_manager._constants import DeliveryUncertain

if TYPE_CHECKING:
    from claude_hub.services.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

# Maximum number of concurrent (non-terminal) child runs a parent may have.
MAX_CONCURRENT_CHILDREN = 32

# Terminal run statuses — no further lifecycle transitions are allowed.
_TERMINAL_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.INTERRUPTED,
    }
)


def _request_fingerprint(action: str, payload: Dict[str, Any]) -> str:
    """Compute a stable fingerprint of a full action request.

    The fingerprint covers the action name and every request field so that a
    reused ``call_id`` carrying a different payload is detected and rejected.
    The hash is deterministic (sorted keys, no whitespace) so the same
    request always produces the same fingerprint across processes.
    """
    canonical = json.dumps({"action": action, **payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentTreeManager:
    """Coordinates the agent tree, mailbox, and event stream."""

    def __init__(self, workspace_manager: "WorkspaceManager") -> None:
        self._wm = workspace_manager
        self._runs: Dict[str, AgentRun] = {}
        # Per-workspace append-only event log.
        self._events: Dict[str, List[AgentEvent]] = {}
        # Per-workspace next sequence number.
        self._next_seq: Dict[str, int] = {}
        # call_id idempotency index, scoped by workspace + action + target:
        #   _call_index[workspace_id][call_id] = {
        #       "action": str,
        #       "target": str,
        #       "fingerprint": str,   # full request fingerprint
        #       "event": AgentEvent,
        #   }
        # Reusing the same call_id for a different action, target, or request
        # payload is a caller bug and is rejected (ValueError).
        self._call_index: Dict[str, Dict[str, dict]] = {}
        # Per-run asyncio.Event for wait() wakeups.
        self._run_events: Dict[str, asyncio.Event] = {}
        # Per-run asyncio.Lock to make wait() race-free (no lost wakeups).
        self._run_locks: Dict[str, asyncio.Lock] = {}
        # Active subtree waiters. Ancestors are woken only when they opted
        # into subtree replay, preserving directed-wakeup behavior otherwise.
        self._subtree_waiters: Dict[str, int] = {}
        # Executor adapters keyed by ExecutorKind.
        self._adapters: Dict[ExecutorKind, ExecutorAdapter] = {
            ExecutorKind.MANAGED_TASK: ManagedTaskAdapter(workspace_manager),
            ExecutorKind.NATIVE_SUBAGENT: NativeSubagentAdapter(),
            ExecutorKind.EXTERNAL_JOB: ExternalJobAdapter(),
            ExecutorKind.RESIDENT_ROOT: ResidentRootAdapter(workspace_manager),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _adapter(self, kind: ExecutorKind) -> ExecutorAdapter:
        return self._adapters[kind]

    def require_executor_available(self, kind: ExecutorKind) -> None:
        """Reject executor kinds that do not have a real production runtime."""

        self._adapter(kind).require_available()

    def _next_sequence(self, workspace_id: str) -> int:
        seq = self._next_seq.get(workspace_id, 1)
        self._next_seq[workspace_id] = seq + 1
        return seq

    def _persist(self) -> None:
        """Persist the agent tree (and full workspace state) to disk.

        Called after every run/event/status mutation so the durable mailbox
        survives a process crash. Failures are re-raised so a persistence
        error fails the action closed (the in-memory state is not left
        uncommitted).
        """
        self._wm._save_state()

    def _validate_workspace(self, workspace_id: str) -> None:
        """Raise ValueError if the workspace does not exist."""
        if workspace_id not in self._wm.workspaces:
            raise ValueError(f"Workspace {workspace_id} not found")

    def _call_record(self, workspace_id: str, call_id: str) -> Optional[dict]:
        """Look up an existing call record by (workspace_id, call_id).

        Returns the full record dict (``{"action", "target", "fingerprint",
        "event"}``) or ``None``.
        """
        return self._call_index.get(workspace_id, {}).get(call_id)

    def _call_key(self, workspace_id: str, call_id: str) -> Optional[AgentEvent]:
        """Look up an existing event by (workspace_id, call_id)."""
        record = self._call_record(workspace_id, call_id)
        return record["event"] if record is not None else None

    def _record_call(
        self,
        workspace_id: str,
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
        event: AgentEvent,
    ) -> None:
        """Record a call_id -> {action, target, fingerprint, event} mapping.

        Scoped to the workspace. If the call_id was already used for a
        different action, target, or request fingerprint, raises ValueError.
        """
        existing = self._call_index.get(workspace_id, {}).get(call_id)
        if existing is not None:
            if (
                existing["action"] != action
                or existing["target"] != target
                or existing["fingerprint"] != fingerprint
            ):
                raise ValueError(
                    f"call_id {call_id!r} already used for action="
                    f"{existing['action']!r} target={existing['target']!r} "
                    f"in workspace {workspace_id}; cannot reuse for "
                    f"action={action!r} target={target!r}"
                )
            return
        self._call_index.setdefault(workspace_id, {})[call_id] = {
            "action": action,
            "target": target,
            "fingerprint": fingerprint,
            "event": event,
        }

    def _append_event(
        self,
        *,
        workspace_id: str,
        agent_run_id: str,
        event_type: AgentEventType,
        author: str,
        recipient: str,
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
        correlation_id: Optional[str] = None,
        payload: Optional[dict] = None,
        rollback_on_error: bool = True,
        persist: bool = True,
    ) -> Tuple[AgentEvent, bool]:
        """Append an event to the workspace stream.

        Idempotent on ``(workspace_id, call_id)``: if a call with the same
        id was already recorded in this workspace for the same ``action``,
        ``target``, and request ``fingerprint``, return the existing event
        and ``is_new=False``. Reusing the same call_id for a different
        action, target, or payload raises ``ValueError``.

        Returns ``(event, is_new)`` so callers can skip adapter side effects
        on duplicate calls.

        Rollback: if persistence fails after the in-memory append, the
        event is removed from the stream, the call record is dropped, and
        the sequence counter is decremented so the in-memory state matches
        the durable state.

        When ``rollback_on_error=False`` (used in the outcome phase after a
        side-effect has already been applied), a persistence failure is
        logged but the in-memory event is kept: the durable state will be
        reconciled by the next successful persist or on shutdown.

        When ``persist=False``, the event is appended in-memory but not
        persisted. The caller is responsible for calling ``_persist()``
        once after batching multiple mutations (intent/delivery/outcome
        protocol).
        """
        existing = self._call_record(workspace_id, call_id)
        if existing is not None:
            if (
                existing["action"] != action
                or existing["target"] != target
                or existing["fingerprint"] != fingerprint
            ):
                raise ValueError(
                    f"call_id {call_id!r} already used for action="
                    f"{existing['action']!r} target={existing['target']!r} "
                    f"in workspace {workspace_id}; cannot reuse for "
                    f"action={action!r} target={target!r}"
                )
            return existing["event"], False

        seq = self._next_sequence(workspace_id)
        event = AgentEvent(
            sequence=seq,
            call_id=call_id,
            correlation_id=correlation_id,
            agent_run_id=agent_run_id,
            type=event_type,
            author=author,
            recipient=recipient,
            action=action,
            target=target,
            fingerprint=fingerprint,
            payload=payload or {},
            created_at=datetime.utcnow(),
        )
        ws_events = self._events.setdefault(workspace_id, [])
        ws_events.append(event)
        self._record_call(workspace_id, call_id, action, target, fingerprint, event)

        if not persist:
            # Caller will persist as part of a batch.
            return event, True

        try:
            self._persist()
        except Exception:
            if rollback_on_error:
                # Rollback: undo the in-memory append so state matches disk.
                ws_events.pop()
                self._call_index.get(workspace_id, {}).pop(call_id, None)
                self._next_seq[workspace_id] = seq
            else:
                # Side-effect already applied; keep the in-memory event and
                # let the next successful persist reconcile durable state.
                logger.exception(
                    "Late persist failure after side-effect for event seq=%s "
                    "call_id=%s; keeping in-memory event for later flush",
                    seq,
                    call_id,
                )
            raise

        # Wake the named recipient and any active subtree waiters above it.
        self._wake_for_run(author, recipient)

        return event, True

    def _wake_for_run(self, author_run_id: str, recipient: str) -> None:
        """Wake the directed recipient and active ancestor subtree waiters."""
        ev = self._run_events.get(recipient)
        if ev is not None:
            ev.set()
        node = self._runs.get(author_run_id)
        while node is not None and node.parent_id is not None:
            node = self._runs.get(node.parent_id)
            if node is None:
                break
            if self._subtree_waiters.get(node.id, 0) > 0:
                ancestor_event = self._run_events.get(node.id)
                if ancestor_event is not None:
                    ancestor_event.set()

    def _wake_ancestors(self, run: AgentRun) -> None:
        """Wake waiters on the run and all its ancestors."""
        node: Optional[AgentRun] = run
        while node is not None:
            ev = self._run_events.get(node.id)
            if ev is not None:
                ev.set()
            node = self._runs.get(node.parent_id) if node.parent_id else None

    def _update_run_status(
        self,
        run_id: str,
        status: AgentRunStatus,
        rollback_on_error: bool = True,
        persist: bool = True,
    ) -> None:
        """Update a run's status with transition validation and rollback.

        Terminal runs (COMPLETED, FAILED, INTERRUPTED) may not transition
        to any other status, EXCEPT that INTERRUPTED and COMPLETED runs may
        transition back to RUNNING via ``followup`` (resume). FAILED runs
        are truly terminal and cannot be resumed.

        If persistence fails, the previous status is restored.

        When ``rollback_on_error=False`` (used in the outcome phase after a
        side-effect has already been applied), a persistence failure is
        logged but the new status is kept: the durable state will be
        reconciled by the next successful persist or on shutdown.

        When ``persist=False``, the status is updated in-memory but not
        persisted. The caller is responsible for calling ``_persist()``
        once after batching multiple mutations (intent/delivery/outcome
        protocol).
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        # Terminal guard: FAILED is truly terminal. INTERRUPTED and COMPLETED
        # may be resumed via followup (transition back to RUNNING).
        if run.status == AgentRunStatus.FAILED and run.status != status:
            logger.warning(
                "Refusing status transition %s -> %s for FAILED run %s",
                run.status,
                status,
                run_id,
            )
            return
        if run.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.INTERRUPTED,
        ) and status not in (AgentRunStatus.RUNNING, run.status):
            logger.warning(
                "Refusing status transition %s -> %s for terminal run %s "
                "(only RUNNING resume allowed)",
                run.status,
                status,
                run_id,
            )
            return
        old_status = run.status
        old_updated_at = run.updated_at
        run.status = status
        run.updated_at = datetime.utcnow()

        if not persist:
            return

        try:
            self._persist()
        except Exception:
            if rollback_on_error:
                run.status = old_status
                run.updated_at = old_updated_at
            else:
                logger.exception(
                    "Late persist failure after side-effect for run %s status "
                    "-> %s; keeping in-memory status for later flush",
                    run_id,
                    status,
                )
            raise

    def _set_last_message(self, run_id: str, message: str, persist: bool = True) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        old_msg = run.last_task_message
        old_updated_at = run.updated_at
        run.last_task_message = message
        run.updated_at = datetime.utcnow()

        if not persist:
            return

        try:
            self._persist()
        except Exception:
            run.last_task_message = old_msg
            run.updated_at = old_updated_at
            raise

    def _active_children(self, parent_id: str) -> List[AgentRun]:
        """Return non-terminal child runs of the given parent."""
        terminal = {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.INTERRUPTED,
        }
        return [
            r for r in self._runs.values() if r.parent_id == parent_id and r.status not in terminal
        ]

    def _recover_context_ref(self, run: AgentRun) -> None:
        """Recover a run's context_ref from its executor's side effects.

        If the process crashed after the adapter created the executor
        context (e.g. a workspace task) but before the run's context_ref
        was persisted, this method finds the existing context and links it
        back to the run. The run's status is advanced to RUNNING to match
        the executor's actual state.
        """
        if run.context_ref is not None:
            return
        if run.executor_kind == ExecutorKind.MANAGED_TASK:
            for task in self._wm.tasks.values():
                if (
                    task.workspace_id == run.workspace_id
                    and getattr(task, "agent_run_id", None) == run.id
                ):
                    run.context_ref = str(task.id)
                    run.updated_at = datetime.utcnow()
                    if run.status == AgentRunStatus.PENDING:
                        run.status = AgentRunStatus.RUNNING
                    try:
                        self._persist()
                    except Exception:
                        run.context_ref = None
                        run.updated_at = run.updated_at  # can't easily restore; leave as-is
                        raise
                    return

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def spawn(self, req: SpawnRequest) -> AgentRun:
        """Create a child run and dispatch its initial task.

        Idempotent on ``(workspace_id, call_id)``: a duplicate call returns
        the existing run without re-creating it or re-triggering the
        executor adapter.

        Durability contract:
        1. The run node is persisted before the adapter is invoked, so a
           crash mid-spawn leaves a recoverable PENDING run on disk.
        2. The adapter's ``spawn`` is idempotent: if a task already exists
           for this run (``agent_run_id``), it is reused.
        3. On recovery, ``_recover_pending_runs`` retries the adapter for
           any run that was persisted but never reached RUNNING.
        """
        self._validate_workspace(req.workspace_id)

        parent = self._runs.get(req.parent_id)
        if parent is None:
            raise KeyError(f"Parent run {req.parent_id} not found")
        if parent.workspace_id != req.workspace_id:
            raise ValueError("Parent run belongs to a different workspace")

        # Full request fingerprint so a reused call_id with a different
        # payload is rejected.
        fingerprint = _request_fingerprint(
            "spawn",
            {
                "workspace_id": req.workspace_id,
                "parent_id": req.parent_id,
                "executor_kind": req.executor_kind.value,
                "title": req.title,
                "initial_message": req.initial_message,
                "session_id": req.session_id,
                "context_ref": req.context_ref,
                "executor_config": (
                    req.executor_config.model_dump(mode="json")
                    if req.executor_config is not None
                    else None
                ),
            },
        )

        # Idempotency: if this call_id already produced a run in this
        # workspace, return it. If the run has no context_ref (e.g. the
        # process crashed after the adapter created the task but before
        # context_ref was persisted), recover it from the task that carries
        # this run's id as agent_run_id.
        existing_record = self._call_record(req.workspace_id, req.call_id)
        if existing_record is not None:
            if (
                existing_record["action"] != "spawn"
                or existing_record["target"] != parent.id
                or existing_record["fingerprint"] != fingerprint
            ):
                raise ValueError(
                    f"call_id {req.call_id!r} already used for action="
                    f"{existing_record['action']!r} target={existing_record['target']!r} "
                    f"in workspace {req.workspace_id}; cannot reuse for "
                    f"action='spawn' target={parent.id!r}"
                )
            existing_run = self._runs.get(existing_record["event"].agent_run_id)
            if existing_run is not None:
                if existing_run.context_ref is None:
                    self._recover_context_ref(existing_run)
                return existing_run

        # Parent/child concurrency limit: a supervisor may not have more
        # than MAX_CONCURRENT_CHILDREN active (non-terminal) children.
        active = self._active_children(parent.id)
        if len(active) >= MAX_CONCURRENT_CHILDREN:
            raise RuntimeError(
                f"Parent run {parent.id} has {len(active)} active children "
                f"(limit {MAX_CONCURRENT_CHILDREN}); interrupt or wait for "
                "completion before spawning more."
            )

        run_id = str(uuid.uuid4())
        adapter = self._adapter(req.executor_kind)
        run = AgentRun(
            id=run_id,
            workspace_id=req.workspace_id,
            parent_id=parent.id,
            # Path includes the run's own id so subtree prefix-matching
            # never mixes in siblings.
            path=f"{parent.path}/{run_id}",
            supervisor_id=parent.id,
            executor_kind=req.executor_kind,
            executor_config=req.executor_config,
            title=req.title,
            last_task_message=req.initial_message,
        )

        # Legacy resident-master calls identify an existing worker session
        # without repeating its CLI/model/target configuration. Recover the
        # exact persisted launch contract from that session before the run's
        # intent is committed. Explicit configs are validated later by the
        # managed adapter against the selected session.
        if (
            req.executor_kind == ExecutorKind.MANAGED_TASK
            and req.session_id
            and run.executor_config is None
        ):
            selected_session = self._wm.sessions.get(req.session_id)
            if selected_session is None:
                raise KeyError(f"Session {req.session_id} not found")
            run.executor_config = cast(ManagedTaskAdapter, adapter).config_from_session(
                selected_session
            )

        # Resolve defaults (including workspace target), validate the CLI/model
        # contract, and attach a durable capability snapshot before the first
        # persistence boundary.
        adapter.prepare_run(run)
        self._runs[run.id] = run
        # Emit a dispatched event addressed to the child. This is the
        # durable "intent" record: if the process crashes after this point
        # but before the adapter returns, recovery will retry the spawn.
        #
        # The run node and the DISPATCHED event (which carries the call_id)
        # are persisted as a SINGLE atomic unit. If they were persisted in
        # two separate steps, a crash between them would leave a run on disk
        # with no call_id record, so a retried spawn with the same call_id
        # would create a duplicate child.
        dispatched_event, _ = self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=run.id,
            event_type=AgentEventType.DISPATCHED,
            author=parent.id,
            recipient=run.id,
            call_id=req.call_id,
            action="spawn",
            target=parent.id,
            fingerprint=fingerprint,
            payload={"message": req.initial_message},
            persist=False,
        )
        try:
            self._persist()
        except Exception:
            # Rollback: remove the run and the intent event so in-memory
            # state matches disk (no partial durable state).
            del self._runs[run.id]
            ws_events = self._events.get(req.workspace_id, [])
            if ws_events and ws_events[-1] is dispatched_event:
                ws_events.pop()
            self._call_index.get(req.workspace_id, {}).pop(req.call_id, None)
            self._next_seq[req.workspace_id] = dispatched_event.sequence
            raise

        # Start the executor. The adapter call is in its own try/except so
        # that an adapter failure is distinguished from a late persist
        # failure (which must NOT mark the run FAILED — the side-effect
        # already applied).
        try:
            if req.executor_kind == ExecutorKind.MANAGED_TASK and req.session_id:
                context_ref = await self._spawn_managed_task(
                    run, req.initial_message, req.session_id
                )
            else:
                context_ref = await adapter.spawn(run, req.initial_message)
        except Exception as exc:
            logger.exception("spawn failed for run %s", run.id)
            # Adapter failed: no side-effect applied, so rollback_on_error
            # stays True (default) for the FAILED status + event.
            self._update_run_status(run.id, AgentRunStatus.FAILED)
            self._append_event(
                workspace_id=req.workspace_id,
                agent_run_id=run.id,
                event_type=AgentEventType.FAILED,
                author=run.id,
                recipient=parent.id,
                call_id=f"{req.call_id}:failed",
                action="spawn:failed",
                target=run.id,
                fingerprint=_request_fingerprint(
                    "spawn:failed", {"run_id": run.id, "error": str(exc)}
                ),
                payload={"error": str(exc)},
            )
            raise

        # Outcome phase: the side-effect (executor spawn) already
        # succeeded. Batch all in-memory mutations (context_ref,
        # RUNNING status, STARTED event) and persist them as a single
        # atomic unit. If the persist fails, the in-memory state is
        # kept (it matches the executor's actual state) and the next
        # successful persist will reconcile the durable state. We do
        # NOT mark the run FAILED here — the executor is running.
        run.context_ref = context_ref
        self._update_run_status(
            run.id, AgentRunStatus.RUNNING, rollback_on_error=False, persist=False
        )
        self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=run.id,
            event_type=AgentEventType.STARTED,
            author=run.id,
            recipient=parent.id,
            call_id=f"{req.call_id}:started",
            action="spawn:started",
            target=run.id,
            fingerprint=_request_fingerprint(
                "spawn:started",
                {"run_id": run.id, "context_ref": context_ref},
            ),
            payload={"context_ref": context_ref},
            rollback_on_error=False,
            persist=False,
        )
        try:
            self._persist()
        except Exception:
            logger.exception(
                "Late persist failure after spawn side-effect for run %s; "
                "keeping in-memory state (context_ref=%s, status=RUNNING) "
                "for later flush",
                run.id,
                context_ref,
            )
            raise

        # Wake waiters (the parent supervisor is listening for the STARTED
        # event) after the batched persist succeeds.
        self._wake_for_run(run.id, parent.id)

        return run

    async def _spawn_managed_task(
        self, run: AgentRun, initial_message: str, session_id: str
    ) -> str:
        """Spawn a managed task with an explicit target session.

        The base ``ManagedTaskAdapter.spawn`` does not accept a session id;
        this helper creates the task with ``session_id`` set so the
        dispatch targets a specific orchestrator worker (used by resident
        master mode).
        """
        from claude_hub.models.schemas import (
            StartTaskRequest,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
            WorkspaceTaskStatus,
        )

        existing_task = next(
            (
                t
                for t in self._wm.tasks.values()
                if t.workspace_id == run.workspace_id and t.agent_run_id == run.id
            ),
            None,
        )
        if existing_task is not None:
            if existing_task.status == WorkspaceTaskStatus.TODO:
                await self._wm.start_task(
                    existing_task.id,
                    StartTaskRequest(
                        agent_type=existing_task.agent_type,
                        target_session_id=existing_task.session_id,
                    ),
                )
            return str(existing_task.id)

        session = self._wm.sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        adapter = cast(ManagedTaskAdapter, self._adapter(ExecutorKind.MANAGED_TASK))
        adapter.validate_session(run, session)
        config = run.executor_config
        if config is None:
            raise RuntimeError("Managed executor config was not prepared")

        task = self._wm.create_task(
            run.workspace_id,
            WorkspaceTaskCreate(
                title=run.title or f"agent-run-{run.id[:8]}",
                prompt=initial_message,
                agent_type=config.agent_type,
                task_mode=WorkspaceTaskMode.REVIEWED,
                agent_run_id=run.id,
                session_id=session_id,
            ),
        )
        await self._wm.start_task(
            task.id,
            StartTaskRequest(
                agent_type=config.agent_type,
                target_session_id=session_id,
            ),
        )
        return str(task.id)

    def _validate_messaging_boundary(self, author: AgentRun, recipient: AgentRun) -> None:
        """Enforce subtree messaging boundaries.

        An author may send/followup only to:
        - Its supervisor (``recipient.id == author.supervisor_id``),
        - A run in its own subtree (``recipient.path`` starts with
          ``author.path``), or
        - Itself (``recipient.id == author.id``).

        Cross-subtree messaging (e.g. between siblings) is not allowed.
        """
        if recipient.id == author.id:
            return
        if recipient.id == author.supervisor_id:
            return
        if recipient.path.startswith(f"{author.path}/"):
            return
        raise ValueError(
            f"Run {author.id} may not message run {recipient.id}: "
            "recipient must be the author's supervisor, in the author's "
            "subtree, or the author itself"
        )

    async def send(self, req: SendRequest) -> AgentEvent:
        """Append a message to the recipient's mailbox without waking it."""
        self._validate_workspace(req.workspace_id)

        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")
        if recipient.workspace_id != req.workspace_id:
            raise ValueError("Recipient run belongs to a different workspace")

        author = self._runs.get(req.author_id)
        if author is None:
            raise KeyError(f"Author run {req.author_id} not found")
        if author.workspace_id != req.workspace_id:
            raise ValueError("Author run belongs to a different workspace")

        # Subtree boundary: an author may send only to its supervisor, a run
        # in its own subtree, or itself.
        self._validate_messaging_boundary(author, recipient)

        fingerprint = _request_fingerprint(
            "send",
            {
                "workspace_id": req.workspace_id,
                "recipient_id": req.recipient_id,
                "author_id": req.author_id,
                "message": req.message,
                "correlation_id": req.correlation_id,
            },
        )

        event, is_new = self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=recipient.id,
            event_type=AgentEventType.MESSAGE,
            author=req.author_id,
            recipient=recipient.id,
            call_id=req.call_id,
            action="send",
            target=recipient.id,
            fingerprint=fingerprint,
            correlation_id=req.correlation_id,
            payload={"message": req.message},
            persist=False,
        )
        if is_new:
            # Batch the MESSAGE event with last_task_message update and
            # persist as a single atomic unit. If persist fails, both are
            # rolled back to their previous values.
            old_last_message = recipient.last_task_message
            old_updated_at = recipient.updated_at
            self._set_last_message(recipient.id, req.message, persist=False)
            try:
                self._persist()
            except Exception:
                # Rollback: remove the event and restore last_task_message
                # to its previous value (NOT None — the old message must
                # survive a failed persist).
                ws_events = self._events.get(req.workspace_id, [])
                if ws_events and ws_events[-1] is event:
                    ws_events.pop()
                self._call_index.get(req.workspace_id, {}).pop(req.call_id, None)
                self._next_seq[req.workspace_id] = event.sequence
                recipient.last_task_message = old_last_message
                recipient.updated_at = old_updated_at
                raise
            # Wake the recipient (and its ancestors) after the batched
            # persist succeeds so supervisors observing the mailbox see
            # the new message.
            self._wake_for_run(recipient.id, recipient.id)
        return event

    async def followup(self, req: FollowupRequest) -> AgentEvent:
        """Append a message and resume the recipient's turn.

        Idempotent on ``(workspace_id, call_id)``: a duplicate call returns
        the existing message event without re-triggering the executor
        adapter.
        """
        self._validate_workspace(req.workspace_id)

        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")
        if recipient.workspace_id != req.workspace_id:
            raise ValueError("Recipient run belongs to a different workspace")

        author = self._runs.get(req.author_id)
        if author is None:
            raise KeyError(f"Author run {req.author_id} not found")
        if author.workspace_id != req.workspace_id:
            raise ValueError("Author run belongs to a different workspace")

        # Subtree boundary: an author may follow up only its supervisor, a
        # run in its own subtree, or itself. Cross-subtree messaging is not
        # allowed (siblings cannot directly wake each other).
        self._validate_messaging_boundary(author, recipient)

        fingerprint = _request_fingerprint(
            "followup",
            {
                "workspace_id": req.workspace_id,
                "recipient_id": req.recipient_id,
                "author_id": req.author_id,
                "message": req.message,
                "correlation_id": req.correlation_id,
            },
        )

        event, is_new = self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=recipient.id,
            event_type=AgentEventType.MESSAGE,
            author=req.author_id,
            recipient=recipient.id,
            call_id=req.call_id,
            action="followup",
            target=recipient.id,
            fingerprint=fingerprint,
            correlation_id=req.correlation_id,
            payload={"message": req.message, "followup": True},
            persist=False,
        )
        if not is_new:
            return event

        # Intent phase: the MESSAGE event is the durable intent record.
        # Batch it with last_task_message update and persist as a single
        # atomic unit. If persist fails, both are rolled back to their
        # previous values.
        old_last_message = recipient.last_task_message
        old_updated_at = recipient.updated_at
        self._set_last_message(recipient.id, req.message, persist=False)
        try:
            self._persist()
        except Exception:
            # Rollback: remove the event and restore last_task_message to
            # its previous value (NOT None — the old message must survive
            # a failed persist).
            ws_events = self._events.get(req.workspace_id, [])
            if ws_events and ws_events[-1] is event:
                ws_events.pop()
            self._call_index.get(req.workspace_id, {}).pop(req.call_id, None)
            self._next_seq[req.workspace_id] = event.sequence
            recipient.last_task_message = old_last_message
            recipient.updated_at = old_updated_at
            raise

        # Delivery: resume the executor's turn. The adapter call is in its
        # own try/except so that an adapter failure is distinguished from a
        # late persist failure (which must NOT mark the run FAILED).
        try:
            await self._adapter(recipient.executor_kind).followup(
                recipient, req.message, call_id=req.call_id
            )
        except DeliveryUncertain:
            # The delivery is ambiguous (tmux send failed in a way that
            # leaves the paste state unknown). This is NOT a terminal
            # failure: the operator can retry via retry_uncertain_delivery.
            # We MUST NOT mark the run FAILED or emit a FAILED event — that
            # would strand the run in a terminal state even though the
            # message may still reach the worker (or already has). The
            # MESSAGE intent event is already persisted, so the run's
            # lifecycle stays non-terminal and the outcome event will be
            # reconciled once the delivery settles (operator retry or
            # recovery replay). Re-raise so the caller surfaces the
            # uncertain state instead of a false success.
            logger.warning(
                "followup delivery uncertain for run %s call_id=%s; "
                "run stays non-terminal, operator retry required",
                recipient.id,
                req.call_id,
            )
            raise
        except Exception as exc:
            logger.exception("followup failed for run %s", recipient.id)
            # Adapter failed: no side-effect applied, so rollback_on_error
            # stays True (default) for the FAILED status + event.
            self._update_run_status(recipient.id, AgentRunStatus.FAILED)
            self._append_event(
                workspace_id=req.workspace_id,
                agent_run_id=recipient.id,
                event_type=AgentEventType.FAILED,
                author=recipient.id,
                recipient=req.author_id,
                call_id=f"{req.call_id}:failed",
                action="followup:failed",
                target=recipient.id,
                fingerprint=_request_fingerprint(
                    "followup:failed",
                    {"run_id": recipient.id, "error": str(exc)},
                ),
                payload={"error": str(exc)},
            )
            raise

        # Outcome phase: the side-effect succeeded. Set the in-memory status
        # to RUNNING first (persist=False) so that even if the subsequent
        # outcome-event persist fails, the in-memory projection matches the
        # executor's actual state. Then append the durable outcome event so
        # recovery can tell the followup was dispatched and does not replay
        # it. Both use rollback_on_error=False because the executor was
        # already resumed.
        #
        # NOTE: ``delivered`` is ``False`` here. The followup call_id was
        # dispatched to the worker's tmux inbox (it now sits in
        # ``processing_call_ids``), but the worker has not yet ACKed it.
        # The followup is only truly "delivered" once the worker processes
        # it and includes the call_id in ``acked_call_ids``. The
        # ``followup:outcome`` event records that the dispatch succeeded,
        # not that the worker received it.
        self._update_run_status(recipient.id, AgentRunStatus.RUNNING, persist=False)
        self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=recipient.id,
            event_type=AgentEventType.PROGRESS,
            author=recipient.id,
            recipient=req.author_id,
            call_id=f"{req.call_id}:outcome",
            action="followup:outcome",
            target=recipient.id,
            fingerprint=_request_fingerprint(
                "followup:outcome",
                {"run_id": recipient.id, "followup_call_id": req.call_id},
            ),
            payload={"delivered": False, "followup_call_id": req.call_id},
            rollback_on_error=False,
        )

        # Wake the recipient (and its ancestors) after the outcome persist
        # so the supervisor observing the mailbox sees the run is RUNNING
        # again.
        self._wake_for_run(recipient.id, recipient.id)

        return event

    async def wait(self, req: WaitRequest) -> List[AgentEvent]:
        """Block until directed events arrive or timeout.

        Returns events for ``recipient_id`` (or its subtree when
        ``subtree=True``) with sequence > ``since_sequence``.

        Race-free: a per-run lock guards the check-then-clear sequence so
        an event arriving between the check and the ``ev.clear()`` is not
        lost.
        """
        self._validate_workspace(req.workspace_id)

        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")
        if recipient.workspace_id != req.workspace_id:
            raise ValueError("Recipient run belongs to a different workspace")

        lock = self._run_locks.setdefault(req.recipient_id, asyncio.Lock())
        ev = self._run_events.setdefault(req.recipient_id, asyncio.Event())

        # Hub-enforced receiver cursor: the effective since_sequence is the
        # max of the caller's requested cursor and the run's persisted
        # ack_sequence. This guarantees that ACKed events are never re-delivered
        # (dedupe) while still allowing a caller that has processed events
        # beyond its ACK point to use its local cursor. On restart the
        # caller's local cursor is lost, so we fall back to ack_sequence
        # (at-least-once replay of unACKed events).
        effective_since = max(req.since_sequence, recipient.ack_sequence)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + req.timeout_seconds
        if req.subtree:
            self._subtree_waiters[req.recipient_id] = (
                self._subtree_waiters.get(req.recipient_id, 0) + 1
            )
        try:
            while True:
                async with lock:
                    events = self._events_for(
                        req.workspace_id, req.recipient_id, effective_since, req.subtree
                    )
                    if events:
                        return events
                    ev.clear()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(ev.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return []
        finally:
            if req.subtree:
                remaining_waiters = self._subtree_waiters.get(req.recipient_id, 1) - 1
                if remaining_waiters > 0:
                    self._subtree_waiters[req.recipient_id] = remaining_waiters
                else:
                    self._subtree_waiters.pop(req.recipient_id, None)

    def ack(
        self,
        workspace_id: str,
        run_id: str,
        sequence: int,
        *,
        persist: bool = True,
    ) -> AgentRun:
        """Advance a run's acknowledged sequence cursor.

        The cursor is persisted so a restarted supervisor can resume from
        where it left off. The cursor only moves forward and may not exceed
        the current maximum sequence number in the workspace.

        ACK is allowed on any run (including terminal ones) because it only
        records how far the supervisor has read the event stream; it does
        not change the run's lifecycle status.
        """
        self._validate_workspace(workspace_id)
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        if run.workspace_id != workspace_id:
            raise ValueError("Run belongs to a different workspace")

        max_seq = self._next_seq.get(workspace_id, 1) - 1
        if sequence > max_seq:
            raise ValueError(f"ACK sequence {sequence} exceeds workspace max sequence {max_seq}")
        if sequence < run.ack_sequence:
            raise ValueError(
                f"ACK sequence {sequence} is behind current ack cursor "
                f"{run.ack_sequence}; cursor only moves forward"
            )
        if sequence > run.ack_sequence:
            old_ack = run.ack_sequence
            old_updated_at = run.updated_at
            run.ack_sequence = sequence
            run.updated_at = datetime.utcnow()
            if persist:
                try:
                    self._persist()
                except Exception:
                    run.ack_sequence = old_ack
                    run.updated_at = old_updated_at
                    raise
        return run

    async def interrupt(self, req: InterruptRequest) -> AgentRun:
        """Interrupt a run, preserving its context.

        Idempotent on ``(workspace_id, call_id)``: a duplicate call returns
        the existing run without re-triggering the executor adapter.

        Only non-terminal runs may be interrupted. A run that is already
        COMPLETED, FAILED, or INTERRUPTED is returned unchanged.
        """
        self._validate_workspace(req.workspace_id)

        run = self._runs.get(req.run_id)
        if run is None:
            raise KeyError(f"Run {req.run_id} not found")
        if run.workspace_id != req.workspace_id:
            raise ValueError("Run belongs to a different workspace")

        fingerprint = _request_fingerprint(
            "interrupt",
            {
                "workspace_id": req.workspace_id,
                "run_id": req.run_id,
                "reason": req.reason,
            },
        )

        # Idempotency: if this call_id already interrupted in this workspace,
        # return the run. Reject call_id reuse for a different action, target,
        # or payload.
        existing_record = self._call_record(req.workspace_id, req.call_id)
        if existing_record is not None:
            if (
                existing_record["action"] != "interrupt"
                or existing_record["target"] != run.id
                or existing_record["fingerprint"] != fingerprint
            ):
                raise ValueError(
                    f"call_id {req.call_id!r} already used for action="
                    f"{existing_record['action']!r} target={existing_record['target']!r} "
                    f"in workspace {req.workspace_id}; cannot reuse for "
                    f"action='interrupt' target={run.id!r}"
                )
            return run

        # Terminal runs cannot be interrupted.
        if run.status in _TERMINAL_STATUSES:
            return run

        # Persist the INTERRUPTED intent event BEFORE the adapter call so a
        # crash mid-interrupt leaves a durable record that recovery can act
        # on. The event is the source of truth for the run's terminal state.
        self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=run.id,
            event_type=AgentEventType.INTERRUPTED,
            author=run.id,
            recipient=run.supervisor_id or run.id,
            call_id=req.call_id,
            action="interrupt",
            target=run.id,
            fingerprint=fingerprint,
            payload={"reason": req.reason},
        )

        # Apply the side-effect. If it fails, do NOT swallow: re-raise so the
        # caller knows the interrupt did not complete. The INTERRUPTED event
        # is already persisted, so recovery will retry the adapter call.
        await self._adapter(run.executor_kind).interrupt(run, req.reason)

        # Outcome phase: the side-effect succeeded. Mark the run INTERRUPTED
        # with rollback_on_error=False so a late save failure does not undo
        # the status that matches the executor's actual state.
        self._update_run_status(run.id, AgentRunStatus.INTERRUPTED, rollback_on_error=False)

        # Wake the supervisor after the outcome persist so it observes the
        # INTERRUPTED status.
        self._wake_for_run(run.id, run.supervisor_id or run.id)

        return run

    def list_runs(self, req: ListRunsRequest) -> List[AgentRun]:
        """List runs, optionally scoped to a subtree or status."""
        self._validate_workspace(req.workspace_id)
        runs = [r for r in self._runs.values() if r.workspace_id == req.workspace_id]
        if req.root_id is not None:
            root = self._runs.get(req.root_id)
            if root is None:
                return []
            prefix = f"{root.path}/"
            runs = [
                r
                for r in runs
                if r.id == root.id or r.path.startswith(prefix) or r.path == root.path
            ]
        if req.status is not None:
            runs = [r for r in runs if r.status == req.status]
        return sorted(runs, key=lambda r: r.created_at)

    def get_events(
        self,
        workspace_id: str,
        run_id: str,
        since_sequence: int = 0,
        subtree: bool = True,
    ) -> List[AgentEvent]:
        """Return events for a run after a cursor.

        The effective cursor is ``max(since_sequence, run.ack_sequence)`` so
        ACKed events are never re-delivered (Hub-enforced dedupe).
        """
        run = self._runs.get(run_id)
        effective_since = max(since_sequence, run.ack_sequence if run else 0)
        return self._events_for(workspace_id, run_id, effective_since, subtree)

    def _events_for(
        self,
        workspace_id: str,
        run_id: str,
        since_sequence: int,
        subtree: bool,
    ) -> List[AgentEvent]:
        run = self._runs.get(run_id)
        if run is None:
            return []
        all_events = self._events.get(workspace_id, [])
        descendant_ids = self._subtree_run_ids(run) - {run.id} if subtree else set()
        return [
            e
            for e in all_events
            if e.sequence > since_sequence and (e.recipient == run_id or e.author in descendant_ids)
        ]

    def _subtree_run_ids(self, root: AgentRun) -> set[str]:
        prefix = f"{root.path}/"
        ids = {root.id}
        for r in self._runs.values():
            if r.workspace_id != root.workspace_id:
                continue
            if r.id == root.id:
                continue
            if r.path.startswith(prefix) or r.path == root.path:
                ids.add(r.id)
        return ids

    # ------------------------------------------------------------------
    # Executor -> Hub event ingestion
    # ------------------------------------------------------------------

    def reconcile_followup_outcome(
        self,
        *,
        workspace_id: str,
        call_id: str,
        persist: bool = True,
        wake: bool = True,
    ) -> None:
        """Reconcile a followup's outcome event after its delivery settles.

        When :meth:`followup` raises :class:`DeliveryUncertain`, the
        ``followup:outcome`` event is intentionally NOT emitted (the
        dispatch outcome is unknown). Once the operator retries the
        uncertain delivery and the call_id lands in ``processing`` (or
        ``delivered``), the dispatch has effectively succeeded, so we
        emit the durable ``followup:outcome`` event and move the run to
        ``RUNNING``.

        Atomic + idempotent:

        * The run status is always (re)set to ``RUNNING`` in-memory
          (``persist=False``), even if the outcome event already exists.
          This closes the half-commit window where the outcome was
          persisted but the status update was lost.
        * If the outcome event (call_id ``f"{call_id}:outcome"``) does
          not yet exist, it is appended in-memory (``persist=False``).
          If it already exists, ``_append_event`` returns it unchanged
          (``is_new=False``) — no duplicate sequence/call_id.
        * A single ``_persist()`` atomically commits the outcome event
          (if new) and the RUNNING status together. On failure the
          in-memory state is retained so the same call_id can retry the
          persist; we re-raise so the caller sees the failure.
        * Only after the durable commit succeeds do we wake the
          recipient so supervisors observe the reconciled state.
        """
        record = self._call_record(workspace_id, call_id)
        if record is None:
            return
        if record.get("action") != "followup":
            return
        followup_event = record["event"]
        recipient_id = followup_event.agent_run_id
        author_id = followup_event.author
        outcome_call_id = f"{call_id}:outcome"

        run = self._runs.get(recipient_id)
        if run is not None:
            # Always reset to RUNNING in-memory, even if the outcome
            # event already exists. This recovers from the half-commit
            # window (outcome persisted, status lost) and resumes
            # COMPLETED/INTERRUPTED runs after a successful followup
            # delivery. _update_run_status's transition validator
            # refuses FAILED -> RUNNING (truly terminal) and allows
            # COMPLETED/INTERRUPTED -> RUNNING (resume).
            self._update_run_status(recipient_id, AgentRunStatus.RUNNING, persist=False)

        # Append the outcome event if it doesn't exist yet. If it does,
        # _append_event returns the existing event (is_new=False) and
        # does not advance the sequence or create a duplicate call_id.
        self._append_event(
            workspace_id=workspace_id,
            agent_run_id=recipient_id,
            event_type=AgentEventType.PROGRESS,
            author=recipient_id,
            recipient=author_id,
            call_id=outcome_call_id,
            action="followup:outcome",
            target=recipient_id,
            fingerprint=_request_fingerprint(
                "followup:outcome",
                {"run_id": recipient_id, "followup_call_id": call_id},
            ),
            payload={"delivered": False, "followup_call_id": call_id},
            persist=False,
        )

        # Single atomic persist for the outcome event (if new) and the
        # RUNNING status. On failure the in-memory state is kept so the
        # same call_id can retry; re-raise so the caller does not see a
        # false success.
        if persist:
            self._persist()

        # Wake only after the durable commit succeeds, or when the caller
        # explicitly owns the outer persistence transaction.
        if persist and wake:
            self._wake_for_run(recipient_id, author_id)

    def emit_event(
        self,
        *,
        workspace_id: str,
        agent_run_id: str,
        event_type: AgentEventType,
        author: str,
        recipient: str,
        call_id: str,
        payload: Optional[dict] = None,
    ) -> AgentEvent:
        """Ingest an event from an executor into the Hub stream.

        This is how managed-task reports, native-subagent progress, etc.
        flow back into the tree. The Hub updates the run's status and
        last_task_message from the event.

        All in-memory mutations (event append, status projection,
        last_task_message) are batched into a single ``_persist()`` call
        so the durable state is consistent.
        """
        fingerprint = _request_fingerprint(
            "emit",
            {
                "workspace_id": workspace_id,
                "agent_run_id": agent_run_id,
                "event_type": event_type.value,
                "author": author,
                "recipient": recipient,
                "payload": payload or {},
            },
        )
        event, is_new = self._append_event(
            workspace_id=workspace_id,
            agent_run_id=agent_run_id,
            event_type=event_type,
            author=author,
            recipient=recipient,
            call_id=call_id,
            action="emit",
            target=agent_run_id,
            fingerprint=fingerprint,
            payload=payload,
            persist=False,
        )
        if not is_new:
            # Duplicate call_id: the event already exists in memory. It may
            # not have been persisted yet (a previous _persist() failed).
            # Persist it now so the event survives a restart. If persist
            # fails, re-raise so the caller knows the event is NOT durable
            # and can retry — we must NOT return success for a non-durable
            # event. Only after a successful durable commit do we wake the
            # recipient so supervisors observe the event.
            self._persist()
            self._wake_for_run(agent_run_id, recipient)
            return event

        run = self._runs.get(agent_run_id)
        if run is not None:
            if event_type in TERMINAL_EVENT_TYPES:
                status_map = {
                    AgentEventType.COMPLETED: AgentRunStatus.COMPLETED,
                    AgentEventType.FAILED: AgentRunStatus.FAILED,
                    AgentEventType.INTERRUPTED: AgentRunStatus.INTERRUPTED,
                }
                self._update_run_status(agent_run_id, status_map[event_type], persist=False)
            elif event_type == AgentEventType.BLOCKED:
                self._update_run_status(agent_run_id, AgentRunStatus.BLOCKED, persist=False)
            elif event_type == AgentEventType.APPROVAL_REQUIRED:
                self._update_run_status(agent_run_id, AgentRunStatus.WAITING, persist=False)
            elif event_type == AgentEventType.TOOL_WAIT:
                self._update_run_status(agent_run_id, AgentRunStatus.WAITING, persist=False)
            else:
                # For non-terminal events, check the report_state in the
                # payload. READY_FOR_REVIEW and REVIEW_STARTED mean the run
                # is waiting for review to complete. REVIEW_FAILED means the
                # task was sent back to WORKING for revisions, so the run
                # should be RUNNING again.
                report_state = (payload or {}).get("report_state")
                if report_state in {"ready_for_review", "review_started", "completed"}:
                    # For REVIEWED tasks, the worker's COMPLETED report maps
                    # to a PROGRESS event (not terminal). The run is now
                    # waiting for the reviewer, so set status to WAITING.
                    self._update_run_status(agent_run_id, AgentRunStatus.WAITING, persist=False)
                elif report_state == "review_failed":
                    self._update_run_status(agent_run_id, AgentRunStatus.RUNNING, persist=False)

            msg = (payload or {}).get("message")
            if msg:
                self._set_last_message(agent_run_id, str(msg), persist=False)

        # Single atomic persist for the event + status + last_message. The
        # event represents the executor's actual state, so on persist failure
        # we keep the in-memory state (rollback_on_error=False semantics)
        # and re-raise so the caller can retry. The next successful persist
        # will reconcile the durable state.
        try:
            self._persist()
        except Exception:
            logger.exception(
                "emit_event: persist failed for event seq=%s call_id=%s; "
                "keeping in-memory event/status/message for retry",
                event.sequence,
                call_id,
            )
            raise

        # Wake the recipient (and its ancestors) after the batched persist
        # so supervisors observing the mailbox see the new event.
        self._wake_for_run(agent_run_id, recipient)

        return event

    def get_run(self, run_id: str) -> Optional[AgentRun]:
        return self._runs.get(run_id)

    def get_run_by_context_ref(self, workspace_id: str, context_ref: str) -> Optional[AgentRun]:
        """Find a run by its executor context reference (e.g. task id)."""
        for run in self._runs.values():
            if run.workspace_id == workspace_id and run.context_ref == context_ref:
                return run
        return None

    def create_root_run(
        self,
        *,
        workspace_id: str,
        executor_kind: ExecutorKind,
        title: Optional[str] = None,
        context_ref: Optional[str] = None,
    ) -> AgentRun:
        """Create a root run (no parent). Used to bootstrap the resident."""
        self._validate_workspace(workspace_id)
        run = AgentRun(
            workspace_id=workspace_id,
            parent_id=None,
            path="",
            supervisor_id=None,
            executor_kind=executor_kind,
            title=title,
            context_ref=context_ref,
            status=AgentRunStatus.RUNNING,
        )
        # Root path is just its own id.
        run.path = run.id
        # Root runs are returned by the same discovery APIs as children; attach
        # the durable capability contract immediately instead of waiting for a
        # cold-reload backfill.
        self._adapter(executor_kind).prepare_run(run)
        self._runs[run.id] = run
        try:
            self._persist()
        except Exception:
            del self._runs[run.id]
            raise
        return run

    # ------------------------------------------------------------------
    # Persistence (durable mailbox / event stream)
    # ------------------------------------------------------------------

    def to_dict(self, workspace_id: str) -> dict:
        """Serialize runs and events for a workspace to a JSON-safe dict."""
        runs = [
            r.model_dump(mode="json") for r in self._runs.values() if r.workspace_id == workspace_id
        ]
        events = [e.model_dump(mode="json") for e in self._events.get(workspace_id, [])]
        return {"agent_runs": runs, "agent_events": events}

    def load_from_dict(self, workspace_id: str, data: dict) -> None:
        """Load runs and events for a workspace from a persisted dict.

        Rebuilds the call_id index (scoped by workspace) and the next
        sequence counter from the loaded events so idempotency and
        monotonic sequencing survive a restart.

        The ``fingerprint`` field is persisted on each event, so a restarted
        process can still detect a call_id reused with a different request
        body.
        """
        for item in data.get("agent_runs", []):
            run = AgentRun(**item)
            self._runs[run.id] = run

        events: list[AgentEvent] = []
        for item in data.get("agent_events", []):
            # Migration: events persisted before the mandatory-recipient
            # change may have ``recipient=None``. Self-address them
            # (``recipient = author``) so the directed mailbox filter
            # (``e.recipient == run_id``) still delivers them. For root
            # runs this is correct (they self-address); for child runs
            # the author is the child run id, which is wrong, but
            # pre-mandatory-recipient events were only ever emitted by
            # root runs (child delegation came with the directed mailbox).
            if item.get("recipient") is None:
                item = dict(item)
                migrated_recipient = item.get("author") or item.get("agent_run_id", "")
                item["recipient"] = migrated_recipient
                logger.info(
                    "Migrated event sequence=%s call_id=%s: " "set recipient=%s (was null)",
                    item.get("sequence"),
                    item.get("call_id"),
                    migrated_recipient,
                )
            events.append(AgentEvent(**item))
        events.sort(key=lambda e: e.sequence)
        self._events[workspace_id] = events

        max_seq = 0
        ws_calls: Dict[str, dict] = {}
        for event in events:
            action = event.action or "emit"
            target = event.target or event.agent_run_id
            # Use the persisted fingerprint if available; otherwise fall back
            # to a stable hash derived from the event's identity so call_id
            # reuse with a different action/target is still rejected.
            fingerprint = event.fingerprint or _request_fingerprint(
                action,
                {
                    "sequence": event.sequence,
                    "call_id": event.call_id,
                    "agent_run_id": event.agent_run_id,
                    "event_type": event.type.value,
                    "author": event.author,
                    "recipient": event.recipient,
                },
            )
            ws_calls[event.call_id] = {
                "action": action,
                "target": target,
                "fingerprint": fingerprint,
                "event": event,
            }
            if event.sequence > max_seq:
                max_seq = event.sequence
        self._call_index[workspace_id] = ws_calls
        self._next_seq[workspace_id] = max_seq + 1

        # Migration: historical root runs may have been persisted as
        # ``managed_task`` before the ``resident_root`` executor kind existed.
        # A root run (parent_id is None) that is ``managed_task`` represents
        # the resident agent itself; convert it to ``resident_root`` and link
        # it to the workspace's resident session so authority checks work.
        #
        # Also, old resident root runs may have been persisted without a
        # context_ref (the resident session id); link them to the workspace's
        # resident session.
        from claude_hub.models.agent_tree import ExecutorKind

        workspace = self._wm.workspaces.get(workspace_id)
        resident_session_id = (
            getattr(workspace, "resident_agent_session_id", None) if workspace else None
        )
        for run in self._runs.values():
            if run.workspace_id != workspace_id:
                continue
            if run.parent_id is not None:
                continue
            # Historical managed_task root -> resident_root.
            if run.executor_kind == ExecutorKind.MANAGED_TASK:
                run.executor_kind = ExecutorKind.RESIDENT_ROOT
                run.updated_at = datetime.utcnow()
                logger.info(
                    "Migrated root run %s from managed_task to resident_root",
                    run.id,
                )
            # Resident root without context_ref -> link to resident session.
            if (
                run.executor_kind == ExecutorKind.RESIDENT_ROOT
                and run.context_ref is None
                and resident_session_id is not None
            ):
                run.context_ref = resident_session_id
                run.updated_at = datetime.utcnow()
                logger.info(
                    "Migrated resident root run %s: set context_ref=%s",
                    run.id,
                    resident_session_id,
                )

        # Backfill the durable executor contract for runs written before
        # executor_config/capability snapshots existed. Managed runs recover
        # the real task session when possible, so a historical Codex/Cursor or
        # remote child is never silently relabelled as local Claude. Stub
        # native/external runs receive an explicit unavailable capability
        # snapshot rather than being advertised as executable.
        for run in self._runs.values():
            if run.workspace_id != workspace_id:
                continue
            adapter = self._adapter(run.executor_kind)
            needs_prepare = run.executor_capabilities is None or (
                run.executor_kind == ExecutorKind.MANAGED_TASK and run.executor_config is None
            )
            if run.executor_kind == ExecutorKind.MANAGED_TASK and run.executor_config is None:
                task = self._wm.tasks.get(run.context_ref or "")
                session = (
                    self._wm.sessions.get(task.session_id) if task and task.session_id else None
                )
                if session is not None:
                    run.executor_config = cast(ManagedTaskAdapter, adapter).config_from_session(
                        session
                    )
                elif task is not None:
                    run.executor_config = ManagedExecutorConfig(agent_type=task.agent_type)
            if needs_prepare:
                adapter.prepare_run(run)

    async def recover_pending_runs(self, workspace_id: str) -> None:
        """Recover runs that were persisted but never reached a consistent state.

        After a crash, a run may be in an inconsistent state:

        1. **PENDING with no context_ref**: the adapter spawn was lost. Retry
           the spawn (the adapter is idempotent and reuses any existing
           executor context keyed by ``agent_run_id``).

        2. **PENDING with context_ref**: the spawn succeeded but the status
           update was lost. Advance to RUNNING.

        3. **Non-terminal with an INTERRUPTED event**: the interrupt intent
           was persisted but the adapter call or status update was lost.
           Retry the adapter interrupt (idempotent) and set status to
           INTERRUPTED.

        4. **Non-terminal with a followup MESSAGE event but not RUNNING**:
           the followup intent was persisted but the adapter call or status
           update was lost. Retry the adapter followup (idempotent) and set
           status to RUNNING.

        Runs that are already RUNNING, WAITING, BLOCKED, or consistently
        terminal are left untouched.
        """
        # Reconcile fail-closed delivery states first. The session/task state
        # may have persisted while the supervisor event append failed; the
        # deterministic call_id makes this cold-start compensation idempotent.
        for session in list(self._wm.sessions.values()):
            if session.workspace_id != workspace_id:
                continue
            for call_id in session.uncertain_call_ids:
                self._wm._emit_delivery_uncertain(workspace_id, session.id, call_id)

        # Reconcile persisted reports into agent tree events. A report may
        # have been persisted to the workspace state while its corresponding
        # agent tree event was not (crash between the two persists).
        # emit_event is idempotent on call_id=f"report:{report.id}", so
        # re-emitting is safe.
        #
        # The mapping MUST be reviewed-aware: for REVIEWED tasks, the
        # worker's COMPLETED report does NOT terminate the run — it maps to
        # PROGRESS (the run waits for the reviewer). Only REVIEW_PASSED
        # emits the terminal COMPLETED event. This mirrors
        # _bridge_report_to_agent_event in _reports.py.
        from ..models.agent_tree import ExecutorKind
        from ..models.schemas import AgentReportState, WorkspaceTaskMode

        for run in list(self._runs.values()):
            if run.workspace_id != workspace_id:
                continue
            if run.executor_kind != ExecutorKind.MANAGED_TASK:
                continue
            task_id = run.context_ref
            if not task_id:
                continue
            task = self._wm.tasks.get(task_id)
            is_reviewed = task is not None and task.task_mode == WorkspaceTaskMode.REVIEWED

            report_state_map = {
                AgentReportState.STARTED: AgentEventType.STARTED,
                AgentReportState.WORKING: AgentEventType.PROGRESS,
                AgentReportState.BLOCKED: AgentEventType.BLOCKED,
                AgentReportState.NEEDS_INPUT: AgentEventType.APPROVAL_REQUIRED,
                AgentReportState.READY_FOR_REVIEW: AgentEventType.PROGRESS,
                # REVIEWED: worker COMPLETED -> PROGRESS (wait for reviewer).
                # DIRECT: worker COMPLETED -> COMPLETED (terminal).
                AgentReportState.COMPLETED: (
                    AgentEventType.PROGRESS if is_reviewed else AgentEventType.COMPLETED
                ),
                AgentReportState.REVIEW_STARTED: AgentEventType.PROGRESS,
                AgentReportState.REVIEW_PASSED: AgentEventType.COMPLETED,
                AgentReportState.REVIEW_FAILED: AgentEventType.PROGRESS,
                AgentReportState.REVIEW_NEEDS_INPUT: AgentEventType.BLOCKED,
            }
            for report in self._wm.reports.values():
                if report.workspace_id != workspace_id:
                    continue
                if report.task_id != task_id:
                    continue
                event_type = report_state_map.get(report.state, AgentEventType.PROGRESS)
                try:
                    self.emit_event(
                        workspace_id=workspace_id,
                        agent_run_id=run.id,
                        event_type=event_type,
                        author=run.id,
                        recipient=run.supervisor_id or run.id,
                        call_id=f"report:{report.id}",
                        payload={
                            "message": report.message,
                            "report_id": report.id,
                            "report_state": report.state.value,
                            "task_id": report.task_id,
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to reconcile report %s to event for run %s",
                        report.id,
                        run.id,
                    )

        for run in list(self._runs.values()):
            if run.workspace_id != workspace_id:
                continue

            run_events = sorted(
                [e for e in self._events.get(workspace_id, []) if e.agent_run_id == run.id],
                key=lambda e: e.sequence,
            )

            # Determine the intended state from the latest event. The latest
            # event is the source of truth for what the last action was.
            latest_event = run_events[-1] if run_events else None

            # Case 3: the latest event is INTERRUPTED — the interrupt intent
            # was persisted but the adapter call or status update was lost.
            # Retry the adapter interrupt (idempotent) and set status to
            # INTERRUPTED only after the adapter call succeeds.
            if (
                latest_event is not None
                and latest_event.type == AgentEventType.INTERRUPTED
                and run.status != AgentRunStatus.INTERRUPTED
            ):
                try:
                    await self._adapter(run.executor_kind).interrupt(run, None)
                    self._update_run_status(
                        run.id, AgentRunStatus.INTERRUPTED, rollback_on_error=False
                    )
                except Exception:
                    logger.exception("Failed to recover interrupt for run %s", run.id)
                continue

            # Case 4: recover every followup MESSAGE event that lacks a
            # matching outcome event (call_id:outcome). A crash can leave
            # multiple followup intents persisted without their outcomes;
            # we must replay ALL of them in sequence order, not just the
            # latest. The adapter is idempotent on call_id
            # (delivered_call_ids on the task), so replaying a partially
            # delivered followup is a no-op.
            if run.status != AgentRunStatus.FAILED:
                followup_events = [
                    e
                    for e in run_events
                    if e.type == AgentEventType.MESSAGE
                    and (e.payload or {}).get("followup") is True
                ]
                for followup_event in followup_events:
                    followup_call_id = followup_event.call_id
                    outcome_call_id = f"{followup_call_id}:outcome"
                    has_outcome = any(e.call_id == outcome_call_id for e in run_events)
                    if has_outcome:
                        continue
                    try:
                        # Replay this followup intent's OWN payload, not the
                        # run's last_task_message. Each followup event
                        # carries the message that was originally sent;
                        # replaying the wrong message would corrupt the
                        # agent's context.
                        last_msg = (followup_event.payload or {}).get("message", "")
                        await self._adapter(run.executor_kind).followup(
                            run, last_msg, call_id=followup_call_id
                        )
                        # Append the outcome event so a subsequent recovery
                        # does not replay this followup. ``delivered`` is
                        # ``False``: the followup was re-dispatched to the
                        # worker's tmux inbox but the worker has not yet
                        # ACKed it. See the note in ``followup``.
                        self._append_event(
                            workspace_id=run.workspace_id,
                            agent_run_id=run.id,
                            event_type=AgentEventType.PROGRESS,
                            author=run.id,
                            recipient=run.supervisor_id or run.id,
                            call_id=outcome_call_id,
                            action="followup:outcome",
                            target=run.id,
                            fingerprint=_request_fingerprint(
                                "followup:outcome",
                                {"run_id": run.id, "followup_call_id": followup_call_id},
                            ),
                            payload={"delivered": False, "followup_call_id": followup_call_id},
                            rollback_on_error=False,
                        )
                        self._update_run_status(
                            run.id, AgentRunStatus.RUNNING, rollback_on_error=False
                        )
                    except Exception:
                        logger.exception(
                            "Failed to recover followup call_id=%s for run %s",
                            followup_call_id,
                            run.id,
                        )

            # Reconcile non-terminal, non-PENDING runs with the executor's
            # actual status. The in-memory status may be stale after a crash
            # (e.g. a managed task moved to REVIEW while the run was still
            # RUNNING). Use the adapter's get_status() as the source of truth.
            if run.status not in (
                AgentRunStatus.PENDING,
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.INTERRUPTED,
            ):
                try:
                    actual = self._adapter(run.executor_kind).get_status(run)
                    if actual != run.status:
                        self._update_run_status(run.id, actual, rollback_on_error=False)
                except Exception:
                    logger.exception("Failed to reconcile status for run %s via get_status", run.id)
                continue

            if run.status != AgentRunStatus.PENDING:
                continue

            if run.context_ref is not None:
                # Already has a context but still PENDING — should be RUNNING.
                self._update_run_status(run.id, AgentRunStatus.RUNNING)
                continue

            # No context_ref and PENDING: the adapter spawn was lost.
            # Retry it. The adapter will find any existing task with
            # agent_run_id == run.id and reuse it.
            try:
                context_ref = await self._adapter(run.executor_kind).spawn(
                    run, run.last_task_message or ""
                )
                run.context_ref = context_ref
                self._update_run_status(run.id, AgentRunStatus.RUNNING)
                logger.info("Recovered pending run %s with context_ref=%s", run.id, context_ref)
            except Exception:
                logger.exception("Failed to recover pending run %s", run.id)
                self._update_run_status(run.id, AgentRunStatus.FAILED)

        # ------------------------------------------------------------------
        # Durable receiver pump on cold recovery.
        #
        # After a crash, call_ids may be in four states:
        #
        #   - ``pending_call_ids``: persisted but not yet sent to tmux.
        #     The pump claims them (``pending → processing``) BEFORE the
        #     tmux send (persist-intent-before-side-effect) and sends them
        #     to tmux, gated by the tmux receipt
        #     (``@receipt_<sha16(call_id)>``) for at-most-once paste per
        #     call_id per tmux session lifetime.
        #
        #   - ``processing_call_ids``: sent to tmux, awaiting the worker's
        #     ACK. On cold recovery the monitor reconciles these against
        #     the tmux receipt: receipt present on a LIVE session → keep
        #     ``processing`` (no repaste); receipt absent on a LIVE
        #     session → move back to ``pending`` for one re-delivery;
        #     session gone/STOPPED/unqueryable → move to ``uncertain``
        #     (fail closed).
        #
        #   - ``uncertain_call_ids``: ambiguous tmux send failure. The Hub
        #     does NOT auto-resend; an explicit operator retry via
        #     ``retry_uncertain_delivery`` is required.
        #
        #   - ``delivered_call_ids``: ACKed by the worker. NEVER re-deliver.
        #
        # The worker ACK (``acked_call_ids`` → ``delivered_call_ids``) is
        # the durable commit. The tmux receipt proves the paste ran in the
        # current tmux session lifetime; it does NOT guarantee exactly-once
        # across session recreation.
        # ------------------------------------------------------------------
        for session in list(self._wm.sessions.values()):
            if session.workspace_id != workspace_id:
                continue
            # Only pump pending call_ids. processing_call_ids are
            # reconciled by the monitor against the tmux receipt; do NOT
            # blindly re-deliver them.
            if not session.pending_call_ids:
                continue
            try:
                await self._wm._pump_session_messages(session.id)
            except Exception:
                logger.exception(
                    "Failed to pump pending messages for session %s during recovery",
                    session.id,
                )
