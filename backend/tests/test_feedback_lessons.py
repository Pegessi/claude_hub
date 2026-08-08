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
    assert len(digest.changed_files) == 6  # capped to _DIGEST_MAX_CHANGED_FILES=6
    assert len(digest.validation[1]) <= 160  # ≤ _DIGEST_MAX_ITEM_CHARS strictly
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
    # Records are NOT marked processed until commit_summary_input() is called
    # by the caller (which happens after the prompt is built and budget checked).
    # Simulate commit (all 5 selected records made it into the prompt).
    committed_paths = [r.get("_path") for r in result["input_records"] if r.get("_path")]
    processed_count = store.commit_summary_input(result, committed_paths)
    assert processed_count == 5

    # Second run picks up the remaining unprocessed records
    result2 = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )
    assert len(result2["input_record_ids"]) == 5
    committed_paths2 = [r.get("_path") for r in result2["input_records"] if r.get("_path")]
    processed_count2 = store.commit_summary_input(result2, committed_paths2)
    assert processed_count2 == 10


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
    committed_paths = [r.get("_path") for r in first["input_records"] if r.get("_path")]
    store.commit_summary_input(first, committed_paths)

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
    committed_paths2 = [r.get("_path") for r in second["input_records"] if r.get("_path")]
    pc = store.commit_summary_input(second, committed_paths2)
    assert pc == 4
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


