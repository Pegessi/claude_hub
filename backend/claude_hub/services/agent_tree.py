"""Unified Agent Tree + Durable Mailbox/Event Stream coordination layer.

The ``AgentTreeManager`` owns:
- The agent run tree (parent/child relationships via ``path``).
- The append-only event stream (per workspace, monotonic ``sequence``).
- Per-run mailboxes (events addressed to a run, or authored within its
  subtree).
- call_id / correlation_id idempotency for all actions.

Actions:
- ``spawn``: create a child run, dispatch its initial task.
- ``send``: append a message to the recipient's mailbox (no turn wake).
- ``followup``: append a message AND resume the recipient's turn.
- ``wait``: block until directed events arrive (cursor-based).
- ``interrupt``: stop a run, preserving context.
- ``list_runs``: return the tree with status and last_task_message.

The Hub owns lifecycle state. Agents report progress by emitting events
(via the executor adapter), not by mutating ``AgentRun.status`` directly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

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


class AgentTreeManager:
    """Coordinates the agent tree, mailbox, and event stream."""

    def __init__(self, workspace_manager: "WorkspaceManager") -> None:
        self._wm = workspace_manager
        self._runs: Dict[str, AgentRun] = {}
        # Per-workspace append-only event log.
        self._events: Dict[str, List[AgentEvent]] = {}
        # Per-workspace next sequence number.
        self._next_seq: Dict[str, int] = {}
        # call_id -> event, for idempotency (dedupe repeated calls).
        self._call_index: Dict[str, AgentEvent] = {}
        # Per-run asyncio.Event for wait() wakeups.
        self._run_events: Dict[str, asyncio.Event] = {}
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
        survives a process crash. Failures are logged but not raised so a
        persistence error cannot break the in-memory coordination path.
        """
        try:
            self._wm._save_state()
        except Exception:
            logger.exception("Failed to persist agent tree state")

    def _append_event(
        self,
        *,
        workspace_id: str,
        agent_run_id: str,
        event_type: AgentEventType,
        author: str,
        recipient: Optional[str],
        call_id: str,
        correlation_id: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> Tuple[AgentEvent, bool]:
        """Append an event to the workspace stream.

        Idempotent on ``call_id``: if a call with the same id was already
        recorded, return the existing event and ``is_new=False``.

        Returns ``(event, is_new)`` so callers can skip adapter side effects
        on duplicate calls.
        """
        existing = self._call_index.get(call_id)
        if existing is not None:
            return existing, False

        seq = self._next_sequence(workspace_id)
        event = AgentEvent(
            sequence=seq,
            call_id=call_id,
            correlation_id=correlation_id,
            agent_run_id=agent_run_id,
            type=event_type,
            author=author,
            recipient=recipient,
            payload=payload or {},
            created_at=datetime.utcnow(),
        )
        self._events.setdefault(workspace_id, []).append(event)
        self._call_index[call_id] = event

        # Wake any waiters on the recipient run (and its ancestors, since
        # supervisors listen to their subtree).
        run = self._runs.get(agent_run_id)
        if run is not None:
            self._wake_ancestors(run)
        if recipient:
            ev = self._run_events.get(recipient)
            if ev is not None:
                ev.set()

        self._persist()
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
        run = self._runs.get(run_id)
        if run is None:
            return
        run.status = status
        run.updated_at = datetime.utcnow()
        self._persist()

    def _set_last_message(self, run_id: str, message: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.last_task_message = message
        run.updated_at = datetime.utcnow()
        self._persist()

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def spawn(self, req: SpawnRequest) -> AgentRun:
        """Create a child run and dispatch its initial task.

        Idempotent on ``call_id``: a duplicate call returns the existing
        run without re-creating it or re-triggering the executor adapter.
        """
        parent = self._runs.get(req.parent_id)
        if parent is None:
            raise KeyError(f"Parent run {req.parent_id} not found")
        if parent.workspace_id != req.workspace_id:
            raise ValueError("Parent run belongs to a different workspace")

        # Idempotency: if this call_id already produced a run, return it.
        existing_event = self._call_index.get(req.call_id)
        if existing_event is not None:
            existing_run = self._runs.get(existing_event.agent_run_id)
            if existing_run is not None:
                return existing_run

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
        self._persist()

        # Emit a dispatched event addressed to the child.
        self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=run.id,
            event_type=AgentEventType.DISPATCHED,
            author=parent.id,
            recipient=run.id,
            call_id=req.call_id,
            payload={"message": req.initial_message},
        )

        # Start the executor.
        try:
            context_ref = await self._adapter(req.executor_kind).spawn(run, req.initial_message)
            run.context_ref = context_ref
            self._update_run_status(run.id, AgentRunStatus.RUNNING)
            self._append_event(
                workspace_id=req.workspace_id,
                agent_run_id=run.id,
                event_type=AgentEventType.STARTED,
                author=run.id,
                recipient=parent.id,
                call_id=f"{req.call_id}:started",
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
                payload={"error": str(exc)},
            )
            raise

        return run

    async def send(self, req: SendRequest) -> AgentEvent:
        """Append a message to the recipient's mailbox without waking it."""
        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")

        event, is_new = self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=recipient.id,
            event_type=AgentEventType.MESSAGE,
            author=req.author_id,
            recipient=recipient.id,
            call_id=req.call_id,
            correlation_id=req.correlation_id,
            payload={"message": req.message},
        )
        if is_new:
            self._set_last_message(recipient.id, req.message)
        return event

    async def followup(self, req: FollowupRequest) -> AgentEvent:
        """Append a message and resume the recipient's turn.

        Idempotent on ``call_id``: a duplicate call returns the existing
        message event without re-triggering the executor adapter.
        """
        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")

        event, is_new = self._append_event(
            workspace_id=req.workspace_id,
            agent_run_id=recipient.id,
            event_type=AgentEventType.MESSAGE,
            author=req.author_id,
            recipient=recipient.id,
            call_id=req.call_id,
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
                payload={"error": str(exc)},
            )
            raise

        return event

    async def wait(self, req: WaitRequest) -> List[AgentEvent]:
        """Block until directed events arrive or timeout.

        Returns events for ``recipient_id`` (or its subtree when
        ``subtree=True``) with sequence > ``since_sequence``.
        """
        recipient = self._runs.get(req.recipient_id)
        if recipient is None:
            raise KeyError(f"Recipient run {req.recipient_id} not found")

        # Fast path: return immediately if there are already new events.
        events = self._events_for(
            req.workspace_id, req.recipient_id, req.since_sequence, req.subtree
        )
        if events:
            return events

        # Wait for new events.
        ev = self._run_events.setdefault(req.recipient_id, asyncio.Event())
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=req.timeout_seconds)
        except asyncio.TimeoutError:
            pass

        return self._events_for(req.workspace_id, req.recipient_id, req.since_sequence, req.subtree)

    async def interrupt(self, req: InterruptRequest) -> AgentRun:
        """Interrupt a run, preserving its context.

        Idempotent on ``call_id``: a duplicate call returns the existing
        run without re-triggering the executor adapter.
        """
        run = self._runs.get(req.run_id)
        if run is None:
            raise KeyError(f"Run {req.run_id} not found")

        # Idempotency: if this call_id already interrupted, return the run.
        existing_event = self._call_index.get(req.call_id)
        if existing_event is not None:
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
            payload={"reason": req.reason},
        )
        return run

    def list_runs(self, req: ListRunsRequest) -> List[AgentRun]:
        """List runs, optionally scoped to a subtree or status."""
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
            # Events authored by any run in the subtree, or addressed to
            # this run.
            subtree_ids = self._subtree_run_ids(run)
            return [
                e
                for e in all_events
                if e.sequence > since_sequence
                and (
                    e.recipient == run_id
                    or e.author in subtree_ids
                    or e.agent_run_id in subtree_ids
                )
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
        event, is_new = self._append_event(
            workspace_id=workspace_id,
            agent_run_id=agent_run_id,
            event_type=event_type,
            author=author,
            recipient=recipient,
            call_id=call_id,
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
        self._persist()
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

        Rebuilds the call_id index and the next sequence counter from the
        loaded events so idempotency and monotonic sequencing survive a
        restart.
        """
        for item in data.get("agent_runs", []):
            run = AgentRun(**item)
            self._runs[run.id] = run

        events = [AgentEvent(**item) for item in data.get("agent_events", [])]
        events.sort(key=lambda e: e.sequence)
        self._events[workspace_id] = events

        max_seq = 0
        for event in events:
            self._call_index[event.call_id] = event
            if event.sequence > max_seq:
                max_seq = event.sequence
        self._next_seq[workspace_id] = max_seq + 1
