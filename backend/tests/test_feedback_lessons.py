import json
from datetime import datetime
from pathlib import Path

import pytest

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    FeedbackSummaryMode,
    ReviewDecision,
)
from claude_hub.services.feedback_lessons import FeedbackLessonStore


@pytest.fixture
def store(tmp_path: Path) -> FeedbackLessonStore:
    return FeedbackLessonStore(tmp_path)


def _write_record(records_dir: Path, task_id: str, payload: dict) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{task_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_extract_named_value_stops_at_pipe(store: FeedbackLessonStore) -> None:
    text = "created_lesson_ids=foo,bar,baz | Each lesson cites >=1 evidence_task_id"
    assert store._extract_named_value(text, "created_lesson_ids") == "foo,bar,baz"


def test_extract_named_ids_drops_non_slug_tokens(store: FeedbackLessonStore) -> None:
    text = "created_lesson_ids=foo,bar,baz | Each lesson cites >=1 evidence_task_id"
    assert store._extract_named_ids(text, "created_lesson_ids") == [
        "foo",
        "bar",
        "baz",
    ]


def test_extract_named_ids_filters_pure_prose(store: FeedbackLessonStore) -> None:
    text = "created_lesson_ids=Each lesson cites evidence"
    assert store._extract_named_ids(text, "created_lesson_ids") == [
        "lesson",
        "cites",
        "evidence",
    ]


def test_summary_outcome_from_report_ignores_trailing_prose(
    store: FeedbackLessonStore,
) -> None:
    report = AgentReport(
        id="r1",
        workspace_id="ws",
        task_id="t1",
        session_id="s1",
        state=AgentReportState.COMPLETED,
        message="done",
        message_en="done",
        message_zh="完成",
        validation=(
            "created_lesson_ids=foo,bar,baz | "
            "Each lesson cites >=1 evidence_task_id from the input digest set"
        ),
        review_decision=ReviewDecision.SKIP,
        risk_level="system_audit",
        created_at=datetime(2026, 6, 7, 12, 0, 0),
    )
    created, merged, skipped = store._summary_outcome_from_report(report)
    assert created == ["foo", "bar", "baz"]
    assert merged == []
    assert skipped is None


def test_digest_preserves_chronological_state_sequence(
    store: FeedbackLessonStore,
) -> None:
    payload = {
        "task": {"id": "t1", "title": "x", "status": "done"},
        "reports": [
            {"state": "started"},
            {"state": "working"},
            {"state": "ready_for_review"},
            {"state": "review_failed"},
            {"state": "working"},
            {"state": "ready_for_review"},
            {"state": "review_failed"},
            {"state": "needs_input"},
            {"state": "completed"},
        ],
        "artifacts": {},
        "final_summary": "fixed",
    }
    digest = store._digest_task_record(payload)
    assert digest.report_state_sequence == [
        "started",
        "working",
        "ready_for_review",
        "review_failed",
        "working",
        "ready_for_review",
        "review_failed",
        "needs_input",
        "completed",
    ]
    assert digest.review_failed_count == 2
    assert digest.needs_input_count == 1
    assert digest.report_total == 9
    assert "review_failed" in digest.report_states  # deduped, kept for back-compat


def test_prepare_summary_input_consumes_all_unprocessed_in_incremental_mode(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    records_dir = tmp_path / "task_records"
    for index in range(20):
        _write_record(
            records_dir,
            f"2026-05-{index:02d}-task-{index:02d}",
            {
                "task": {"id": f"task-{index:02d}", "title": f"T{index}", "status": "done"},
                "reports": [{"state": "completed"}],
                "artifacts": {},
                "final_summary": "ok",
            },
        )

    result = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )

    assert len(result["input_record_ids"]) == 20
    assert result["processed_count"] == 20
    assert result["first_scan"] is True


def test_prepare_summary_input_caps_at_limit_for_full_mode(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    records_dir = tmp_path / "task_records"
    for index in range(12):
        _write_record(
            records_dir,
            f"2026-05-{index:02d}-task-{index:02d}",
            {
                "task": {"id": f"task-{index:02d}", "title": f"T{index}", "status": "done"},
                "reports": [{"state": "completed"}],
                "artifacts": {},
                "final_summary": "ok",
            },
        )

    result = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.FULL,
        limit=5,
        force=False,
    )

    assert len(result["input_record_ids"]) == 5


def test_prepare_summary_input_returns_only_new_records_on_subsequent_run(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    records_dir = tmp_path / "task_records"
    for index in range(3):
        _write_record(
            records_dir,
            f"2026-05-{index:02d}-task-{index:02d}",
            {
                "task": {"id": f"task-{index:02d}", "title": f"T{index}", "status": "done"},
                "reports": [{"state": "completed"}],
                "artifacts": {},
                "final_summary": "ok",
            },
        )

    first = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=50,
        force=False,
    )
    assert len(first["input_record_ids"]) == 3

    _write_record(
        records_dir,
        "2026-05-09-task-09",
        {
            "task": {"id": "task-09", "title": "T9", "status": "done"},
            "reports": [{"state": "completed"}],
            "artifacts": {},
            "final_summary": "ok",
        },
    )

    second = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=50,
        force=False,
    )
    assert second["input_record_ids"] == ["task-09"]
    assert second["processed_count"] == 4
    assert second["first_scan"] is False
