"""Feedback must retain actual failed-report evidence and verify every citation."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from claude_hub.models import (
    FeedbackLessonCreate,
    FeedbackSummaryMode,
    FeedbackTaskDigest,
    Workspace,
)
from claude_hub.services.feedback_lessons import FeedbackLessonStore, FeedbackLessonValidationError
from claude_hub.services.workspace_manager import WorkspaceManager


def _record(root: Path, task_id: str, *, workspace_id: str = "ws") -> Path:
    records = root / workspace_id / "task_records"
    records.mkdir(parents=True, exist_ok=True)
    path = records / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "workspace_id": workspace_id,
                "task": {"id": task_id, "title": "Fix review", "status": "done"},
                "reports": [
                    {
                        "id": "report-failed",
                        "state": "review_failed",
                        "message": "wrong checkout",
                        "validation": "pwd returned /repo/main; expected /worktrees/fix",
                        "risks": "feature diff was never reviewed",
                        "env": {"TOKEN": "must-not-copy"},
                    },
                    {"id": "report-success", "state": "completed", "message": "done"},
                ],
                "artifacts": {"validation": ["reviewed feature branch"]},
                "final_summary": "All tests passed",
            }
        )
    )
    return path


def _lesson(ids: list[str]) -> FeedbackLessonCreate:
    return FeedbackLessonCreate(
        summary="Review the worker checkout",
        applies_when=["feature worktree"],
        do="Check pwd before reviewing",
        avoid="Assuming the main checkout contains the fix",
        evidence_task_ids=ids,
        confidence=0.95,
    )


@pytest.mark.parametrize(
    "bad_record", ["missing", "other-workspace", "malformed", "not-object", "bad-reports"]
)
def test_every_automatic_lesson_citation_requires_local_record(
    tmp_path: Path, bad_record: str
) -> None:
    store = FeedbackLessonStore(tmp_path)
    _record(tmp_path, "real")
    if bad_record == "other-workspace":
        _record(tmp_path, "unsupported", workspace_id="other")
    elif bad_record != "missing":
        path = _record(tmp_path, "unsupported")
        path.write_text(
            {
                "malformed": "{bad",
                "not-object": "[]",
                "bad-reports": '{"task":{"id":"unsupported"},"reports":42}',
            }[bad_record]
        )

    with pytest.raises(FeedbackLessonValidationError, match="unsupported"):
        store.create_lesson("ws", _lesson(["real", "unsupported"]))
    assert store.list_lessons("ws") == []


def test_copied_foreign_record_does_not_verify_citation(tmp_path: Path) -> None:
    store = FeedbackLessonStore(tmp_path)
    _record(tmp_path, "real")
    foreign = _record(tmp_path, "foreign", workspace_id="other")
    (tmp_path / "ws/task_records/foreign.json").write_text(foreign.read_text())
    with pytest.raises(FeedbackLessonValidationError, match="foreign"):
        store.create_lesson("ws", _lesson(["real", "foreign"]))


def test_legacy_record_rejects_conflicting_task_workspace(tmp_path: Path) -> None:
    store = FeedbackLessonStore(tmp_path)
    path = _record(tmp_path, "foreign")
    payload = json.loads(path.read_text())
    del payload["workspace_id"]
    payload["task"]["workspace_id"] = "other"
    path.write_text(json.dumps(payload))
    with pytest.raises(FeedbackLessonValidationError, match="foreign"):
        store.create_lesson("ws", _lesson(["foreign"]))


@pytest.mark.parametrize("task_id", ["/absolute", "../outside", "**/*", "[real]", "*"])
def test_evidence_ids_are_not_interpreted_as_paths_or_globs(tmp_path: Path, task_id: str) -> None:
    store = FeedbackLessonStore(tmp_path)
    _record(tmp_path, "real")
    with pytest.raises(FeedbackLessonValidationError, match="no task record"):
        store.create_lesson("ws", _lesson([task_id]))


def test_manual_confirmation_preserves_unarchived_evidence_support(tmp_path: Path) -> None:
    store = FeedbackLessonStore(tmp_path)
    lesson = store.create_lesson("ws", _lesson(["unarchived"]), enforce_iteration_signal=False)
    assert lesson.confidence == 0.6


def test_failure_evidence_survives_record_cache_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FeedbackLessonStore(tmp_path)
    record = _record(tmp_path, "task")
    selected = store.prepare_summary_input(
        "ws",
        record.parent,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )
    manager = WorkspaceManager()
    now = datetime.now()
    workspace = Workspace(
        id="ws",
        name="repo",
        default_branch="main",
        session_prefix="test",
        path=str(tmp_path),
        created_at=now,
        updated_at=now,
    )
    manager.workspaces = {"ws": workspace}
    monkeypatch.setattr(manager, "_feedback_store", lambda: store)
    prompt, task_ids, paths = manager._build_workspace_feedback_summary_prompt(workspace, selected)
    store.commit_summary_input(selected, paths)
    cached = json.loads((tmp_path / "ws/feedback/index.json").read_text())
    digest = FeedbackTaskDigest.model_validate(cached["processed_task_records"][0]["digest"])

    assert task_ids == ["task"]
    assert digest.failure_evidence[0].report_id == "report-failed"
    assert digest.failure_evidence[0].message == "wrong checkout"
    assert "pwd returned /repo/main" in prompt
    assert "report-failed" in prompt
    assert "must-not-copy" not in prompt
    assert "must-not-copy" not in json.dumps(cached)
    assert len(prompt) <= store.REAPER_PROMPT_HARD_CHAR_LIMIT
    assert (
        store._compact_digest_for_prompt(digest)["failure_evidence"][0]["report_id"]
        == "report-failed"
    )


def test_failure_evidence_keeps_recent_bounded_excerpts_and_legacy_defaults(tmp_path: Path) -> None:
    store = FeedbackLessonStore(tmp_path)
    reports = [
        {
            "id": f"r{i}",
            "state": "review_failed",
            "message": "m" * 2000,
            "validation": "v" * 2000,
            "risks": "r" * 2000,
        }
        for i in range(10)
    ]
    digest = store._digest_task_record({"task": {"id": "t"}, "reports": reports})
    evidence = store._compact_digest_for_prompt(digest)["failure_evidence"]
    assert [item["report_id"] for item in evidence] == ["r7", "r8", "r9"]
    for item in evidence:
        assert len(item["message"]) <= 240
        assert len(item["validation"]) <= 320
        assert len(item["risks"]) <= 160
    legacy = FeedbackTaskDigest.model_validate({"task_id": "old", "review_failed_count": 1})
    assert store._compact_digest_for_prompt(legacy)["failure_evidence"] == []


def test_evidence_prompt_budget_keeps_dropped_records_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FeedbackLessonStore(tmp_path)
    monkeypatch.setattr(store, "REAPER_PROMPT_HARD_CHAR_LIMIT", 8500)
    for i in range(3):
        path = _record(tmp_path, f"task-{i}")
        payload = json.loads(path.read_text())
        payload["reports"] = [
            {
                "id": f"report-{j}",
                "state": "review_failed",
                "message": "m" * 240,
                "validation": "v" * 320,
                "risks": "r" * 160,
            }
            for j in range(3)
        ]
        path.write_text(json.dumps(payload))
    records = tmp_path / "ws/task_records"
    selected = store.prepare_summary_input(
        "ws", records, mode=FeedbackSummaryMode.INCREMENTAL, limit=3, force=False
    )
    manager = WorkspaceManager()
    now = datetime.now()
    workspace = Workspace(
        id="ws",
        name="repo",
        default_branch="main",
        session_prefix="test",
        path=str(tmp_path),
        created_at=now,
        updated_at=now,
    )
    manager.workspaces = {"ws": workspace}
    monkeypatch.setattr(manager, "_feedback_store", lambda: store)
    prompt, task_ids, paths = manager._build_workspace_feedback_summary_prompt(workspace, selected)
    assert len(prompt) <= store.REAPER_PROMPT_HARD_CHAR_LIMIT
    assert 0 < len(task_ids) < 3
    store.commit_summary_input(selected, paths)
    remaining = store.prepare_summary_input(
        "ws", records, mode=FeedbackSummaryMode.INCREMENTAL, limit=3, force=False
    )
    assert set(remaining["input_record_ids"]) == {"task-0", "task-1", "task-2"} - set(task_ids)


def test_blank_legacy_workspace_identity_still_verifies_task_record(tmp_path: Path) -> None:
    store = FeedbackLessonStore(tmp_path)
    path = _record(tmp_path, "legacy")
    payload = json.loads(path.read_text())
    del payload["workspace_id"]
    path.write_text(json.dumps(payload))
    assert store.create_lesson("ws", _lesson(["legacy"])).confidence == 0.6
