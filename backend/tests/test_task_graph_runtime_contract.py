"""Runtime contract: OpenAPI, CLI, models, and persistence exclude Agent Tree."""

from __future__ import annotations

import json
import re
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
from claude_hub.models import User, WorkspaceTask
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager

_wm = import_module("claude_hub.services.workspace_manager")

_LEGACY_OPENAPI_PATH_FRAGMENT = "/api/agent-tree"
_LEGACY_SCHEMA_NAME = "AgentRun"
_LEGACY_FIELD_TOKENS = frozenset(
    {"agent_run_id", "resident_root", "agent_runs", "agent_events", "compat_run_id"}
)


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


def _openapi_document() -> dict[str, Any]:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _walk_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            out.extend(_walk_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_walk_strings(item))
    return out


def test_openapi_paths_exclude_agent_tree() -> None:
    openapi = _openapi_document()
    for path in openapi.get("paths", {}):
        assert _LEGACY_OPENAPI_PATH_FRAGMENT not in path, path


def test_openapi_schemas_exclude_legacy_agent_tree_surface() -> None:
    openapi = _openapi_document()
    schemas = openapi.get("components", {}).get("schemas", {})
    assert _LEGACY_SCHEMA_NAME not in schemas
    for name, schema in schemas.items():
        assert name != _LEGACY_SCHEMA_NAME
        assert "agent_tree" not in name.lower()
        blob = json.dumps(schema, sort_keys=True)
        for token in _LEGACY_FIELD_TOKENS:
            assert token not in blob, f"{name} mentions {token}"
        scrubbed = blob.replace("AgentRuntimeStatus", "")
        assert not re.search(r"\bAgentRun\b", scrubbed), name
    for text in _walk_strings(openapi.get("paths", {})):
        if _LEGACY_OPENAPI_PATH_FRAGMENT in text:
            pytest.fail(f"OpenAPI path text still mentions agent-tree: {text!r}")


def test_workspace_task_model_and_json_schema_exclude_legacy_fields() -> None:
    assert "agent_run_id" not in WorkspaceTask.model_fields
    schema = WorkspaceTask.model_json_schema()
    blob = json.dumps(schema, sort_keys=True)
    for token in _LEGACY_FIELD_TOKENS:
        assert token not in blob
    scrubbed = blob.replace("AgentRuntimeStatus", "")
    assert not re.search(r"\bAgentRun\b", scrubbed)


def test_cli_top_level_help_has_no_agent_tree() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    lowered = result.output.lower()
    assert "agent-tree" not in lowered
    assert "agent_tree" not in lowered


def test_cli_agent_tree_subcommand_does_not_exist() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["agent-tree", "--help"])
    assert result.exit_code != 0
    lowered = result.output.lower()
    assert "no such command" in lowered or "unknown" in lowered or "error" in lowered


def test_normal_persistence_payload_excludes_legacy_agent_tree_keys(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, state_root = persist_api
    client = TestClient(app)
    repo = tmp_path / "persist-contract"
    repo.mkdir()
    workspace = client.post(
        "/api/workspaces",
        json={"name": "persist-contract", "path": str(repo), "target": "local"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "solo", "prompt": "work", "agent_type": "claude"},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    followup = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task_id}/followup",
        json={"message": "ping", "call_id": "persist-fu-1"},
    )
    assert followup.status_code == 200, followup.text

    manager._save_state()
    state_file = state_root / workspace_id / "state.json"
    disk = json.loads(state_file.read_text(encoding="utf-8"))

    assert "agent_runs" not in disk
    assert "agent_events" not in disk
    for item in disk.get("tasks") or []:
        assert isinstance(item, dict)
        for token in _LEGACY_FIELD_TOKENS:
            assert token not in item
    for item in disk.get("task_events") or []:
        assert isinstance(item, dict)
        for token in _LEGACY_FIELD_TOKENS:
            assert token not in item

    index = json.loads((state_root / "index.json").read_text(encoding="utf-8"))
    index_blob = json.dumps(index)
    assert "resident_ack_sequence" not in index_blob
    assert "resident_root" not in index_blob
