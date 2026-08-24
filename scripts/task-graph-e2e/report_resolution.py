"""Per-event AgentReport resolution helpers for Task Graph strict E2E."""

from __future__ import annotations

from typing import Any

VERDICT_EVENT_TYPES = frozenset(
    {"review_passed", "review_failed", "review_needs_input"}
)


def event_report_id(event: dict[str, Any]) -> str:
    """Return the report_id carried by a TaskEvent payload."""

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    report_id = event.get("report_id") or payload.get("report_id")
    if not report_id:
        raise AssertionError(f"TaskEvent missing report_id: {event!r}")
    return str(report_id)


def expected_agent_report_state(event: dict[str, Any]) -> str:
    """Map a target TaskEvent to the persisted AgentReport.state expectation."""

    event_type = str(event.get("type") or "").lower()
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload_state = str(
        payload.get("state") or payload.get("report_state") or ""
    ).lower()
    if event_type == "report":
        return payload_state or "ready_for_review"
    if event_type == "review_started":
        return "review_started"
    if event_type in VERDICT_EVENT_TYPES:
        return event_type
    if payload_state in VERDICT_EVENT_TYPES:
        return payload_state
    raise AssertionError(
        f"unsupported target event type for report resolution: {event!r}"
    )


def expected_actor_session_id(
    event: dict[str, Any],
    *,
    worker_session_id: str,
    reviewer_session_id: str,
) -> str:
    role = str(event.get("actor_role") or "").lower()
    if role == "worker":
        return worker_session_id
    if role == "reviewer":
        return reviewer_session_id
    raise AssertionError(f"unsupported actor_role for report resolution: {event!r}")


def find_agent_report_by_id(
    reports: list[dict[str, Any]], report_id: str
) -> dict[str, Any] | None:
    for report in reports:
        if str(report.get("id") or "") == report_id:
            return report
    return None


def resolve_target_event_report(
    event: dict[str, Any],
    reports: list[dict[str, Any]],
    *,
    child_task_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> dict[str, Any]:
    """Resolve one target TaskEvent to a persisted AgentReport row."""

    report_id = event_report_id(event)
    agent_report = find_agent_report_by_id(reports, report_id)
    if agent_report is None:
        raise AssertionError(
            f"seq {event.get('sequence')}: AgentReport {report_id!r} "
            "not found in task reports API"
        )

    expected_task_id = child_task_id
    expected_session_id = expected_actor_session_id(
        event,
        worker_session_id=worker_session_id,
        reviewer_session_id=reviewer_session_id,
    )
    expected_state = expected_agent_report_state(event)

    actual_task_id = agent_report.get("task_id")
    actual_session_id = agent_report.get("session_id")
    actual_state = str(agent_report.get("state") or "").lower()

    mismatches: list[str] = []
    if actual_task_id != expected_task_id:
        mismatches.append(
            f"task_id expected {expected_task_id!r} got {actual_task_id!r}"
        )
    if actual_session_id != expected_session_id:
        mismatches.append(
            f"session_id expected {expected_session_id!r} got {actual_session_id!r}"
        )
    if actual_state != expected_state:
        mismatches.append(f"state expected {expected_state!r} got {actual_state!r}")
    if mismatches:
        raise AssertionError(
            f"seq {event.get('sequence')} report {report_id!r} mismatch: "
            + "; ".join(mismatches)
        )

    return {
        "sequence": int(event["sequence"]),
        "type": event.get("type"),
        "report_id": report_id,
        "task_id": actual_task_id,
        "session_id": actual_session_id,
        "state": actual_state,
        "actor_session_id": event.get("actor_session_id"),
        "actor_role": event.get("actor_role"),
        "call_id": agent_report.get("call_id"),
        "resolved": True,
        "agent_report_snapshot": {
            "id": agent_report.get("id"),
            "task_id": agent_report.get("task_id"),
            "session_id": agent_report.get("session_id"),
            "state": agent_report.get("state"),
            "call_id": agent_report.get("call_id"),
        },
    }


def assert_seq123_report_resolution(
    events: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    *,
    child_task_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> dict[str, Any]:
    """Assert seq1/2/3 target events each resolve to a matching AgentReport."""

    ordered = sorted(events, key=lambda item: int(item["sequence"]))
    if len(ordered) != 3:
        raise AssertionError(
            f"expected exactly 3 target events for report resolution, got {len(ordered)}"
        )
    sequences = [int(event["sequence"]) for event in ordered]
    if sequences != [1, 2, 3]:
        raise AssertionError(f"expected target sequences [1,2,3], got {sequences}")

    resolutions = [
        resolve_target_event_report(
            event,
            reports,
            child_task_id=child_task_id,
            worker_session_id=worker_session_id,
            reviewer_session_id=reviewer_session_id,
        )
        for event in ordered
    ]
    by_sequence = {str(item["sequence"]): item for item in resolutions}
    return {
        "records": resolutions,
        "by_sequence": by_sequence,
        "seq1_worker_report": by_sequence["1"],
        "seq2_review_started": by_sequence["2"],
        "seq3_review_passed": by_sequence["3"],
        "all_resolved": True,
    }
