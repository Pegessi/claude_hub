import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    FeedbackLessonCreate,
    FeedbackLessonScope,
    FeedbackSummaryMode,
    ReviewDecision,
)
from claude_hub.services.feedback_lessons import (
    FeedbackLessonStore,
    FeedbackLessonValidationError,
)


def _write_iteration_record(
    state_root: Path,
    workspace_id: str,
    task_id: str,
    *,
    review_failed_count: int = 2,
    needs_input_count: int = 0,
) -> None:
    records_dir = state_root / workspace_id / "task_records"
    records_dir.mkdir(parents=True, exist_ok=True)
    states = (
        ["started", "working"]
        + ["review_failed"] * review_failed_count
        + ["needs_input"] * needs_input_count
        + ["completed"]
    )
    payload = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "task": {"id": task_id, "title": "fixture", "status": "done"},
        "session": {},
        "reports": [{"state": state} for state in states],
        "timeline": [],
        "artifacts": {"changed_files": [], "validation": [], "risks": []},
        "final_summary": "fixture",
    }
    (records_dir / f"2026-05-15T00-00-00-{task_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _make_payload(**overrides: Any) -> FeedbackLessonCreate:
    base: dict[str, Any] = {
        "summary": "Always commit before reporting.",
        "applies_when": ["any task delivering an MR"],
        "do": "Verify git push before reporting completion.",
        "avoid": "Do not call the task done while changes are local-only.",
        "tags": ["delivery"],
        "scope": FeedbackLessonScope.WORKSPACE,
        "evidence_task_ids": ["task-a"],
        "confidence": 0.9,
    }
    base.update(overrides)
    return FeedbackLessonCreate(**base)


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


def test_digest_preserves_iteration_counts_and_truncates_verbose_fields(
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
        "artifacts": {
            "changed_files": [f"file{i}.py" for i in range(25)],
            "validation": ["short validation", "x" * 600],
            "risks": ["short risk", "y" * 600, "z" * 600, "w" * 600, "v" * 600, "u" * 600],
        },
        "final_summary": "f" * 400,
    }
    digest = store._digest_task_record(payload)
    # report_state_sequence intentionally emptied to save prompt tokens;
    # counts carry the signal the Reaper needs.
    assert digest.report_state_sequence == []
    assert digest.review_failed_count == 2
    assert digest.needs_input_count == 1
    assert digest.report_total == 9
    assert "review_failed" in digest.report_states  # deduped, kept for back-compat
    # Truncation applied (_truncate_str honors max_len strictly: value[:n-3]+"..."):
    assert len(digest.final_summary) <= 200
    assert digest.final_summary.endswith("...")
    assert len(digest.changed_files) == 10
    assert len(digest.validation[1]) <= 200  # ≤ 200 chars strictly
    assert digest.validation[1].endswith("...")
    assert len(digest.risks) == 2  # capped to 2 items


def test_prepare_summary_input_caps_incremental_to_limit_to_prevent_oversized_prompts(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Incremental mode must also cap at limit to prevent unbounded prompt growth
    when many records accumulate between reaper runs (e.g. after long inactivity).
    Records beyond the cap stay unprocessed and are picked up on the next run."""
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

    # Capped to limit (5 most recent), NOT all 20
    assert len(result["input_record_ids"]) == 5
    assert result["input_record_ids"] == ["task-15", "task-16", "task-17", "task-18", "task-19"]
    assert result["processed_count"] == 5  # only the 5 selected are marked processed
    assert result["first_scan"] is True

    # Second run picks up the remaining unprocessed records
    result2 = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )
    assert len(result2["input_record_ids"]) == 5
    assert result2["processed_count"] == 10


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


def test_create_lesson_rejects_empty_required_fields(store: FeedbackLessonStore) -> None:
    workspace_id = "ws"
    with pytest.raises(FeedbackLessonValidationError, match="applies_when"):
        store.create_lesson(workspace_id, _make_payload(applies_when=[]))
    with pytest.raises(FeedbackLessonValidationError, match="^do is required"):
        store.create_lesson(workspace_id, _make_payload(do="  "))
    with pytest.raises(FeedbackLessonValidationError, match="^avoid is required"):
        store.create_lesson(workspace_id, _make_payload(avoid=""))
    with pytest.raises(FeedbackLessonValidationError, match="evidence_task_ids"):
        store.create_lesson(workspace_id, _make_payload(evidence_task_ids=[]))


def test_create_lesson_rejects_single_evidence_without_signal_a(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    _write_iteration_record(
        tmp_path, workspace_id, "task-a", review_failed_count=0, needs_input_count=0
    )
    with pytest.raises(FeedbackLessonValidationError, match="Signal A"):
        store.create_lesson(workspace_id, _make_payload(evidence_task_ids=["task-a"]))


def test_create_lesson_accepts_single_evidence_with_signal_a_and_caps_confidence(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    _write_iteration_record(tmp_path, workspace_id, "task-a", review_failed_count=2)
    lesson = store.create_lesson(
        workspace_id,
        _make_payload(evidence_task_ids=["task-a"], confidence=0.95),
    )
    assert lesson.confidence == 0.6


def test_create_lesson_rejects_multi_evidence_without_any_iteration(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    _write_iteration_record(tmp_path, workspace_id, "task-a", review_failed_count=0)
    _write_iteration_record(tmp_path, workspace_id, "task-b", review_failed_count=0)
    with pytest.raises(FeedbackLessonValidationError, match="multi-evidence"):
        store.create_lesson(
            workspace_id,
            _make_payload(evidence_task_ids=["task-a", "task-b"]),
        )


def test_create_lesson_accepts_multi_evidence_with_any_iteration_and_caps_confidence(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    _write_iteration_record(tmp_path, workspace_id, "task-a", review_failed_count=1)
    _write_iteration_record(tmp_path, workspace_id, "task-b", review_failed_count=0)
    lesson = store.create_lesson(
        workspace_id,
        _make_payload(evidence_task_ids=["task-a", "task-b"], confidence=0.95),
    )
    assert lesson.confidence == 0.85


def test_create_lesson_skips_signal_check_when_enforce_disabled(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    # No task_records on disk → would normally fail signal check
    lesson = store.create_lesson(
        workspace_id,
        _make_payload(evidence_task_ids=["unrecorded-task"], confidence=0.9),
        enforce_iteration_signal=False,
    )
    # Confidence cap still applied even when signal check is skipped
    assert lesson.confidence == 0.6


def test_create_lesson_rejects_single_evidence_with_missing_record(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    workspace_id = "ws"
    with pytest.raises(FeedbackLessonValidationError, match="no task record was found"):
        store.create_lesson(workspace_id, _make_payload(evidence_task_ids=["nonexistent"]))


def test_create_lesson_merges_deterministically_when_fingerprint_echoed(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Reaper can echo an existing fingerprint on POST; server must merge
    instead of creating a duplicate even when title/wording differs.
    Guards the active_lessons fingerprint-in-payload contract."""
    from claude_hub.models import FeedbackLessonCreate, FeedbackLessonScope

    workspace_id = "ws"
    _write_iteration_record(tmp_path, workspace_id, "task-a", review_failed_count=2)

    first = store.create_lesson(workspace_id, _make_payload(evidence_task_ids=["task-a"]))
    assert first.fingerprint
    # Reaper posts a semantically-identical lesson with different wording
    # but echoes the existing fingerprint. Must merge.
    second = store.create_lesson(
        workspace_id,
        FeedbackLessonCreate(
            summary="Always push commits before reporting DONE.",  # reworded
            applies_when=["any MR delivery"],
            do="Run git push before marking done.",
            avoid="Do not report done with unpushed changes.",
            tags=["delivery"],
            scope=FeedbackLessonScope.WORKSPACE,
            evidence_task_ids=["task-a"],
            confidence=0.6,
            fingerprint=first.fingerprint,
        ),
        enforce_iteration_signal=False,
    )
    assert second.id == first.id  # merged, not a new lesson
    assert "task-a" in second.evidence_task_ids
    assert len(store.list_lessons(workspace_id, include_inactive=True)) == 1


def test_prepare_summary_input_clamps_large_limit_to_max_digests(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Caller may pass limit up to 200 (backward compat); store internally
    clamps to _REAPER_MAX_DIGESTS_PER_RUN=30 to keep prompts bounded."""
    from claude_hub.models import FeedbackSummaryMode

    workspace_id = "ws"
    records_dir = tmp_path / "task_records"
    for index in range(60):
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
        limit=200,  # large but permitted by schema
        force=False,
    )
    assert len(result["input_record_ids"]) == 30  # clamped to cap
    assert result["limit"] == 30
    assert result["processed_count"] == 30
    result2 = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=200,
        force=False,
    )
    assert len(result2["input_record_ids"]) == 30
    assert result2["processed_count"] == 60


def test_feedback_summary_request_accepts_legacy_limit_range() -> None:
    """Schema must accept 1..200 (backward-compat); previously-valid
    limits 31..200 must NOT 422. Internal clamp (see previous test)
    enforces the 30-digest safety bound."""
    from claude_hub.models import FeedbackSummaryMode, FeedbackSummaryRequest

    req = FeedbackSummaryRequest(limit=200)
    assert req.limit == 200
    assert req.mode == FeedbackSummaryMode.INCREMENTAL
    assert FeedbackSummaryRequest().limit == 30
