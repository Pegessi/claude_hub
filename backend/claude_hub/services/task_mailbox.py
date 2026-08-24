"""Task-owned durable mailbox: append, call_id index, wait/ack, persist."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..models import WorkspaceTask
from ..models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from . import workspace_state_policy as state_policy
from .directed_wait import DirectedWaitCoordinator
from .request_fingerprint import request_fingerprint
from .task_graph import (
    TASK_CONSUMER_PREFIX,
    is_legacy_resident_consumer_key,
    make_task_consumer_key,
    task_supervisor_consumer_key,
    tasks_in_subtree,
)

logger = logging.getLogger(__name__)


class TaskCallIdConflict(ValueError):
    """Same call_id reused with a different action, target, or fingerprint.

    Maps to HTTP 409 on the Task-first public surface. Unrelated validation
    stays a plain ``ValueError`` (HTTP 400).
    """


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
        pending = self._next_seq.get(workspace_id, 1) - 1
        return max(task_max, pending)

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
    def _event_report_id(event: TaskEvent) -> Optional[str]:
        if isinstance(event.report_id, str) and event.report_id:
            return event.report_id
        payload = dict(event.payload or {})
        value = payload.get("report_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _legacy_event_action(event: TaskEvent) -> str:
        return event.action or "emit"

    @staticmethod
    def _legacy_event_target(event: TaskEvent) -> Optional[str]:
        return event.target

    def _is_canonical_legacy_report_alias(
        self,
        event: TaskEvent,
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
        return False

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
        if existing is not None and self._is_canonical_legacy_report_alias(
            existing,
            workspace_id=workspace_id,
            task_id=task_id,
            report_id=report_id,
        ):
            return existing
        return None

    def _reject_or_reuse_foreign_call_id(
        self,
        workspace_id: str,
        call_id: str,
        action: str,
        target: str,
        fingerprint: str,
    ) -> Optional[TaskEvent]:
        return None

    def consumer_cursor(self, workspace_id: str, consumer_key: str) -> int:
        if is_legacy_resident_consumer_key(consumer_key, workspace_id):
            raise ValueError(
                f"legacy resident consumer key {consumer_key!r} is not supported at runtime"
            )
        if not consumer_key.startswith(TASK_CONSUMER_PREFIX):
            raise ValueError(f"Mailbox consumer must be task:<task_id>; got {consumer_key!r}")
        task_id = consumer_key[len(TASK_CONSUMER_PREFIX) :]
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        return int(task.consumer_ack_sequence)

    def _set_consumer_cursor(self, workspace_id: str, consumer_key: str, sequence: int) -> None:
        if is_legacy_resident_consumer_key(consumer_key, workspace_id):
            raise ValueError(
                f"legacy resident consumer key {consumer_key!r} is not supported at runtime"
            )
        if not consumer_key.startswith(TASK_CONSUMER_PREFIX):
            raise ValueError(f"Mailbox consumer must be task:<task_id>; got {consumer_key!r}")
        task_id = consumer_key[len(TASK_CONSUMER_PREFIX) :]
        task = self._wm.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        self._wm.tasks[task_id] = task.model_copy(update={"consumer_ack_sequence": sequence})

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
        fingerprint = request_fingerprint(action, body)
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
        task = self._wm.tasks.get(event.task_id)
        if task is None:
            return
        self._wake_task_consumers(event, task)

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
        """Load Task events and rebuild the call index."""

        events: List[TaskEvent] = []
        for item in data.get("task_events") or []:
            if isinstance(item, dict):
                item = dict(item)
                item.pop("compat_run_id", None)
            events.append(TaskEvent(**item))
        events.sort(key=lambda event: event.sequence)
        self._events[workspace_id] = events
        self._rebuild_call_index(workspace_id)
        self._backfill_report_task_events(workspace_id)
        self._rebuild_call_index(workspace_id)
        self._next_seq[workspace_id] = self._workspace_max_sequence(workspace_id) + 1

    def purge_task_events(self, workspace_id: str, task_id: str) -> None:
        """Remove durable Task events and call-index entries for one Task."""

        remaining = [
            event for event in self._events.get(workspace_id, []) if event.task_id != task_id
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

    def _backfill_report_task_events(self, workspace_id: str) -> None:
        """Append missing AgentReports when cold-loading Task mailbox state."""

        events = list(self._events.get(workspace_id, []))
        covered_report_ids = {event.report_id for event in events if event.report_id}
        covered_call_ids = {event.call_id for event in events}
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
            if self._wm._is_fallback_reaper_report(report):
                review_cycle = int(report.review_cycle or task.review_cycle or 0)
                if state_policy.current_round_has_verdict(
                    task.review_cycle, task.reviewed_cycle
                ) or self._wm._review_cycle_has_reviewer_activity(task.id, review_cycle):
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
            fingerprint = request_fingerprint("report", body)
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
