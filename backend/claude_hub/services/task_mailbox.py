"""Task-owned durable mailbox: append, call_id index, wait/ack, persist.

Reuses Agent Tree fingerprint / append-rollback / cold-load index rebuild.
Does not write AgentRun lifecycle, cursor, or context.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from ..models import AgentReport, WorkspaceTask
from ..models.agent_tree import AgentEvent, AgentEventType, AgentRun, ExecutorKind
from ..models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from .agent_tree import _request_fingerprint
from .directed_wait import DirectedWaitCoordinator
from .task_graph import (
    TASK_CONSUMER_PREFIX,
    compat_run_id_for_task,
    make_task_consumer_key,
    task_supervisor_consumer_key,
    tasks_in_subtree,
)
from .task_migration import linked_run_for_task, task_for_run

logger = logging.getLogger(__name__)


class TaskCallIdConflict(ValueError):
    """Same call_id reused with a different action, target, or fingerprint.

    Maps to HTTP 409 on the Task-first public surface. Unrelated validation
    stays a plain ``ValueError`` (HTTP 400).
    """


_AGENT_TYPE_TO_TASK: Dict[AgentEventType, TaskEventType] = {
    AgentEventType.DISPATCHED: TaskEventType.DISPATCHED,
    AgentEventType.STARTED: TaskEventType.STARTED,
    AgentEventType.PROGRESS: TaskEventType.PROGRESS,
    AgentEventType.HEARTBEAT: TaskEventType.PROGRESS,
    AgentEventType.MESSAGE: TaskEventType.MESSAGE,
    AgentEventType.TOOL_WAIT: TaskEventType.PROGRESS,
    AgentEventType.APPROVAL_REQUIRED: TaskEventType.NEEDS_INPUT,
    AgentEventType.BLOCKED: TaskEventType.NEEDS_INPUT,
    AgentEventType.FAILED: TaskEventType.FAILED,
    AgentEventType.COMPLETED: TaskEventType.COMPLETED,
    AgentEventType.INTERRUPTED: TaskEventType.INTERRUPT,
}


_TASK_TYPE_TO_AGENT: Dict[TaskEventType, AgentEventType] = {
    TaskEventType.DISPATCHED: AgentEventType.DISPATCHED,
    TaskEventType.STARTED: AgentEventType.STARTED,
    TaskEventType.PROGRESS: AgentEventType.PROGRESS,
    TaskEventType.REPORT: AgentEventType.PROGRESS,
    TaskEventType.FOLLOWUP: AgentEventType.MESSAGE,
    TaskEventType.MESSAGE: AgentEventType.MESSAGE,
    TaskEventType.REVIEW_STARTED: AgentEventType.PROGRESS,
    TaskEventType.REVIEW_PASSED: AgentEventType.COMPLETED,
    TaskEventType.REVIEW_FAILED: AgentEventType.PROGRESS,
    TaskEventType.REVIEW_NEEDS_INPUT: AgentEventType.BLOCKED,
    TaskEventType.NEEDS_INPUT: AgentEventType.APPROVAL_REQUIRED,
    TaskEventType.HUMAN_ACCEPTANCE_REQUESTED: AgentEventType.APPROVAL_REQUIRED,
    TaskEventType.HUMAN_ACCEPTED: AgentEventType.COMPLETED,
    TaskEventType.ABORT: AgentEventType.INTERRUPTED,
    TaskEventType.INTERRUPT: AgentEventType.INTERRUPTED,
    TaskEventType.COMPLETED: AgentEventType.COMPLETED,
    TaskEventType.FAILED: AgentEventType.FAILED,
}


def project_task_event_to_agent_event(
    event: TaskEvent,
    *,
    recipient_id: str,
    author_id: Optional[str] = None,
) -> AgentEvent:
    """In-memory AgentEvent view. Never appended to Agent Tree storage."""

    payload = dict(event.payload or {})
    if event.actor_session_id and "actor_session_id" not in payload:
        payload["actor_session_id"] = event.actor_session_id
    if "actor_role" not in payload:
        payload["actor_role"] = event.actor_role.value
    if event.review_cycle is not None and "review_cycle" not in payload:
        payload["review_cycle"] = event.review_cycle
    if event.task_id and "task_id" not in payload:
        payload["task_id"] = event.task_id
    if event.report_id and "report_id" not in payload:
        payload["report_id"] = event.report_id
    return AgentEvent(
        sequence=event.sequence,
        call_id=event.call_id,
        agent_run_id=event.compat_run_id or recipient_id,
        type=_TASK_TYPE_TO_AGENT.get(event.type, AgentEventType.PROGRESS),
        author=author_id or event.compat_run_id or recipient_id,
        recipient=recipient_id,
        action=event.action,
        target=event.target,
        fingerprint=event.fingerprint,
        payload=payload,
        created_at=event.created_at,
    )


class TaskMailbox:
    """Workspace-scoped Task event stream and ``task:<task_id>`` consumer cursors."""

    def __init__(self, workspace_manager: Any) -> None:
        self._wm = workspace_manager
        self._events: Dict[str, List[TaskEvent]] = {}
        self._next_seq: Dict[str, int] = {}
        self._call_index: Dict[str, Dict[str, dict[str, Any]]] = {}
        self._waiters = DirectedWaitCoordinator()

    def _persist(self) -> None:
        self._wm._save_state()

    def _workspace_max_sequence(self, workspace_id: str) -> int:
        task_max = max((item.sequence for item in self._events.get(workspace_id, [])), default=0)
        agent_max = max(
            (item.sequence for item in self._wm.agent_tree._events.get(workspace_id, [])),
            default=0,
        )
        pending = self._next_seq.get(workspace_id, 1) - 1
        return max(task_max, agent_max, pending)

    def _next_sequence(self, workspace_id: str) -> int:
        seq = self._workspace_max_sequence(workspace_id) + 1
        self._next_seq[workspace_id] = seq + 1
        return seq

    def _call_record(self, workspace_id: str, call_id: str) -> Optional[dict[str, Any]]:
        return self._call_index.get(workspace_id, {}).get(call_id)

    def _record_call(
        self,
        workspace_id: str,
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
        event: TaskEvent,
    ) -> None:
        existing = self._call_record(workspace_id, call_id)
        if existing is not None:
            if (
                existing["action"] != action
                or existing["target"] != target
                or existing["fingerprint"] != fingerprint
            ):
                raise TaskCallIdConflict(
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

    def _compat_task_event(self, workspace_id: str, call_id: str) -> Optional[TaskEvent]:
        for event in self._events.get(workspace_id, []):
            if event.call_id == call_id:
                return event
        return None

    @staticmethod
    def _event_report_id(event: TaskEvent | AgentEvent) -> Optional[str]:
        if isinstance(event, TaskEvent) and isinstance(event.report_id, str) and event.report_id:
            return event.report_id
        payload = dict(event.payload or {})
        value = payload.get("report_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _legacy_event_action(event: TaskEvent | AgentEvent) -> str:
        return event.action or "emit"

    @staticmethod
    def _legacy_event_target(event: TaskEvent | AgentEvent) -> Optional[str]:
        if event.target:
            return event.target
        if isinstance(event, AgentEvent):
            return event.agent_run_id
        return None

    def _is_canonical_legacy_report_alias(
        self,
        event: TaskEvent | AgentEvent,
        *,
        workspace_id: str,
        task_id: str,
        report_id: str,
    ) -> bool:
        """True when an existing event is the historical emit/run ``report:<id>`` bridge."""

        if self._legacy_event_action(event) != "emit":
            return False
        target = self._legacy_event_target(event)
        if target is None or target == task_id:
            return False
        if self._event_report_id(event) != report_id:
            return False
        if isinstance(event, TaskEvent):
            return event.task_id == task_id
        linked = self._resolve_legacy_task(workspace_id, event)
        return linked is not None and linked.id == task_id

    def _matches_stored_report_projection(
        self,
        *,
        workspace_id: str,
        task_id: str,
        action: str,
        target: str,
        call_id: str,
        report_id: Optional[str],
        event_type: TaskEventType,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        payload: Optional[Dict[str, Any]],
    ) -> bool:
        """True only when the new write equals the stored report's bridge body.

        Payload must be exactly ``_canonical_report_bridge_payload``: no extra
        keys and no missing keys. Field spot-checks are not enough.
        """

        if not report_id:
            return False
        report = self._wm.reports.get(report_id)
        if report is None or report.workspace_id != workspace_id:
            return False
        if report.task_id != task_id:
            return False
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            return False
        session = self._wm.sessions.get(report.session_id)
        if session is None or session.workspace_id != workspace_id:
            return False
        expected_role = self._wm._task_actor_role_for_session(session)
        expected_type = self._wm._task_event_type_for_report(report.state, task)
        expected = {
            "action": "report",
            "target": task.id,
            "call_id": f"report:{report.id}",
            "report_id": report.id,
            "task_id": task.id,
            "actor_session_id": session.id,
            "actor_role": expected_role.value,
            "review_cycle": report.review_cycle,
            "event_type": expected_type.value,
            "payload": self._wm._canonical_report_bridge_payload(report, session, task),
        }
        incoming = {
            "action": action,
            "target": target,
            "call_id": call_id,
            "report_id": report_id,
            "task_id": task_id,
            "actor_session_id": actor_session_id,
            "actor_role": actor_role.value,
            "review_cycle": review_cycle,
            "event_type": event_type.value,
            "payload": dict(payload or {}),
        }
        return incoming == expected

    def _reuse_legacy_report_alias(
        self,
        workspace_id: str,
        *,
        call_id: str,
        action: str,
        target: str,
        task_id: str,
        report_id: Optional[str],
        event_type: TaskEventType,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        payload: Optional[Dict[str, Any]],
    ) -> Optional[TaskEvent]:
        """Reuse the original emit/run event for a matching stored-report rewrite."""

        if not self._matches_stored_report_projection(
            workspace_id=workspace_id,
            task_id=task_id,
            action=action,
            target=target,
            call_id=call_id,
            report_id=report_id,
            event_type=event_type,
            actor_role=actor_role,
            actor_session_id=actor_session_id,
            review_cycle=review_cycle,
            payload=payload,
        ):
            return None
        assert report_id is not None

        existing = self._compat_task_event(workspace_id, call_id)
        if existing is not None:
            if self._is_canonical_legacy_report_alias(
                existing,
                workspace_id=workspace_id,
                task_id=task_id,
                report_id=report_id,
            ):
                return existing
            return None

        foreign = self._wm.agent_tree._call_record(workspace_id, call_id)
        if foreign is None:
            return None
        agent_event = foreign.get("event")
        if not isinstance(agent_event, AgentEvent):
            return None
        if not self._is_canonical_legacy_report_alias(
            agent_event,
            workspace_id=workspace_id,
            task_id=task_id,
            report_id=report_id,
        ):
            return None
        return self._preview_from_agent_event(workspace_id, agent_event)

    def _preview_from_agent_event(self, workspace_id: str, agent_event: AgentEvent) -> TaskEvent:
        """Build a non-persisted view of an Agent Tree event. Never empty task_id."""

        task = self._resolve_legacy_task(workspace_id, agent_event)
        if task is None:
            raise ValueError(
                f"call_id {agent_event.call_id!r} already recorded in Agent Tree "
                f"compat index and is not linked to a Task"
            )
        actor_session_id, actor_role = self._legacy_actor_fields(agent_event)
        payload = dict(agent_event.payload or {})
        report_id = payload.get("report_id")
        review_cycle = payload.get("review_cycle")
        return TaskEvent(
            sequence=agent_event.sequence,
            call_id=agent_event.call_id,
            fingerprint=agent_event.fingerprint
            or _request_fingerprint(
                agent_event.action or "emit",
                {
                    "sequence": agent_event.sequence,
                    "call_id": agent_event.call_id,
                    "agent_run_id": agent_event.agent_run_id,
                },
            ),
            task_id=task.id,
            actor_session_id=actor_session_id,
            actor_role=actor_role,
            review_cycle=int(review_cycle) if review_cycle is not None else None,
            type=_AGENT_TYPE_TO_TASK.get(agent_event.type, TaskEventType.PROGRESS),
            action=agent_event.action or "emit",
            target=agent_event.target or agent_event.agent_run_id,
            consumer_key=(
                make_task_consumer_key(task.parent_task_id)
                if task.parent_task_id
                else make_task_consumer_key(task.id)
            ),
            payload=payload,
            created_at=agent_event.created_at,
            compat_run_id=task.agent_run_id or agent_event.agent_run_id,
            report_id=report_id if isinstance(report_id, str) else None,
        )

    def _reject_or_reuse_foreign_call_id(
        self,
        workspace_id: str,
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
    ) -> Optional[TaskEvent]:
        """Reuse a matching Agent Tree call_id; never append a second Task event."""

        foreign = self._wm.agent_tree._call_record(workspace_id, call_id)
        if foreign is None:
            return None
        if (
            foreign["action"] != action
            or foreign["target"] != target
            or foreign["fingerprint"] != fingerprint
        ):
            raise TaskCallIdConflict(
                f"call_id {call_id!r} already used for action="
                f"{foreign['action']!r} target={foreign['target']!r} "
                f"in workspace {workspace_id}; cannot reuse for "
                f"action={action!r} target={target!r}"
            )
        existing = self._call_record(workspace_id, call_id)
        if existing is not None:
            existing_event = existing["event"]
            if isinstance(existing_event, TaskEvent):
                return existing_event
            raise TaskCallIdConflict(f"call_id {call_id!r} already recorded in Task mailbox index")
        found = self._compat_task_event(workspace_id, call_id)
        if found is not None:
            return found
        agent_event = foreign.get("event")
        if isinstance(agent_event, AgentEvent):
            return self._preview_from_agent_event(workspace_id, agent_event)
        raise TaskCallIdConflict(f"call_id {call_id!r} already recorded in Agent Tree compat index")

    def consumer_cursor(self, workspace_id: str, consumer_key: str) -> int:
        if not consumer_key.startswith(TASK_CONSUMER_PREFIX):
            raise ValueError(f"Mailbox consumer must be task:<task_id>; got {consumer_key!r}")
        task_id = consumer_key[len(TASK_CONSUMER_PREFIX) :]
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        return int(task.consumer_ack_sequence)

    def _set_consumer_cursor(self, workspace_id: str, consumer_key: str, sequence: int) -> None:
        if not consumer_key.startswith(TASK_CONSUMER_PREFIX):
            raise ValueError(f"Mailbox consumer must be task:<task_id>; got {consumer_key!r}")
        task_id = consumer_key[len(TASK_CONSUMER_PREFIX) :]
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        self._wm.tasks[task_id] = task.model_copy(update={"consumer_ack_sequence": sequence})

    def _compat_run_id_for_linked_task(self, workspace_id: str, task: WorkspaceTask) -> str:
        """Stable event author id. Unique ``context_ref`` is a read-only hint."""

        if task.agent_run_id:
            return task.agent_run_id
        runs: Dict[str, AgentRun] = cast(Dict[str, AgentRun], self._wm.agent_tree._runs)
        matches = [
            run
            for run in runs.values()
            if run.workspace_id == workspace_id
            and run.executor_kind == ExecutorKind.MANAGED_TASK
            and run.context_ref == task.id
        ]
        if len(matches) == 1:
            return matches[0].id
        return compat_run_id_for_task(task)

    def append_event(
        self,
        *,
        workspace_id: str,
        task_id: str,
        actor_role: TaskActorRole,
        event_type: TaskEventType,
        call_id: str,
        action: str,
        consumer_key: Optional[str] = None,
        actor_session_id: Optional[str] = None,
        review_cycle: Optional[int] = None,
        target: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        report_id: Optional[str] = None,
        persist: bool = True,
        wake: bool = True,
    ) -> Tuple[TaskEvent, bool]:
        """Append a Task event. Idempotent on ``(workspace_id, call_id)``."""

        if workspace_id not in self._wm.workspaces:
            raise ValueError(f"Workspace {workspace_id} not found")
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)

        resolved_target = target or task_id
        resolved_consumer = consumer_key or (
            make_task_consumer_key(task.parent_task_id)
            if task.parent_task_id
            else make_task_consumer_key(task.id)
        )
        body: Dict[str, Any] = {
            "task_id": task_id,
            "actor_session_id": actor_session_id,
            "actor_role": actor_role.value,
            "review_cycle": review_cycle,
            "event_type": event_type.value,
            "target": resolved_target,
            "consumer_key": resolved_consumer,
            "payload": payload or {},
            "report_id": report_id,
        }
        fingerprint = _request_fingerprint(action, body)
        alias = self._reuse_legacy_report_alias(
            workspace_id,
            call_id=call_id,
            action=action,
            target=resolved_target,
            task_id=task_id,
            report_id=report_id,
            event_type=event_type,
            actor_role=actor_role,
            actor_session_id=actor_session_id,
            review_cycle=review_cycle,
            payload=payload,
        )
        if alias is not None:
            return self._finish_append(alias, persist=persist, wake=wake, created=False)
        reused = self._reject_or_reuse_foreign_call_id(
            workspace_id, call_id, action, resolved_target, fingerprint
        )
        if reused is not None:
            return self._finish_append(reused, persist=persist, wake=wake, created=False)

        existing = self._call_record(workspace_id, call_id)
        if existing is not None:
            if (
                existing["action"] != action
                or existing["target"] != resolved_target
                or existing["fingerprint"] != fingerprint
            ):
                raise TaskCallIdConflict(
                    f"call_id {call_id!r} already used for action="
                    f"{existing['action']!r} target={existing['target']!r} "
                    f"in workspace {workspace_id}; cannot reuse for "
                    f"action={action!r} target={resolved_target!r}"
                )
            return self._finish_append(existing["event"], persist=persist, wake=wake, created=False)

        seq = self._next_sequence(workspace_id)
        event = TaskEvent(
            sequence=seq,
            call_id=call_id,
            fingerprint=fingerprint,
            task_id=task_id,
            actor_session_id=actor_session_id,
            actor_role=actor_role,
            review_cycle=review_cycle,
            type=event_type,
            action=action,
            target=resolved_target,
            consumer_key=resolved_consumer,
            payload=payload or {},
            created_at=datetime.utcnow(),
            compat_run_id=self._compat_run_id_for_linked_task(workspace_id, task),
            report_id=report_id,
        )
        ws_events = self._events.setdefault(workspace_id, [])
        ws_events.append(event)
        self._record_call(workspace_id, call_id, action, resolved_target, fingerprint, event)

        if not persist:
            return event, True
        try:
            self._persist()
        except Exception:
            ws_events.pop()
            self._call_index.get(workspace_id, {}).pop(call_id, None)
            self._next_seq[workspace_id] = seq
            raise
        if wake:
            self._wake_compat_waiters(event)
        return event, True

    def _finish_append(
        self,
        event: TaskEvent,
        *,
        persist: bool,
        wake: bool,
        created: bool,
    ) -> Tuple[TaskEvent, bool]:
        if persist:
            self._persist()
            if wake:
                self._wake_compat_waiters(event)
        return event, created

    def _unstage_events(self, workspace_id: str, events: List[TaskEvent]) -> None:
        """Drop a just-staged suffix so a failed pair does not leave one event."""

        ws_events = self._events.get(workspace_id, [])
        for event in reversed(events):
            if not ws_events or ws_events[-1] is not event:
                continue
            ws_events.pop()
            self._call_index.get(workspace_id, {}).pop(event.call_id, None)
            self._next_seq[workspace_id] = event.sequence

    def _wake_compat_waiters(self, event: TaskEvent) -> None:
        """Wake after a durable TaskMailbox commit.

        Resolve the Task once. Wake its linked compat run, then ancestor
        Task consumers and the workspace resident when the event is
        addressed to that consumer or the Task is top-level. Never compare
        ``workspace_id`` to a Task id. ``persist=False`` callers must not
        invoke this until their outer ``_save_state`` succeeds.
        """

        task = self._wm.tasks.get(event.task_id)
        if task is None:
            return
        self._wake_task_consumers(event, task)
        tree = self._wm.agent_tree
        woken: set[str] = set()

        def _wake_run(run_id: Optional[str]) -> None:
            if not run_id or run_id in woken:
                return
            woken.add(run_id)
            tree._wake_for_run(run_id, run_id)

        _wake_run(event.compat_run_id or compat_run_id_for_task(task))

        ancestor_id = task.parent_task_id
        seen = {task.id}
        while ancestor_id and ancestor_id not in seen:
            seen.add(ancestor_id)
            ancestor = self._wm.tasks.get(ancestor_id)
            if ancestor is None or ancestor.workspace_id != task.workspace_id:
                break
            _wake_run(compat_run_id_for_task(ancestor))
            ancestor_id = ancestor.parent_task_id

        if event.consumer_key.startswith(TASK_CONSUMER_PREFIX):
            consumer_task = self._wm.tasks.get(event.consumer_key[len(TASK_CONSUMER_PREFIX) :])
            if consumer_task is not None and consumer_task.workspace_id == task.workspace_id:
                _wake_run(compat_run_id_for_task(consumer_task))

    def _wake_task_consumers(self, event: TaskEvent, task: WorkspaceTask) -> None:
        """Wake Task waiters. Never requires an AgentRun or Resident consumer."""

        self._waiters.wake(event.consumer_key)
        ancestor_id = task.parent_task_id
        seen = {task.id}
        while ancestor_id and ancestor_id not in seen:
            seen.add(ancestor_id)
            ancestor = self._wm.tasks.get(ancestor_id)
            if ancestor is None or ancestor.workspace_id != task.workspace_id:
                break
            self._waiters.wake_if_subtree(make_task_consumer_key(ancestor.id))
            ancestor_id = ancestor.parent_task_id

    def wait(
        self,
        workspace_id: str,
        consumer_key: str,
        since_sequence: int = 0,
        subtree: bool = False,
    ) -> List[TaskEvent]:
        """Return events after the durable cursor.

        Direct wait (``subtree=False``) is addressed only. Parent subtree
        replay includes descendant Task events so a root sees grandchildren.
        Immediate snapshot; use ``wait_for`` for the directed long-poll.
        """

        cursor = self.consumer_cursor(workspace_id, consumer_key)
        floor = max(int(since_sequence), cursor)
        visible = self._visible_task_ids(workspace_id, consumer_key) if subtree else None
        matched: List[TaskEvent] = []
        for event in self._events.get(workspace_id, []):
            if event.sequence <= floor:
                continue
            if event.consumer_key == consumer_key:
                matched.append(event)
                continue
            if visible is not None and event.task_id in visible:
                matched.append(event)
        return matched

    async def wait_for(
        self,
        workspace_id: str,
        consumer_key: str,
        since_sequence: int = 0,
        subtree: bool = False,
        timeout_seconds: float = 30.0,
    ) -> List[TaskEvent]:
        """Directed long-poll for a Task consumer. No AgentRun."""

        self.consumer_cursor(workspace_id, consumer_key)
        return await self._waiters.wait(
            consumer_key,
            subtree=subtree,
            timeout_seconds=timeout_seconds,
            poll=lambda: self.wait(
                workspace_id,
                consumer_key,
                since_sequence=since_sequence,
                subtree=subtree,
            ),
        )

    def _visible_task_ids(self, workspace_id: str, consumer_key: str) -> set[str]:
        if not consumer_key.startswith(TASK_CONSUMER_PREFIX):
            raise ValueError(f"Mailbox consumer must be task:<task_id>; got {consumer_key!r}")
        task_id = consumer_key[len(TASK_CONSUMER_PREFIX) :]
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        return {item.id for item in tasks_in_subtree(self._wm.tasks.values(), workspace_id, task)}

    def ack(self, workspace_id: str, consumer_key: str, sequence: int, persist: bool = True) -> int:
        """Advance a Task consumer cursor. Never writes AgentRun.ack or resident index."""

        current = self.consumer_cursor(workspace_id, consumer_key)
        workspace_max = self._workspace_max_sequence(workspace_id)
        if sequence < current:
            raise ValueError(f"ACK sequence {sequence} is behind current ack cursor {current}")
        if sequence > workspace_max:
            raise ValueError(f"ACK sequence {sequence} is ahead of workspace max {workspace_max}")
        previous = current
        self._set_consumer_cursor(workspace_id, consumer_key, sequence)
        if not persist:
            return sequence
        try:
            self._persist()
        except Exception:
            self._set_consumer_cursor(workspace_id, consumer_key, previous)
            raise
        return sequence

    def to_dict(self, workspace_id: str) -> Dict[str, Any]:
        return {
            "task_events": [
                item.model_dump(mode="json") for item in self._events.get(workspace_id, [])
            ]
        }

    def load_from_dict(self, workspace_id: str, data: Dict[str, Any]) -> None:
        """Load Task events, project leftover AgentEvents, rebuild the call index."""

        events: List[TaskEvent] = []
        for item in data.get("task_events") or []:
            events.append(TaskEvent(**item))
        events.sort(key=lambda event: event.sequence)
        self._events[workspace_id] = events
        self._rebuild_call_index(workspace_id)
        self._project_legacy_agent_events(workspace_id)
        self._backfill_report_task_events(workspace_id)
        self._rebuild_call_index(workspace_id)
        self._next_seq[workspace_id] = self._workspace_max_sequence(workspace_id) + 1

    def purge_task_events(self, workspace_id: str, task_id: str) -> None:
        """Remove durable Task events and call-index entries for one Task."""

        remaining = [
            event
            for event in self._events.get(workspace_id, [])
            if event.task_id != task_id
        ]
        self._events[workspace_id] = remaining
        self._rebuild_call_index(workspace_id)

    def _rebuild_call_index(self, workspace_id: str) -> None:
        ws_calls: Dict[str, dict[str, Any]] = {}
        for event in self._events.get(workspace_id, []):
            existing = ws_calls.get(event.call_id)
            if existing is not None:
                if (
                    existing["action"] != event.action
                    or existing["target"] != event.target
                    or existing["fingerprint"] != event.fingerprint
                ):
                    raise ValueError(
                        f"conflicting duplicate call_id {event.call_id!r} in workspace "
                        f"{workspace_id}: action={existing['action']!r} "
                        f"target={existing['target']!r} vs action={event.action!r} "
                        f"target={event.target!r}"
                    )
                continue
            ws_calls[event.call_id] = {
                "action": event.action,
                "target": event.target,
                "fingerprint": event.fingerprint,
                "event": event,
            }
        self._call_index[workspace_id] = ws_calls

    def _run_to_task_index(self, workspace_id: str) -> Dict[str, WorkspaceTask]:
        run_to_task: Dict[str, WorkspaceTask] = {}
        for task in self._wm.tasks.values():
            if task.workspace_id != workspace_id:
                continue
            if task.agent_run_id:
                run_to_task[task.agent_run_id] = task
        for run in self._wm.agent_tree._runs.values():
            if run.workspace_id != workspace_id:
                continue
            context_ref = run.context_ref
            if context_ref and context_ref in self._wm.tasks:
                mapped = self._wm.tasks[context_ref]
                if mapped.workspace_id == workspace_id:
                    run_to_task[run.id] = mapped
        return run_to_task

    def _resolve_legacy_task(
        self, workspace_id: str, agent_event: AgentEvent
    ) -> Optional[WorkspaceTask]:
        payload = dict(agent_event.payload or {})
        payload_task_id = payload.get("task_id")
        if isinstance(payload_task_id, str):
            task = self._wm.tasks.get(payload_task_id)
            if isinstance(task, WorkspaceTask) and task.workspace_id == workspace_id:
                return task
        for run_id in (agent_event.agent_run_id, agent_event.author):
            task = self._compat_task_for_id(workspace_id, run_id)
            if task is not None:
                return task
        return None

    def _compat_task_for_id(
        self, workspace_id: str, run_id: Optional[str]
    ) -> Optional[WorkspaceTask]:
        if not run_id:
            return None
        run = self._wm.agent_tree._runs.get(run_id)
        if run is not None and run.workspace_id == workspace_id:
            return task_for_run(run, self._wm.tasks, workspace_id)
        found = self._wm.tasks.get(run_id)
        if (
            isinstance(found, WorkspaceTask)
            and found.workspace_id == workspace_id
            and compat_run_id_for_task(found) == run_id
        ):
            return found
        return None

    def _legacy_report_id(self, event: TaskEvent | AgentEvent) -> Optional[str]:
        report_id = self._event_report_id(event)
        if report_id:
            return report_id
        if event.call_id.startswith("report:"):
            suffix = event.call_id[len("report:") :]
            return suffix or None
        return None

    def _legacy_event_task_id(
        self, event: TaskEvent | AgentEvent, workspace_id: str
    ) -> Optional[str]:
        if isinstance(event, TaskEvent):
            return event.task_id
        task = self._resolve_legacy_task(workspace_id, event)
        return task.id if task is not None else None

    def _is_same_report_alias(
        self,
        existing: TaskEvent,
        agent_event: AgentEvent,
        workspace_id: str,
    ) -> bool:
        existing_report = self._legacy_report_id(existing)
        incoming_report = self._legacy_report_id(agent_event)
        incoming_task = self._legacy_event_task_id(agent_event, workspace_id)
        if not existing_report or not incoming_report or not incoming_task:
            return False
        return existing_report == incoming_report and existing.task_id == incoming_task

    def _report_alias_identity_conflict(
        self,
        existing: TaskEvent,
        agent_event: AgentEvent,
        workspace_id: str,
    ) -> bool:
        existing_report = self._legacy_report_id(existing)
        incoming_report = self._legacy_report_id(agent_event)
        incoming_task = self._legacy_event_task_id(agent_event, workspace_id)
        if not existing_report and not incoming_report:
            return False
        if existing_report and incoming_report and existing_report != incoming_report:
            return True
        return bool(incoming_task and existing.task_id != incoming_task)

    def _stored_report_for_legacy(
        self,
        workspace_id: str,
        agent_event: AgentEvent,
        task: WorkspaceTask,
    ) -> Optional[AgentReport]:
        report_id = self._legacy_report_id(agent_event)
        if not report_id:
            return None
        report = self._wm.reports.get(report_id)
        if not isinstance(report, AgentReport) or report.workspace_id != workspace_id:
            return None
        if report.task_id and report.task_id != task.id:
            return None
        return report

    def _legacy_consumer_key(self, workspace_id: str, agent_event: AgentEvent) -> Optional[str]:
        recipient = agent_event.recipient
        if not recipient:
            return None
        projected = self._resolve_legacy_task(workspace_id, agent_event)
        if projected is not None and recipient == compat_run_id_for_task(projected):
            return make_task_consumer_key(projected.id)
        direct = self._wm.tasks.get(recipient)
        if direct is not None and direct.workspace_id == workspace_id:
            return make_task_consumer_key(direct.id)
        task = self._compat_task_for_id(workspace_id, recipient)
        if task is not None:
            return make_task_consumer_key(task.id)
        run = self._wm.agent_tree._runs.get(recipient)
        if run is not None and run.workspace_id == workspace_id:
            if run.executor_kind == ExecutorKind.RESIDENT_ROOT:
                if projected is not None:
                    return task_supervisor_consumer_key(projected)
                return None
            if projected is not None:
                linked = linked_run_for_task(projected, self._wm.agent_tree._runs)
                if linked is not None and recipient == linked.parent_id:
                    return task_supervisor_consumer_key(projected)
        if projected is not None:
            linked = linked_run_for_task(projected, self._wm.agent_tree._runs)
            if linked is not None and recipient == linked.parent_id:
                return task_supervisor_consumer_key(projected)
        return None

    def _project_legacy_agent_events(self, workspace_id: str) -> None:
        """Map leftover AgentEvents onto TaskEvent without mutating AgentRun."""

        known_by_call = {event.call_id: event for event in self._events.get(workspace_id, [])}
        report_ids = {
            event.report_id for event in self._events.get(workspace_id, []) if event.report_id
        }
        projected: List[TaskEvent] = []
        for agent_event in self._wm.agent_tree._events.get(workspace_id, []):
            payload = dict(agent_event.payload or {})
            report_id = payload.get("report_id")
            action = agent_event.action or "emit"
            target = agent_event.target or agent_event.agent_run_id
            fingerprint = agent_event.fingerprint or _request_fingerprint(
                action,
                {
                    "sequence": agent_event.sequence,
                    "call_id": agent_event.call_id,
                    "agent_run_id": agent_event.agent_run_id,
                    "event_type": agent_event.type.value,
                    "author": agent_event.author,
                    "recipient": agent_event.recipient,
                },
            )
            existing = known_by_call.get(agent_event.call_id)
            if existing is not None:
                if self._is_same_report_alias(existing, agent_event, workspace_id):
                    continue
                if self._report_alias_identity_conflict(existing, agent_event, workspace_id) or (
                    existing.action != action
                    or existing.target != target
                    or existing.fingerprint != fingerprint
                ):
                    raise ValueError(
                        f"conflicting duplicate call_id {agent_event.call_id!r} in workspace "
                        f"{workspace_id}: action={existing.action!r} "
                        f"target={existing.target!r} vs action={action!r} "
                        f"target={target!r}"
                    )
                continue
            if isinstance(report_id, str) and report_id in report_ids:
                continue
            task = self._resolve_legacy_task(workspace_id, agent_event)
            if task is None:
                continue
            consumer_key = self._legacy_consumer_key(workspace_id, agent_event)
            if consumer_key is None:
                continue
            stored = self._stored_report_for_legacy(workspace_id, agent_event, task)
            if stored is not None:
                actor_session_id, actor_role = self._wm._task_actor_for_report(stored)
                review_cycle: Optional[int] = stored.review_cycle
                event_type = self._wm._task_event_type_for_report(stored.state, task)
                report_id = stored.id
            else:
                actor_session_id, actor_role = self._legacy_actor_fields(agent_event)
                raw_cycle = payload.get("review_cycle")
                review_cycle = int(raw_cycle) if raw_cycle is not None else None
                event_type = _AGENT_TYPE_TO_TASK.get(agent_event.type, TaskEventType.PROGRESS)
                if not isinstance(report_id, str):
                    report_id = self._legacy_report_id(agent_event)
            event = TaskEvent(
                sequence=agent_event.sequence,
                call_id=agent_event.call_id,
                fingerprint=fingerprint,
                task_id=task.id,
                actor_session_id=actor_session_id,
                actor_role=actor_role,
                review_cycle=int(review_cycle) if review_cycle is not None else None,
                type=event_type,
                action=action,
                target=target,
                consumer_key=consumer_key,
                payload=payload,
                created_at=agent_event.created_at,
                compat_run_id=task.agent_run_id or agent_event.agent_run_id,
                report_id=report_id if isinstance(report_id, str) else None,
            )
            projected.append(event)
            known_by_call[event.call_id] = event
            if isinstance(report_id, str):
                report_ids.add(report_id)
        if projected:
            merged = list(self._events.get(workspace_id, []))
            merged.extend(projected)
            merged.sort(key=lambda event: event.sequence)
            self._events[workspace_id] = merged

    def _backfill_report_task_events(self, workspace_id: str) -> None:
        """Append missing AgentReports after legacy projection. Never writes AgentRun."""

        events = list(self._events.get(workspace_id, []))
        covered_report_ids = {event.report_id for event in events if event.report_id}
        covered_call_ids = {event.call_id for event in events}
        for agent_event in self._wm.agent_tree._events.get(workspace_id, []):
            covered_call_ids.add(agent_event.call_id)
            report_id = (agent_event.payload or {}).get("report_id")
            if isinstance(report_id, str):
                covered_report_ids.add(report_id)
        reports = [
            report
            for report in self._wm.reports.values()
            if report.workspace_id == workspace_id
            and report.task_id
            and report.task_id in self._wm.tasks
            and self._wm.tasks[report.task_id].workspace_id == workspace_id
        ]
        reports.sort(key=lambda report: (report.created_at, report.id))
        added: List[TaskEvent] = []
        for report in reports:
            task = self._wm.tasks[report.task_id]
            call_id = f"report:{report.id}"
            if report.id in covered_report_ids:
                continue
            if call_id in covered_call_ids:
                continue
            if report.call_id and report.call_id in covered_call_ids:
                continue
            actor_session_id, actor_role = self._wm._task_actor_for_report(report)
            event_type = self._wm._task_event_type_for_report(report.state, task)
            consumer_key = task_supervisor_consumer_key(task)
            payload = self._wm._canonical_report_event_payload(
                report,
                task,
                actor_session_id=actor_session_id,
                actor_role=actor_role,
            )
            body = {
                "task_id": task.id,
                "actor_session_id": actor_session_id,
                "actor_role": actor_role.value,
                "review_cycle": report.review_cycle,
                "event_type": event_type.value,
                "target": task.id,
                "consumer_key": consumer_key,
                "payload": payload,
                "report_id": report.id,
            }
            fingerprint = _request_fingerprint("report", body)
            existing = self._call_record(workspace_id, call_id)
            if existing is not None:
                if (
                    existing["action"] != "report"
                    or existing["target"] != task.id
                    or existing["fingerprint"] != fingerprint
                ):
                    raise ValueError(
                        f"conflicting duplicate call_id {call_id!r} in workspace "
                        f"{workspace_id}: action={existing['action']!r} "
                        f"target={existing['target']!r} vs action='report' "
                        f"target={task.id!r}"
                    )
                continue
            sequence = self._next_sequence(workspace_id)
            event = TaskEvent(
                sequence=sequence,
                call_id=call_id,
                fingerprint=fingerprint,
                task_id=task.id,
                actor_session_id=actor_session_id,
                actor_role=actor_role,
                review_cycle=report.review_cycle,
                type=event_type,
                action="report",
                target=task.id,
                consumer_key=consumer_key,
                payload=payload,
                created_at=report.created_at,
                compat_run_id=self._compat_run_id_for_linked_task(workspace_id, task),
                report_id=report.id,
            )
            added.append(event)
            covered_report_ids.add(report.id)
            covered_call_ids.add(call_id)
            self._record_call(workspace_id, call_id, "report", task.id, fingerprint, event)
        if added:
            merged = list(self._events.get(workspace_id, []))
            merged.extend(added)
            merged.sort(key=lambda event: event.sequence)
            self._events[workspace_id] = merged

    def _legacy_actor_fields(self, event: AgentEvent) -> Tuple[Optional[str], TaskActorRole]:
        payload = dict(event.payload or {})
        session_id = payload.get("actor_session_id")
        if not isinstance(session_id, str) or not session_id:
            session_raw = payload.get("session_id")
            session_id = session_raw if isinstance(session_raw, str) and session_raw else None
        role_raw = payload.get("actor_role")
        if isinstance(role_raw, str) and role_raw:
            try:
                return session_id, TaskActorRole(role_raw)
            except ValueError:
                pass
        return session_id, TaskActorRole.WORKER
