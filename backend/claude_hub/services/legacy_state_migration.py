"""One-time raw state.json migration: Agent Tree → Task Graph only.

Migration is load-only: production runtime never reads ``agent_runs`` /
``agent_events`` / ``agent_run_id`` after the first successful migrate+save.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple

from ..models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from .request_fingerprint import request_fingerprint
from .task_graph import make_task_consumer_key

logger = logging.getLogger(__name__)

LEGACY_ORCHESTRATION_KEYS = frozenset({"agent_runs", "agent_events"})
LEGACY_TASK_KEYS = frozenset({"agent_run_id"})
LEGACY_INDEX_KEYS = frozenset({"resident_ack_sequence"})

_AGENT_TYPE_TO_TASK: Dict[str, TaskEventType] = {
    "dispatched": TaskEventType.DISPATCHED,
    "started": TaskEventType.STARTED,
    "progress": TaskEventType.PROGRESS,
    "heartbeat": TaskEventType.PROGRESS,
    "message": TaskEventType.MESSAGE,
    "tool_wait": TaskEventType.PROGRESS,
    "approval_required": TaskEventType.NEEDS_INPUT,
    "blocked": TaskEventType.NEEDS_INPUT,
    "failed": TaskEventType.FAILED,
    "completed": TaskEventType.COMPLETED,
    "interrupted": TaskEventType.INTERRUPT,
}


@dataclass
class _LegacyRun:
    id: str
    workspace_id: str
    parent_id: Optional[str]
    path: str
    supervisor_id: Optional[str]
    executor_kind: str
    context_ref: Optional[str]
    ack_sequence: int = 0


@dataclass
class MigrationResult:
    state: Dict[str, Any]
    migrated: bool
    discarded_runs: List[str] = field(default_factory=list)
    discarded_events: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def state_has_legacy_orchestration_keys(raw: Mapping[str, Any]) -> bool:
    if LEGACY_ORCHESTRATION_KEYS & set(raw.keys()):
        return True
    for item in raw.get("tasks") or []:
        if isinstance(item, dict) and LEGACY_TASK_KEYS & set(item.keys()):
            value = item.get("agent_run_id")
            if value is not None:
                return True
    for item in raw.get("task_events") or []:
        if isinstance(item, dict) and item.get("compat_run_id"):
            return True
    return False


def index_has_legacy_keys(item: Mapping[str, Any]) -> bool:
    return bool(LEGACY_INDEX_KEYS & set(item.keys()))


def migrate_raw_workspace_state(
    *,
    workspace_id: str,
    raw: MutableMapping[str, Any],
    index_item: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> MigrationResult:
    """Migrate one workspace state blob. Does not mutate the input dict."""

    if not state_has_legacy_orchestration_keys(raw) and (
        index_item is None or not index_has_legacy_keys(index_item)
    ):
        cleaned = _strip_residual_legacy_fields(copy.deepcopy(dict(raw)))
        return MigrationResult(state=cleaned, migrated=False)

    working = copy.deepcopy(dict(raw))
    runs = _parse_runs(working.get("agent_runs") or [], workspace_id)
    tasks = _parse_tasks(working.get("tasks") or [], workspace_id)
    reports = {
        str(item["id"]): item
        for item in (working.get("reports") or [])
        if isinstance(item, dict) and item.get("id")
    }
    task_events = [
        TaskEvent(**item)
        for item in (working.get("task_events") or [])
        if isinstance(item, dict) and item.get("call_id")
    ]
    agent_events = list(working.get("agent_events") or [])

    discarded_runs: List[str] = []
    discarded_events: List[str] = []
    warnings: List[str] = []

    missing_resident_ack = index_item is not None and "resident_ack_sequence" not in index_item
    legacy_resident_ack = (
        int(index_item.get("resident_ack_sequence", 0)) if index_item is not None else 0
    )

    _inherit_task_parents(tasks, runs, workspace_id, discarded_runs, warnings)
    _backfill_task_run_links(tasks, runs, workspace_id)
    _lift_task_ack_cursors(
        tasks,
        runs,
        workspace_id,
        missing_resident_ack=missing_resident_ack,
        legacy_resident_ack=legacy_resident_ack,
    )

    known_by_call = {event.call_id: event for event in task_events}
    report_ids = {event.report_id for event in task_events if event.report_id}
    projected, event_discards = _project_agent_events(
        workspace_id=workspace_id,
        agent_events=agent_events,
        tasks=tasks,
        runs=runs,
        reports=reports,
        known_by_call=known_by_call,
        report_ids=report_ids,
    )
    discarded_events.extend(event_discards)
    if projected:
        task_events.extend(projected)
        task_events.sort(key=lambda event: event.sequence)

    backfilled = _backfill_report_events(
        workspace_id=workspace_id,
        tasks=tasks,
        reports=reports,
        existing_events=task_events,
        now=now,
    )
    if backfilled:
        task_events.extend(backfilled)
        task_events.sort(key=lambda event: event.sequence)

    for run in runs.values():
        if run.executor_kind == "resident_root":
            discarded_runs.append(run.id)

    for task in tasks.values():
        task.pop("agent_run_id", None)

    working["tasks"] = list(tasks.values())
    working["task_events"] = [_task_event_to_dict(event) for event in task_events]
    for key in LEGACY_ORCHESTRATION_KEYS:
        working.pop(key, None)

    if index_item is not None:
        index_item.pop("resident_ack_sequence", None)

    cleaned = _strip_residual_legacy_fields(working)
    return MigrationResult(
        state=cleaned,
        migrated=True,
        discarded_runs=discarded_runs,
        discarded_events=discarded_events,
        warnings=warnings,
    )


def _strip_residual_legacy_fields(state: Dict[str, Any]) -> Dict[str, Any]:
    for key in LEGACY_ORCHESTRATION_KEYS:
        state.pop(key, None)
    for item in state.get("tasks") or []:
        if isinstance(item, dict):
            item.pop("agent_run_id", None)
    for item in state.get("task_events") or []:
        if isinstance(item, dict):
            item.pop("compat_run_id", None)
    return state


def _parse_runs(items: Iterable[Any], workspace_id: str) -> Dict[str, _LegacyRun]:
    runs: Dict[str, _LegacyRun] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if str(item.get("workspace_id")) != workspace_id:
            continue
        runs[str(item["id"])] = _LegacyRun(
            id=str(item["id"]),
            workspace_id=workspace_id,
            parent_id=item.get("parent_id"),
            path=str(item.get("path") or item["id"]),
            supervisor_id=item.get("supervisor_id"),
            executor_kind=str(item.get("executor_kind") or "managed_task"),
            context_ref=item.get("context_ref"),
            ack_sequence=int(item.get("ack_sequence") or 0),
        )
    return runs


def _parse_tasks(items: Iterable[Any], workspace_id: str) -> Dict[str, Dict[str, Any]]:
    tasks: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if str(item.get("workspace_id")) != workspace_id:
            continue
        tasks[str(item["id"])] = dict(item)
    return tasks


def _linked_run_for_task(task: Dict[str, Any], runs: Dict[str, _LegacyRun]) -> Optional[_LegacyRun]:
    run_id = task.get("agent_run_id")
    if run_id:
        return runs.get(str(run_id))
    matches = [
        run
        for run in runs.values()
        if run.context_ref == task.get("id") and run.executor_kind == "managed_task"
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _task_for_run(
    run: _LegacyRun,
    tasks: Dict[str, Dict[str, Any]],
    workspace_id: str,
) -> Optional[Dict[str, Any]]:
    linked = [
        task
        for task in tasks.values()
        if task.get("workspace_id") == workspace_id and task.get("agent_run_id") == run.id
    ]
    if len(linked) == 1:
        return linked[0]
    if len(linked) > 1:
        return None
    if not run.context_ref:
        return None
    hinted = tasks.get(str(run.context_ref))
    if hinted is None or hinted.get("workspace_id") != workspace_id:
        return None
    if hinted.get("agent_run_id") not in (None, run.id):
        return None
    return hinted


def _inherit_task_parents(
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
    workspace_id: str,
    discarded_runs: List[str],
    warnings: List[str],
) -> None:
    for task in tasks.values():
        if task.get("workspace_id") != workspace_id:
            continue
        if "parent_task_id" in task:
            continue
        run = _linked_run_for_task(task, runs)
        if run is None or not run.parent_id:
            continue
        parent_run = runs.get(run.parent_id)
        if parent_run is None or parent_run.executor_kind == "resident_root":
            if parent_run is not None and parent_run.executor_kind == "resident_root":
                discarded_runs.append(parent_run.id)
            continue
        parent_task = _task_for_run(parent_run, tasks, workspace_id)
        if parent_task is None or parent_task.get("id") == task.get("id"):
            warnings.append(
                f"skip parent inherit task_id={task.get('id')} ambiguous parent run={run.parent_id}"
            )
            continue
        task["parent_task_id"] = parent_task["id"]


def _backfill_task_run_links(
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
    workspace_id: str,
) -> None:
    for run in runs.values():
        if run.workspace_id != workspace_id or run.executor_kind != "managed_task":
            continue
        if not run.context_ref:
            continue
        if any(
            task.get("workspace_id") == workspace_id and task.get("agent_run_id") == run.id
            for task in tasks.values()
        ):
            continue
        hinted = tasks.get(run.context_ref)
        if hinted is None or hinted.get("workspace_id") != workspace_id:
            continue
        if hinted.get("agent_run_id") is not None:
            continue
        hinted["agent_run_id"] = run.id


def _lift_task_ack_cursors(
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
    workspace_id: str,
    *,
    missing_resident_ack: bool,
    legacy_resident_ack: int,
) -> None:
    lifted_resident_ack = int(legacy_resident_ack)
    for run in runs.values():
        if run.workspace_id != workspace_id:
            continue
        if run.executor_kind == "resident_root":
            if missing_resident_ack:
                lifted_resident_ack = max(lifted_resident_ack, run.ack_sequence)
            continue
        if run.executor_kind != "managed_task":
            continue
        linked = _task_for_run(run, tasks, workspace_id)
        if linked is None or "consumer_ack_sequence" in linked:
            continue
        current = int(linked.get("consumer_ack_sequence") or 0)
        linked["consumer_ack_sequence"] = max(current, run.ack_sequence)

    if lifted_resident_ack > 0:
        for task in tasks.values():
            if task.get("workspace_id") != workspace_id:
                continue
            if task.get("parent_task_id"):
                continue
            current = int(task.get("consumer_ack_sequence") or 0)
            task["consumer_ack_sequence"] = max(current, lifted_resident_ack)


def _resolve_legacy_task(
    workspace_id: str,
    agent_event: Mapping[str, Any],
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
) -> Optional[Dict[str, Any]]:
    payload = dict(agent_event.get("payload") or {})
    payload_task_id = payload.get("task_id")
    if isinstance(payload_task_id, str) and payload_task_id in tasks:
        found = tasks[payload_task_id]
        if found.get("workspace_id") == workspace_id:
            return found
    for run_id in (agent_event.get("agent_run_id"), agent_event.get("author")):
        if not isinstance(run_id, str):
            continue
        run = runs.get(run_id)
        if run is not None:
            task = _task_for_run(run, tasks, workspace_id)
            if task is not None:
                return task
        if run_id in tasks and tasks[run_id].get("workspace_id") == workspace_id:
            return tasks[run_id]
    return None


def _legacy_consumer_key(
    workspace_id: str,
    agent_event: Mapping[str, Any],
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
) -> Optional[str]:
    recipient = agent_event.get("recipient")
    if not isinstance(recipient, str) or not recipient:
        return None
    projected = _resolve_legacy_task(workspace_id, agent_event, tasks, runs)
    if projected is not None and recipient == projected.get("id"):
        return _supervisor_consumer_key_for_task_dict(projected, tasks)
    if recipient.startswith("task:"):
        task_id = recipient[len("task:") :]
        if task_id in tasks:
            return make_task_consumer_key(task_id)
    if projected is not None and recipient in tasks:
        return make_task_consumer_key(recipient)
    run = runs.get(recipient)
    if run is not None and run.executor_kind == "resident_root":
        if projected is not None:
            return _supervisor_consumer_key_for_task_dict(projected, tasks)
        return None
    if projected is not None:
        linked = _linked_run_for_task(projected, runs)
        if linked is not None and recipient == linked.parent_id:
            return _supervisor_consumer_key_for_task_dict(projected, tasks)
    return None


def _supervisor_consumer_key_for_task_dict(
    task: Dict[str, Any],
    tasks: Dict[str, Dict[str, Any]],
) -> str:
    parent_id = task.get("parent_task_id")
    if isinstance(parent_id, str) and parent_id in tasks:
        return make_task_consumer_key(parent_id)
    return make_task_consumer_key(str(task["id"]))


def _legacy_actor_fields(event: Mapping[str, Any]) -> Tuple[Optional[str], TaskActorRole]:
    payload = dict(event.get("payload") or {})
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


def _project_agent_events(
    *,
    workspace_id: str,
    agent_events: List[Any],
    tasks: Dict[str, Dict[str, Any]],
    runs: Dict[str, _LegacyRun],
    reports: Dict[str, Dict[str, Any]],
    known_by_call: Dict[str, TaskEvent],
    report_ids: set[str],
) -> Tuple[List[TaskEvent], List[str]]:
    projected: List[TaskEvent] = []
    discarded: List[str] = []
    for raw in agent_events:
        if not isinstance(raw, dict):
            continue
        call_id = str(raw.get("call_id") or "")
        if not call_id:
            discarded.append("<missing-call-id>")
            continue
        payload = dict(raw.get("payload") or {})
        report_id = payload.get("report_id")
        action = raw.get("action") or "emit"
        target = raw.get("target") or raw.get("agent_run_id") or ""
        fingerprint = raw.get("fingerprint") or request_fingerprint(
            action,
            {
                "sequence": raw.get("sequence"),
                "call_id": call_id,
                "agent_run_id": raw.get("agent_run_id"),
                "event_type": raw.get("type"),
                "author": raw.get("author"),
                "recipient": raw.get("recipient"),
            },
        )
        existing = known_by_call.get(call_id)
        if existing is not None:
            if (
                existing.action != action
                or existing.target != target
                or existing.fingerprint != fingerprint
            ):
                raise ValueError(
                    f"conflicting duplicate call_id {call_id!r} during legacy migration "
                    f"workspace={workspace_id}"
                )
            continue
        if isinstance(report_id, str) and report_id in report_ids:
            continue
        task = _resolve_legacy_task(workspace_id, raw, tasks, runs)
        if task is None:
            discarded.append(call_id)
            continue
        consumer_key = _legacy_consumer_key(workspace_id, raw, tasks, runs)
        if consumer_key is None:
            discarded.append(call_id)
            continue
        stored = reports.get(str(report_id)) if isinstance(report_id, str) else None
        if stored is not None and stored.get("task_id") == task.get("id"):
            actor_session_id = stored.get("session_id")
            actor_role = TaskActorRole.WORKER
            review_cycle = stored.get("review_cycle")
            event_type = TaskEventType.REPORT
            report_id_value = stored.get("id")
        else:
            actor_session_id, actor_role = _legacy_actor_fields(raw)
            raw_cycle = payload.get("review_cycle")
            review_cycle = int(raw_cycle) if raw_cycle is not None else None
            event_type = _AGENT_TYPE_TO_TASK.get(str(raw.get("type") or ""), TaskEventType.PROGRESS)
            report_id_value = report_id if isinstance(report_id, str) else None
        event = TaskEvent(
            sequence=int(raw.get("sequence") or 0),
            call_id=call_id,
            fingerprint=str(fingerprint),
            task_id=str(task["id"]),
            actor_session_id=actor_session_id if isinstance(actor_session_id, str) else None,
            actor_role=actor_role,
            review_cycle=int(review_cycle) if review_cycle is not None else None,
            type=event_type,
            action=str(action),
            target=str(target),
            consumer_key=consumer_key,
            payload=payload,
            created_at=raw.get("created_at") or datetime.utcnow(),
            report_id=str(report_id_value) if report_id_value else None,
        )
        projected.append(event)
        known_by_call[call_id] = event
        if event.report_id:
            report_ids.add(event.report_id)
    return projected, discarded


def _backfill_report_events(
    *,
    workspace_id: str,
    tasks: Dict[str, Dict[str, Any]],
    reports: Dict[str, Dict[str, Any]],
    existing_events: List[TaskEvent],
    now: Optional[datetime],
) -> List[TaskEvent]:
    covered_report_ids = {event.report_id for event in existing_events if event.report_id}
    covered_call_ids = {event.call_id for event in existing_events}
    added: List[TaskEvent] = []
    stamp = now or datetime.utcnow()
    max_sequence = max((event.sequence for event in existing_events), default=0)
    for report in sorted(
        reports.values(), key=lambda item: (item.get("created_at"), item.get("id"))
    ):
        task_id = report.get("task_id")
        if not isinstance(task_id, str) or task_id not in tasks:
            continue
        task = tasks[task_id]
        if task.get("workspace_id") != workspace_id:
            continue
        report_id = str(report.get("id"))
        call_id = f"report:{report_id}"
        if report_id in covered_report_ids or call_id in covered_call_ids:
            continue
        if report.get("call_id") and report["call_id"] in covered_call_ids:
            continue
        max_sequence += 1
        consumer_key = _supervisor_consumer_key_for_task_dict(task, tasks)
        payload = {
            "report_id": report_id,
            "task_id": task_id,
            "actor_session_id": report.get("session_id"),
            "actor_role": TaskActorRole.WORKER.value,
            "review_cycle": report.get("review_cycle"),
        }
        fingerprint = request_fingerprint("report", payload)
        event = TaskEvent(
            sequence=max_sequence,
            call_id=call_id,
            fingerprint=fingerprint,
            task_id=task_id,
            actor_session_id=report.get("session_id"),
            actor_role=TaskActorRole.WORKER,
            review_cycle=report.get("review_cycle"),
            type=TaskEventType.REPORT,
            action="report",
            target=task_id,
            consumer_key=consumer_key,
            payload=payload,
            created_at=report.get("created_at") or stamp,
            report_id=report_id,
        )
        added.append(event)
        covered_report_ids.add(report_id)
        covered_call_ids.add(call_id)
    return added


def _task_event_to_dict(event: TaskEvent) -> Dict[str, Any]:
    data = event.model_dump(mode="json")
    data.pop("compat_run_id", None)
    return data
