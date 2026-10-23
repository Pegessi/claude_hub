"""Task status has one writer and one transition table."""

from __future__ import annotations

import ast
import logging
from datetime import datetime
from pathlib import Path

import pytest

from claude_hub.models import AgentType, WorkspaceTask, WorkspaceTaskStatus
from claude_hub.services import task_transitions
from claude_hub.services.task_transitions import (
    STRICT_ENV,
    TaskTransitionError,
    apply_task_update,
    transition_allowed,
)

SERVICES = Path(task_transitions.__file__).resolve().parent
NOW = datetime(2026, 10, 24, 9, 0, 0)


def _task(status: WorkspaceTaskStatus) -> WorkspaceTask:
    return WorkspaceTask(
        id="task-1",
        workspace_id="ws-1",
        title="T",
        prompt="P",
        agent_type=AgentType.CODEX,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def test_declared_edge_applies_and_returns_a_new_task() -> None:
    task = _task(WorkspaceTaskStatus.QUEUED)
    updated = apply_task_update(
        task, {"status": WorkspaceTaskStatus.WORKING, "started_at": NOW}, cause="dispatch"
    )
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert task.status == WorkspaceTaskStatus.QUEUED


def test_edge_with_the_wrong_cause_raises_in_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(TaskTransitionError, match="done->working cause=dispatch"):
        apply_task_update(
            _task(WorkspaceTaskStatus.DONE),
            {"status": WorkspaceTaskStatus.WORKING},
            cause="dispatch",
        )


def test_unexpected_edge_only_logs_outside_strict_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(STRICT_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger=task_transitions.__name__):
        updated = apply_task_update(
            _task(WorkspaceTaskStatus.DONE),
            {"status": WorkspaceTaskStatus.WORKING},
            cause="dispatch",
        )
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert "Unexpected Task transition done->working" in caplog.text


@pytest.mark.parametrize("cause", ["manual", "load"])
def test_human_edits_and_load_repair_may_take_any_edge(cause: str) -> None:
    for current in WorkspaceTaskStatus:
        for target in WorkspaceTaskStatus:
            assert transition_allowed(current, target, cause)


def test_non_status_updates_never_need_an_edge() -> None:
    task = _task(WorkspaceTaskStatus.DONE)
    assert apply_task_update(task, {"title": "Renamed"}, cause="anything").title == "Renamed"


def test_done_is_only_reopened_by_handoff_or_the_feedback_reaper() -> None:
    reopeners = {
        cause
        for (current, _target), causes in task_transitions.TRANSITIONS.items()
        if current == WorkspaceTaskStatus.DONE
        for cause in causes
    }
    assert reopeners == {"handoff", "feedback_reaper"}


def _literal_task_status_writes(path: Path) -> list[str]:
    """``x.model_copy(update={"status": WorkspaceTaskStatus...})`` bypasses the writer."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "model_copy":
            continue
        for keyword in node.keywords:
            if keyword.arg != "update" or not isinstance(keyword.value, ast.Dict):
                continue
            for key, value in zip(keyword.value.keys, keyword.value.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "status"
                    and "WorkspaceTaskStatus" in ast.unparse(value)
                ):
                    offenders.append(f"{path.relative_to(SERVICES.parent)}:{node.lineno}")
    return offenders


def test_task_status_is_written_only_through_apply_task_update() -> None:
    offenders = [
        offender
        for path in sorted(SERVICES.rglob("*.py"))
        if path.name != "task_transitions.py"
        for offender in _literal_task_status_writes(path)
    ]
    assert offenders == [], (
        "Write Task status with services.task_transitions.apply_task_update "
        f"so the transition table sees it: {offenders}"
    )
