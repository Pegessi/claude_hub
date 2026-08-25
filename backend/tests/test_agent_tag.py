"""agent_tag metadata: schema, API, persistence, and legacy load."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models import User, WorkspaceTaskStatus
from claude_hub.models.schemas import (
    AGENT_TAG_MAX_LENGTH,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
    normalize_agent_tag,
)
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager

_wm = import_module("claude_hub.services.workspace_manager")


def _reset_singleton() -> None:
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    workspace_manager._feedback_summary_locks.clear()
    workspace_manager.task_mailbox._events.clear()
    workspace_manager.task_mailbox._call_index.clear()
    workspace_manager.task_mailbox._next_seq.clear()
    workspace_manager.task_mailbox._waiters.events.clear()
    workspace_manager.task_mailbox._waiters.locks.clear()
    workspace_manager.task_mailbox._waiters.subtree_waiters.clear()


@pytest.fixture()
def persist_api(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> Generator[tuple[WorkspaceManager, Path], None, None]:
    root = tmp_path / "workspaces"
    root.mkdir()
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    _reset_singleton()

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    yield workspace_manager, root
    app.dependency_overrides.pop(get_current_user, None)
    _reset_singleton()


def _create_workspace(client: TestClient, tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    response = client.post(
        "/api/workspaces",
        json={"name": "Agent Tag", "path": str(repo), "session_prefix": "at"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_normalize_agent_tag_trims_and_bounds() -> None:
    assert normalize_agent_tag(None) is None
    assert normalize_agent_tag("  reviewer-alpha  ") == "reviewer-alpha"
    with pytest.raises(ValueError, match="empty"):
        normalize_agent_tag("")
    with pytest.raises(ValueError, match="empty"):
        normalize_agent_tag("   ")
    assert normalize_agent_tag("", allow_clear=True) is None
    with pytest.raises(ValueError, match="string"):
        normalize_agent_tag(42)
    too_long = "x" * (AGENT_TAG_MAX_LENGTH + 1)
    with pytest.raises(ValueError, match=str(AGENT_TAG_MAX_LENGTH)):
        normalize_agent_tag(too_long)


def test_normalize_agent_tag_rejects_control_characters() -> None:
    assert normalize_agent_tag("worker-α_1") == "worker-α_1"
    for invalid in ["line\nbreak", "tab\there", "ctrl\x07"]:
        with pytest.raises(ValueError, match="single-line"):
            normalize_agent_tag(invalid)


def test_workspace_task_create_update_schema_validation() -> None:
    created = WorkspaceTaskCreate(title="t", prompt="p", agent_tag="  worker  ")
    assert created.agent_tag == "worker"
    with pytest.raises(ValueError):
        WorkspaceTaskCreate(title="t", prompt="p", agent_tag="   ")
    cleared = WorkspaceTaskUpdate(agent_tag="")
    assert cleared.agent_tag is None


def test_legacy_task_without_agent_tag_loads_unchanged(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _root = persist_api
    client = TestClient(app)
    workspace_id = _create_workspace(client, tmp_path)
    legacy = {
        "id": "legacy-task-1",
        "workspace_id": workspace_id,
        "title": "Legacy",
        "prompt": "No tag field",
        "agent_type": "claude",
        "task_mode": "reviewed",
        "execution_complexity": "auto",
        "origin": "human",
        "status": "todo",
        "dispatch_pending": False,
        "review_attempts": 0,
        "review_cycle": 1,
        "reviewed_cycle": 0,
        "created_at": "2026-08-25T12:00:00",
        "updated_at": "2026-08-25T12:00:00",
    }
    normalized = manager._normalize_task_item(legacy)
    task = WorkspaceTask(**normalized)
    assert task.agent_tag is None


def test_untagged_task_state_json_omits_agent_tag_key(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, state_root = persist_api
    client = TestClient(app)
    workspace_id = _create_workspace(client, tmp_path)
    create = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Plain", "prompt": "no tag"},
    )
    assert create.status_code == 201, create.text
    task_id = create.json()["id"]

    manager._save_state()
    on_disk = json.loads((state_root / workspace_id / "state.json").read_text(encoding="utf-8"))
    saved = next(item for item in on_disk["tasks"] if item["id"] == task_id)
    assert "agent_tag" not in saved


def test_legacy_state_json_without_agent_tag_round_trips(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, state_root = persist_api
    client = TestClient(app)
    workspace_id = _create_workspace(client, tmp_path)
    task_id = "legacy-task-roundtrip"
    legacy = {
        "id": task_id,
        "workspace_id": workspace_id,
        "title": "Legacy",
        "prompt": "No tag field",
        "agent_type": "claude",
        "task_mode": "reviewed",
        "execution_complexity": "auto",
        "origin": "human",
        "status": "todo",
        "dispatch_pending": False,
        "review_attempts": 0,
        "review_cycle": 1,
        "reviewed_cycle": 0,
        "created_at": "2026-08-25T12:00:00",
        "updated_at": "2026-08-25T12:00:00",
    }
    state_file = state_root / workspace_id / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"tasks": [legacy], "sessions": [], "reports": []}, indent=2),
        encoding="utf-8",
    )

    _reset_singleton()
    reloaded = WorkspaceManager()
    assert reloaded.tasks[task_id].agent_tag is None
    reloaded._save_state()
    saved = next(item for item in json.loads(state_file.read_text(encoding="utf-8"))["tasks"])
    assert saved["id"] == task_id
    assert "agent_tag" not in saved


def test_agent_tag_persistence_save_and_reload(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, state_root = persist_api
    client = TestClient(app)
    workspace_id = _create_workspace(client, tmp_path)
    create = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Tagged",
            "prompt": "carry tag",
            "agent_tag": "  orchestrator  ",
        },
    )
    assert create.status_code == 201, create.text
    task_id = create.json()["id"]
    assert create.json()["agent_tag"] == "orchestrator"

    manager._save_state()
    state_file = state_root / workspace_id / "state.json"
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    saved = next(item for item in on_disk["tasks"] if item["id"] == task_id)
    assert saved["agent_tag"] == "orchestrator"

    _reset_singleton()
    reloaded = WorkspaceManager()
    assert reloaded.tasks[task_id].agent_tag == "orchestrator"


def test_agent_tag_api_board_tree_and_update_clear(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    client = TestClient(app)
    workspace_id = _create_workspace(client, tmp_path)

    tagged = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Tagged", "prompt": "x", "agent_tag": "worker-a"},
    )
    assert tagged.status_code == 201, tagged.text
    tagged_id = tagged.json()["id"]

    untagged = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Plain", "prompt": "y"},
    )
    assert untagged.status_code == 201, untagged.text
    assert untagged.json().get("agent_tag") in (None,)

    invalid = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Bad", "prompt": "z", "agent_tag": "   "},
    )
    assert invalid.status_code == 422

    invalid_control = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Bad control", "prompt": "z", "agent_tag": "bad\nline"},
    )
    assert invalid_control.status_code == 422

    board = client.get(f"/api/workspaces/{workspace_id}/board")
    assert board.status_code == 200, board.text
    board_tasks = {item["id"]: item for item in board.json()["tasks"]}
    assert board_tasks[tagged_id]["agent_tag"] == "worker-a"
    assert board_tasks[untagged.json()["id"]].get("agent_tag") in (None,)

    tree = client.get(f"/api/workspaces/{workspace_id}/tasks/tree")
    assert tree.status_code == 200, tree.text
    tree_by_id = {item["id"]: item for item in tree.json()}
    assert tree_by_id[tagged_id]["agent_tag"] == "worker-a"

    cleared = client.patch(
        f"/api/workspaces/tasks/{tagged_id}",
        json={"agent_tag": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["agent_tag"] is None
    assert workspace_manager.tasks[tagged_id].status == WorkspaceTaskStatus.TODO

    workspace_manager._save_state()
    state_root = persist_api[1]
    on_disk = json.loads((state_root / workspace_id / "state.json").read_text(encoding="utf-8"))
    cleared_saved = next(item for item in on_disk["tasks"] if item["id"] == tagged_id)
    assert "agent_tag" not in cleared_saved


def test_openapi_includes_agent_tag_on_task_schemas() -> None:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200, response.text
    schemas = response.json()["components"]["schemas"]
    for name in ("WorkspaceTask", "WorkspaceTaskCreate", "WorkspaceTaskUpdate"):
        props = schemas[name]["properties"]
        assert "agent_tag" in props, name
        agent_tag_schema = props["agent_tag"]
        if agent_tag_schema.get("type") != "string":
            assert "anyOf" in agent_tag_schema or "allOf" in agent_tag_schema, name
