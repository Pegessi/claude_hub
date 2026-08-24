"""Unit tests for Task Graph E2E per-event AgentReport resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_DIR = _REPO_ROOT / "scripts" / "task-graph-e2e"
sys.path.insert(0, str(_HARNESS_DIR))

import report_resolution as rr  # noqa: E402


def _event(
    *,
    sequence: int,
    event_type: str,
    report_id: str,
    actor_role: str,
    actor_session_id: str,
    task_id: str = "child-task",
    report_state: str | None = None,
) -> dict:
    payload = {
        "report_id": report_id,
        "task_id": task_id,
        "actor_role": actor_role,
        "actor_session_id": actor_session_id,
    }
    if report_state is not None:
        payload["report_state"] = report_state
    return {
        "sequence": sequence,
        "type": event_type,
        "task_id": task_id,
        "actor_role": actor_role,
        "actor_session_id": actor_session_id,
        "payload": payload,
    }


def test_assert_seq123_report_resolution_matches_agent_reports() -> None:
    child_id = "child-task"
    worker = "e2e-agent-1"
    reviewer = "e2e-reviewer-1"
    events = [
        _event(
            sequence=1,
            event_type="report",
            report_id="r1",
            actor_role="worker",
            actor_session_id=worker,
            report_state="ready_for_review",
        ),
        _event(
            sequence=2,
            event_type="review_started",
            report_id="r2",
            actor_role="reviewer",
            actor_session_id=reviewer,
            report_state="review_started",
        ),
        _event(
            sequence=3,
            event_type="review_passed",
            report_id="r3",
            actor_role="reviewer",
            actor_session_id=reviewer,
            report_state="review_passed",
        ),
    ]
    reports = [
        {
            "id": "r1",
            "task_id": child_id,
            "session_id": worker,
            "state": "ready_for_review",
            "call_id": "e2e-worker-report-1",
        },
        {
            "id": "r2",
            "task_id": child_id,
            "session_id": reviewer,
            "state": "review_started",
            "call_id": "report:r2",
        },
        {
            "id": "r3",
            "task_id": child_id,
            "session_id": reviewer,
            "state": "review_passed",
            "call_id": "report:r3",
        },
    ]
    payload = rr.assert_seq123_report_resolution(
        events,
        reports,
        child_task_id=child_id,
        worker_session_id=worker,
        reviewer_session_id=reviewer,
    )
    assert payload["all_resolved"] is True
    assert payload["seq1_worker_report"]["state"] == "ready_for_review"
    assert payload["seq2_review_started"]["session_id"] == reviewer
    assert payload["seq3_review_passed"]["report_id"] == "r3"


def test_resolve_target_event_report_fails_on_missing_agent_report() -> None:
    event = _event(
        sequence=1,
        event_type="report",
        report_id="missing",
        actor_role="worker",
        actor_session_id="e2e-agent-1",
    )
    with pytest.raises(AssertionError, match="not found in task reports API"):
        rr.resolve_target_event_report(
            event,
            [],
            child_task_id="child-task",
            worker_session_id="e2e-agent-1",
            reviewer_session_id="e2e-reviewer-1",
        )


def test_resolve_target_event_report_fails_on_state_mismatch() -> None:
    event = _event(
        sequence=2,
        event_type="review_started",
        report_id="r2",
        actor_role="reviewer",
        actor_session_id="e2e-reviewer-1",
        report_state="review_started",
    )
    reports = [
        {
            "id": "r2",
            "task_id": "child-task",
            "session_id": "e2e-reviewer-1",
            "state": "review_passed",
        }
    ]
    with pytest.raises(AssertionError, match="state expected 'review_started'"):
        rr.resolve_target_event_report(
            event,
            reports,
            child_task_id="child-task",
            worker_session_id="e2e-agent-1",
            reviewer_session_id="e2e-reviewer-1",
        )


def test_run_e2e_writes_report_resolution_evidence() -> None:
    source = (_HARNESS_DIR / "run_e2e.py").read_text(encoding="utf-8")
    assert "report_resolution" in source
    assert "target_event_report_resolution" in source
    assert "assert_seq123_report_resolution" in source
