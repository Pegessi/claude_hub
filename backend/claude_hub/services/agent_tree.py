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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

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
    SendRequest,
    SpawnRequest,
    WaitRequest,
)

from .agent_tree_adapters import (
    ExecutorAdapter,
    ExternalJobAdapter,
    ManagedTaskAdapter,
    NativeSubagentAdapter,
)

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
        # Executor adapters keyed by ExecutorKind.
        self._adapters: Dict[ExecutorKind, ExecutorAdapter] = {
            ExecutorKind.MANAGED_TASK: ManagedTaskAdapter(workspace_manager),
            ExecutorKind.NATIVE_SUBAGENT: NativeSubagentAdapter(),
            ExecutorKind.EXTERNAL_JOB: ExternalJobAdapter(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _adapter(self, kind: ExecutorKind) -> ExecutorAdapter:
        return self._adapters[kind]

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
        recipient: Optional[str],
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
        correlation_id: Optional[str] = None,
        payload: Optional[dict] = None,
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

        try:
            self._persist()
        except Exception:
            # Rollback: undo the in-memory append so state matches disk.
            ws_events.pop()
            self._call_index.get(workspace_id, {}).pop(call_id, None)
            self._next_seq[workspace_id] = seq
            raise

        # Wake any waiters on the recipient run (and its ancestors, since
        # supervisors listen to their subtree).
        run = self._runs.get(agent_run_id)
        if run is not None:
            self._wake_ancestors(run)
        if recipient:
            ev = self._run_events.get(recipient)
            if ev is not None:
                ev.set()

        return event, True

    def _wake_ancestors(self, run: AgentRun) -> None:
        """Wake waiters on the run and all its ancestors."""
        node: Optional[AgentRun] = run
        while node is not None:
            ev = self._run_events.get(node.id)
            if ev is not None:
                ev.set()
            node = self._runs.get(node.parent_id) if node.parent_id else None

    def _update_run_status(self, run_id: str, status: AgentRunStatus) -> None:
        """Update a run's status with transition validation and rollback.

        Terminal runs (COMPLETED, FAILED, INTERRUPTED) may not transition
        to any other status. If persistence fails, the previous status is
        restored.
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        if run.status in _TERMINAL_STATUSES and run.status != status:
            logger.warning(
                "Refusing status transition %s -> %s for terminal run %s",
                run.status,
                status,
                run_id,
            )
            return
        old_status = run.status
        old_updated_at = run.updated_at
        run.status = status
        run.updated_at = datetime.utcnow()
        try:
            self._persist()
        except Exception:
            run.status = old_status
            run.updated_at = old_updated_at
            raise

    def _set_last_message(self, run_id: str, message: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        old_msg = run.last_task_message
        old_updated_at = run.updated_at
        run.last_task_message = message
        run.updated_at = datetime.utcnow()
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
        run = AgentRun(
            id=run_id,
            workspace_id=req.workspace_id,
            parent_id=parent.id,
            # Path includes the run's own id so subtree prefix-matching
            # never mixes in siblings.
            path=f"{parent.path}/{run_id}",
            supervisor_id=parent.id,
            executor_kind=req.executor_kind,
            title=req.title,
            last_task_message=req.initial_message,
        )
        self._runs[run.id] = run
        try:
            self._persist()
        except Exception:
            # Rollback: remove the run so in-memory state matches disk.
            del self._runs[run.id]
            raise

        # Emit a dispatched event addressed to the child. This is the
        # durable "intent" record: if the process crashes after this point
        # but before the adapter returns, recovery will retry the spawn.
        self._append_event(
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
        )

        # Start the executor.
        try:
            adapter = self._adapter(req.executor_kind)
            if req.executor_kind == ExecutorKind.MANAGED_TASK and req.session_id:
                # Managed-task adapter accepts a target session hint via
                # the run's context_ref pre-set? No — pass it through the
                # adapter's spawn by stashing it on the run temporarily.
                context_ref = await self._spawn_managed_task(
                    run, req.initial_message, req.session_id
                )
            else:
                context_ref = await adapter.spawn(run, req.initial_message)
            run.context_ref = context_ref
            self._update_run_status(run.id, AgentRunStatus.RUNNING)
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
            )
        except Exception as exc:
            logger.exception("spawn failed for run %s", run.id)
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
            AgentType,
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
                await self._wm.start_task(existing_task.id)
            return str(existing_task.id)

        task = self._wm.create_task(
            run.workspace_id,
            WorkspaceTaskCreate(
                title=run.title or f"agent-run-{run.id[:8]}",
                prompt=initial_message,
                agent_type=AgentType.CLAUDE,
                task_mode=WorkspaceTaskMode.REVIEWED,
                agent_run_id=run.id,
                session_id=session_id,
            ),
        )
        await self._wm.start_task(task.id)
        return str(task.id)

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

        fingerprint = _request_fingerprint(
            "send",
            {
                "workspace_id": req.workspace_id,
                "recipient_id": req.recipient_id,
                "author_id": req.author_id,
                "message": req.message,
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
        )
        if is_new:
            self._set_last_message(recipient.id, req.message)
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

        fingerprint = _request_fingerprint(
            "followup",
            {
                "workspace_id": req.workspace_id,
                "recipient_id": req.recipient_id,
                "author_id": req.author_id,
                "message": req.message,
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
        )
        if not is_new:
            return event

        self._set_last_message(recipient.id, req.message)

        # Resume the executor's turn.
        try:
            await self._adapter(recipient.executor_kind).followup(recipient, req.message)
            self._update_run_status(recipient.id, AgentRunStatus.RUNNING)
        except Exception as exc:
            logger.exception("followup failed for run %s", recipient.id)
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

        async with lock:
            events = self._events_for(
                req.workspace_id, req.recipient_id, req.since_sequence, req.subtree
            )
            if events:
                return events
            # Clear while holding the lock so an append_event that arrives
            # after this point will set the event after we've cleared it.
            ev.clear()

        try:
            await asyncio.wait_for(ev.wait(), timeout=req.timeout_seconds)
        except asyncio.TimeoutError:
            pass

        return self._events_for(req.workspace_id, req.recipient_id, req.since_sequence, req.subtree)

    def ack(self, workspace_id: str, run_id: str, sequence: int) -> AgentRun:
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

        try:
            await self._adapter(run.executor_kind).interrupt(run, req.reason)
        except Exception:
            logger.exception("interrupt failed for run %s", run.id)

        self._update_run_status(run.id, AgentRunStatus.INTERRUPTED)
        self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=run.id,
            event_type=AgentEventType.INTERRUPTED,
            author=run.id,
            recipient=run.supervisor_id,
            call_id=req.call_id,
            action="interrupt",
            target=run.id,
            fingerprint=fingerprint,
            payload={"reason": req.reason},
        )
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
        """Return events for a run (or its subtree) after a cursor."""
        return self._events_for(workspace_id, run_id, since_sequence, subtree)

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
        if subtree:
            # Events addressed to this run, or authored by any run in its
            # subtree. We do NOT include events merely because their
            # agent_run_id is in the subtree — that would leak events
            # directed at a sibling's mailbox to this supervisor.
            subtree_ids = self._subtree_run_ids(run)
            return [
                e
                for e in all_events
                if e.sequence > since_sequence
                and (e.recipient == run_id or e.author in subtree_ids)
            ]
        return [
            e
            for e in all_events
            if e.sequence > since_sequence and (e.recipient == run_id or e.agent_run_id == run_id)
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

    def emit_event(
        self,
        *,
        workspace_id: str,
        agent_run_id: str,
        event_type: AgentEventType,
        author: str,
        recipient: Optional[str],
        call_id: str,
        payload: Optional[dict] = None,
    ) -> AgentEvent:
        """Ingest an event from an executor into the Hub stream.

        This is how managed-task reports, native-subagent progress, etc.
        flow back into the tree. The Hub updates the run's status and
        last_task_message from the event.
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
        )
        if not is_new:
            return event

        run = self._runs.get(agent_run_id)
        if run is not None:
            if event_type in TERMINAL_EVENT_TYPES:
                status_map = {
                    AgentEventType.COMPLETED: AgentRunStatus.COMPLETED,
                    AgentEventType.FAILED: AgentRunStatus.FAILED,
                    AgentEventType.INTERRUPTED: AgentRunStatus.INTERRUPTED,
                }
                self._update_run_status(agent_run_id, status_map[event_type])
            elif event_type == AgentEventType.BLOCKED:
                self._update_run_status(agent_run_id, AgentRunStatus.BLOCKED)
            elif event_type == AgentEventType.APPROVAL_REQUIRED:
                self._update_run_status(agent_run_id, AgentRunStatus.WAITING)
            elif event_type == AgentEventType.TOOL_WAIT:
                self._update_run_status(agent_run_id, AgentRunStatus.WAITING)
            else:
                # For non-terminal events, check the report_state in the
                # payload. READY_FOR_REVIEW and REVIEW_STARTED mean the run
                # is waiting for review to complete.
                report_state = (payload or {}).get("report_state")
                if report_state in {"ready_for_review", "review_started"}:
                    self._update_run_status(agent_run_id, AgentRunStatus.WAITING)

            msg = (payload or {}).get("message")
            if msg:
                self._set_last_message(agent_run_id, str(msg))

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

        events = [AgentEvent(**item) for item in data.get("agent_events", [])]
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

    async def recover_pending_runs(self, workspace_id: str) -> None:
        """Recover runs that were persisted but never reached a terminal state.

        After a crash, a run may be in PENDING status with a DISPATCHED
        event but no context_ref (the adapter call was lost). This method
        retries the adapter's spawn for each such run. The adapter is
        idempotent: if the executor context already exists (e.g. a
        workspace task with ``agent_run_id == run.id``), it is reused.

        Runs that are already RUNNING, WAITING, BLOCKED, or terminal are
        left untouched.
        """
        for run in list(self._runs.values()):
            if run.workspace_id != workspace_id:
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