def test_create_lesson_rejects_unknown_client_fingerprint(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Client-provided fingerprints must reference an existing active lesson
    (echo-merge contract). A made-up fingerprint should 400 rather than create
    a lesson with a non-deterministic key that can never merge later."""
    from claude_hub.models import FeedbackLessonCreate, FeedbackLessonScope

    workspace_id = "ws"
    _write_iteration_record(tmp_path, workspace_id, "task-a", review_failed_count=2)

    with pytest.raises(FeedbackLessonValidationError, match="fingerprint"):
        store.create_lesson(
            workspace_id,
            FeedbackLessonCreate(
                summary="A lesson.",
                applies_when=["x"],
                do="y",
                avoid="z",
                tags=["t"],
                scope=FeedbackLessonScope.WORKSPACE,
                evidence_task_ids=["task-a"],
                confidence=0.6,
                fingerprint="workspace:deadbeefcafebabe",  # not an existing fp
            ),
            enforce_iteration_signal=False,
        )


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
    committed_paths = [r.get("_path") for r in result["input_records"] if r.get("_path")]
    pc = store.commit_summary_input(result, committed_paths)
    assert pc == 30
    result2 = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=200,
        force=False,
    )
    assert len(result2["input_record_ids"]) == 30
    committed_paths2 = [r.get("_path") for r in result2["input_records"] if r.get("_path")]
    pc2 = store.commit_summary_input(result2, committed_paths2)
    assert pc2 == 60


def test_feedback_summary_request_accepts_legacy_limit_range() -> None:
    """Schema must accept 1..200 (backward-compat); previously-valid
    limits 31..200 must NOT 422. Internal clamp (see previous test)
    enforces the 30-digest safety bound."""
    from claude_hub.models import FeedbackSummaryMode, FeedbackSummaryRequest

    req = FeedbackSummaryRequest(limit=200)
    assert req.limit == 200
    assert req.mode == FeedbackSummaryMode.INCREMENTAL
    assert FeedbackSummaryRequest().limit == 30


def test_compact_digest_for_prompt_bounds_every_free_text_field(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Adversarial: every free-text field in a task record is oversized;
    _compact_digest_for_prompt must bound each field so the serialized
    record fits in a small fixed envelope."""
    from claude_hub.services.feedback_lessons import (
        _DIGEST_MAX_CHANGED_FILES,
        _DIGEST_MAX_FINAL_SUMMARY,
        _DIGEST_MAX_ITEM_CHARS,
        _DIGEST_MAX_PATH_CHARS,
        _DIGEST_MAX_RISKS_ITEMS,
        _DIGEST_MAX_TITLE,
        _DIGEST_MAX_VALIDATION_ITEMS,
    )

    long_title = "T" * 5000
    long_summary = "S" * 5000
    long_item = "I" * 5000
    long_path = "very/long/path/" * 200 + "file.py"
    payload = {
        "task": {"id": "task-" + "x" * 500, "title": long_title, "status": "completed"},
        "reports": [{"state": "completed"}] * 5,
        "artifacts": {
            "changed_files": [long_path] * 50,
            "validation": [long_item] * 30,
            "risks": [long_item] * 30,
        },
        "final_summary": long_summary,
    }
    digest = store._digest_task_record(payload)
    compact = store._compact_digest_for_prompt(digest)
    assert len(compact["title"]) <= _DIGEST_MAX_TITLE
    assert len(compact["final_summary"]) <= _DIGEST_MAX_FINAL_SUMMARY
    assert len(compact["changed_files"]) <= _DIGEST_MAX_CHANGED_FILES
    for f in compact["changed_files"]:
        assert len(f) <= _DIGEST_MAX_PATH_CHARS
    assert len(compact["validation"]) <= _DIGEST_MAX_VALIDATION_ITEMS
    for v in compact["validation"]:
        assert len(v) <= _DIGEST_MAX_ITEM_CHARS
    assert len(compact["risks"]) <= _DIGEST_MAX_RISKS_ITEMS
    for r in compact["risks"]:
        assert len(r) <= _DIGEST_MAX_ITEM_CHARS
    # JSON-serialized size of one bounded digest must be a small constant.
    import json as _json

    one_digest_chars = len(_json.dumps(compact, ensure_ascii=False))
    # Conservative: one compacted adversarial digest < 3 KB even when every
    # input field is thousands of chars.
    assert one_digest_chars < 3_000, f"one digest = {one_digest_chars} chars"


def test_prepare_summary_input_clamps_outer_task_id(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """The outer compact-record wrapper and input_record_ids list must clamp
    task_id to _DIGEST_MAX_TASK_ID even when the raw record carries an oversized
    id (guards: feedback store loads from legacy caches, filename-fallback ids)."""
    from claude_hub.models import FeedbackSummaryMode
    from claude_hub.services.feedback_lessons import _DIGEST_MAX_TASK_ID

    workspace_id = "ws"
    records_dir = tmp_path / "task_records"
    long_id = "task-" + "x" * 500  # well over 64-char cap
    # Write with a SHORT filename (OS filename length limit) but with the long
    # id INSIDE the JSON payload, simulating a record that was written before
    # task_id clamping was added.
    _write_record(
        records_dir,
        "2026-06-07T12-00-00-task-long.json",
        {
            "task": {"id": long_id, "title": "T", "status": "done"},
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
    assert len(result["input_record_ids"]) == 1
    outer_tid = result["input_record_ids"][0]
    assert (
        len(outer_tid) <= _DIGEST_MAX_TASK_ID
    ), f"outer task_id not clamped: {len(outer_tid)} > {_DIGEST_MAX_TASK_ID}"
    assert outer_tid.endswith("...")
    # Also clamp inside the digest field itself.
    wrapper_tid = result["input_records"][0]["task_id"]
    assert len(wrapper_tid) <= _DIGEST_MAX_TASK_ID
    digest_tid = result["input_records"][0]["digest"]["task_id"]
    assert len(digest_tid) <= _DIGEST_MAX_TASK_ID
    # And the internal _path bookkeeping uses the file path, not the id.
    assert result["input_records"][0]["_path"].endswith(".json")


def test_budget_loop_drops_oldest_and_carry_over_works(
    store: FeedbackLessonStore, tmp_path: Path
) -> None:
    """Through prepare_summary_input + _build_workspace_feedback_summary_prompt
    + commit_summary_input: when the global budget trims digests, dropped
    records are NOT marked processed and are picked up on the next run
    (carry-over). Also asserts the strict budget loop can drop all the way
    to zero digests."""
    import types

    from claude_hub.models import FeedbackSummaryMode
    from claude_hub.services.workspace_manager._feedback import _FeedbackMixin

    workspace_id = "ws-budget"
    records_dir = tmp_path / "task_records"

    long_summary = "S" * 500
    n_tasks = 10
    for i in range(n_tasks):
        tid = f"task-{i:03d}"
        path = records_dir / f"2026-06-07T12-{i:02d}-00-{tid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": workspace_id,
                    "task": {
                        "id": tid,
                        "title": f"Title {i} " + ("x" * 200),
                        "status": "done",
                    },
                    "reports": [{"state": "completed"}],
                    "artifacts": {
                        "changed_files": ["some/long/path/" + ("f" * 80) + ".py"] * 6,
                        "validation": [long_summary, long_summary],
                        "risks": [long_summary, long_summary],
                    },
                    "final_summary": long_summary,
                }
            ),
            encoding="utf-8",
        )

    ws_stub = types.SimpleNamespace(id=workspace_id)
    mixin = _FeedbackMixin()
    mixin._feedback_store = lambda: store  # type: ignore[attr-defined]
    mixin.feedback_lessons = (  # type: ignore[attr-defined]
        lambda _wid, *, query="", limit=20, include_inactive=False: []
    )

    # Tight budget to force dropping.
    store.REAPER_PROMPT_HARD_CHAR_LIMIT = 5_000

    result = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=30,
        force=False,
    )
    assert len(result["input_records"]) == n_tasks
    prompt, committed_ids, committed_paths = mixin._build_workspace_feedback_summary_prompt(
        ws_stub, result
    )
    assert len(prompt) <= 5_000, f"prompt {len(prompt)} exceeds 5000"
    assert len(committed_ids) < n_tasks, "budget did not drop any digests"
    assert len(committed_ids) == len(committed_paths)
    assert "_path" not in prompt
    all_paths = {r["_path"] for r in result["input_records"]}
    assert set(committed_paths) <= all_paths
    for tid in committed_ids:
        assert int(tid.split("-")[1]) >= n_tasks - len(committed_ids)

    pc = store.commit_summary_input(result, committed_paths)
    assert pc == len(committed_paths)

    result2 = store.prepare_summary_input(
        workspace_id,
        records_dir,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=30,
        force=False,
    )
    ids2 = set(result2["input_record_ids"])
    ids1 = set(committed_ids)
    assert ids2, "second run got no records; carry-over broken"
    assert not (ids1 & ids2), f"carry-over leaked already-committed ids: {ids1 & ids2}"
    idx1 = sorted(int(t.split("-")[1]) for t in ids1)
    idx2 = sorted(int(t.split("-")[1]) for t in ids2)
    assert max(idx2) < min(idx1), f"wrong ordering: {idx1} vs {idx2}"

    # Strict-budget edge case: budget smaller than preamble → zero digests,
    # no records marked processed, all carried over to next run.
    store.REAPER_PROMPT_HARD_CHAR_LIMIT = 100
    records_dir2 = tmp_path / "task_records2"
    records_dir2.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        tid = f"t2-{i:03d}"
        (records_dir2 / f"2026-06-07T13-{i:02d}-00-{tid}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": workspace_id,
                    "task": {"id": tid, "title": "T", "status": "done"},
                    "reports": [{"state": "completed"}],
                    "artifacts": {},
                    "final_summary": "ok",
                }
            ),
            encoding="utf-8",
        )
    result3 = store.prepare_summary_input(
        workspace_id,
        records_dir2,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=30,
        force=False,
    )
    assert len(result3["input_records"]) == 3
    prompt3, ids3, paths3 = mixin._build_workspace_feedback_summary_prompt(ws_stub, result3)
    assert ids3 == []
    assert paths3 == []
    pkg = json.loads(prompt3.split("Input package JSON:\n", 1)[1])
    assert pkg["input_task_digests"] == []
    store.commit_summary_input(result3, [])
    store.REAPER_PROMPT_HARD_CHAR_LIMIT = 100_000
    result4 = store.prepare_summary_input(
        workspace_id,
        records_dir2,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=30,
        force=False,
    )
    assert (
        len(result4["input_record_ids"]) == 3
    ), f"expected 3 records carried over, got {len(result4['input_record_ids'])}"
