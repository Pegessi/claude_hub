"""Explicit host dependencies preserve caller-owned WorkspaceManager bindings."""

import importlib
from datetime import datetime
from pathlib import Path

import pytest

from claude_hub.models import Workspace
from claude_hub.services.workspace_manager import WorkspaceManager


def test_existing_manager_reads_current_clock_and_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager.__new__(WorkspaceManager)
    first = datetime(2026, 1, 1)
    second = datetime(2026, 2, 1)

    monkeypatch.setattr(module, "_now", lambda: first)
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path / "first")
    assert manager._runtime_now() is first
    assert manager._workspace_dir("workspace") == tmp_path / "first" / "workspace"

    monkeypatch.setattr(module, "_now", lambda: second)
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path / "second")
    assert manager._runtime_now() is second
    assert manager._workspace_dir("workspace") == tmp_path / "second" / "workspace"


def test_existing_manager_uses_current_resident_prompt_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager.__new__(WorkspaceManager)
    workspace = Workspace(
        id="workspace",
        name="Example",
        path="/fixture/workspace",
        default_branch="main",
        session_prefix="example",
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    calls: list[tuple[Workspace, str, str]] = []

    def build(current: Workspace, base_url: str, session_id: str) -> str:
        calls.append((current, base_url, session_id))
        return "current builder"

    monkeypatch.setattr(module, "build_resident_agent_prompt", build)
    assert manager._runtime_resident_prompt(workspace, "http://localhost", "session") == (
        "current builder"
    )
    assert calls == [(workspace, "http://localhost", "session")]


def test_scheduled_file_override_is_independent_of_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("claude_hub.services.workspace_manager")
    scheduling = importlib.import_module("claude_hub.services.workspace_manager._scheduling")
    manager = WorkspaceManager.__new__(WorkspaceManager)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    monkeypatch.setattr(scheduling, "SCHEDULED_TASKS_FILE", first)
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path / "unrelated")
    assert manager._runtime_scheduled_tasks_file == first
    monkeypatch.setattr(scheduling, "SCHEDULED_TASKS_FILE", second)
    assert manager._runtime_scheduled_tasks_file == second
