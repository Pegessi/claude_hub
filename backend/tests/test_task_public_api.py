"""Task-first REST contract: tree/events/wait/ack/followup. No AgentRun writes."""

from __future__ import annotations

import asyncio
import json
import time
from importlib import import_module
from pathlib import Path
from typing import Any, Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import User
from claude_hub.services.task_graph import make_task_consumer_key
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
    workspace_manager.agent_tree._runs.clear()
    workspace_manager.agent_tree._events.clear()
    workspace_manager.agent_tree._call_index.clear()
    workspace_manager.task_mailbox._waiters.events.clear()
    workspace_manager.task_mailbox._waiters.locks.clear()
    workspace_manager.task_mailbox._waiters.subtree_waiters.clear()
    workspace_manager.agent_tree._waiters.events.clear()
    workspace_manager.agent_tree._waiters.locks.clear()
    workspace_manager.agent_tree._waiters.subtree_waiters.clear()


def _runs_dump(manager: WorkspaceManager) -> str:
    return json.dumps(
        {
            run_id: run.model_dump(mode="json")
            for run_id, run in sorted(manager.agent_tree._runs.items())
        },
        sort_keys=True,
        default=str,
    )


def _agent_events_dump(manager: WorkspaceManager) -> str:
    return json.dumps(
        {
            workspace_id: [event.model_dump(mode="json") for event in events]
            for workspace_id, events in sorted(manager.agent_tree._events.items())
        },
        sort_keys=True,
        default=str,
    )


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
    monkeypatch.setattr(workspace_manager, "start_task", AsyncMock())
    monkeypatch.setattr(workspace_manager, "send_session_message", AsyncMock())

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


def _client() -> TestClient:
    return TestClient(app)


def _create_workspace(client: TestClient, tmp_path: Path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    response = client.post(
        "/api/workspaces",
        json={"name": name, "path": str(repo), "target": "local"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_task(
    client: TestClient,
    workspace_id: str,
    title: str,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title,
        "prompt": f"do {title}",
        "agent_type": "claude",
    }
    if parent_task_id is not None:
        body["parent_task_id"] = parent_task_id
    response = client.post(f"/api/workspaces/{workspace_id}/tasks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_ordinary_task_tree_events_wait_ack_followup(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    events_before = _agent_events_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "ordinary")
    task = _create_task(client, workspace_id, "solo")
    task_id = task["id"]

    tree = client.get(f"/api/workspaces/{workspace_id}/tasks/tree")
    assert tree.status_code == 200, tree.text
    assert [item["id"] for item in tree.json()] == [task_id]

    subtree = client.get(f"/api/workspaces/{workspace_id}/tasks/{task_id}/tree")
    assert subtree.status_code == 200
    assert [item["id"] for item in subtree.json()] == [task_id]

    followup = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task_id}/followup",
        json={"message": "ping", "call_id": "fu-ordinary"},
    )
    assert followup.status_code == 200, followup.text
    event = followup.json()
    assert event["call_id"] == "fu-ordinary"
    assert event["type"] == "followup"
    assert event["task_id"] == task_id
    assert event["consumer_key"] == make_task_consumer_key(task_id)
    assert "author" not in event

    events = client.get(f"/api/workspaces/{workspace_id}/tasks/{task_id}/events")
    assert events.status_code == 200
    assert [item["call_id"] for item in events.json()] == ["fu-ordinary"]

    waited = client.post(f"/api/workspaces/{workspace_id}/tasks/{task_id}/wait")
    assert waited.status_code == 200
    assert [item["call_id"] for item in waited.json()] == ["fu-ordinary"]
    sequence = waited.json()[0]["sequence"]

    acked = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task_id}/ack",
        json={"sequence": sequence},
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["id"] == task_id
    assert acked.json()["consumer_ack_sequence"] == sequence

    after_ack = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task_id}/wait",
        params={"timeout_seconds": 0},
    )
    assert after_ack.json() == []
    assert _runs_dump(manager) == runs_before
    assert _agent_events_dump(manager) == events_before
    assert manager.agent_tree._runs == {}


def test_parent_child_subtree_and_wait_ack_cold_cursor(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "subtree")
    parent = _create_task(client, workspace_id, "parent")
    child = _create_task(client, workspace_id, "child", parent_task_id=parent["id"])

    top = client.get(f"/api/workspaces/{workspace_id}/tasks/tree")
    assert [item["id"] for item in top.json()] == [parent["id"]]
    subtree = client.get(f"/api/workspaces/{workspace_id}/tasks/{parent['id']}/tree")
    assert {item["id"] for item in subtree.json()} == {parent["id"], child["id"]}

    followup = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{child['id']}/followup",
        json={"message": "child note", "call_id": "fu-child"},
    )
    assert followup.status_code == 200, followup.text
    assert followup.json()["consumer_key"] == make_task_consumer_key(child["id"])

    parent_direct = client.get(f"/api/workspaces/{workspace_id}/tasks/{parent['id']}/events")
    assert parent_direct.json() == []
    parent_subtree = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{parent['id']}/wait",
        params={"subtree": True},
    )
    assert [item["call_id"] for item in parent_subtree.json()] == ["fu-child"]
    sequence = parent_subtree.json()[0]["sequence"]

    ack = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{parent['id']}/ack",
        json={"sequence": sequence},
    )
    assert ack.status_code == 200
    assert ack.json()["consumer_ack_sequence"] == sequence
    assert manager.tasks[parent["id"]].consumer_ack_sequence == sequence
    assert manager.tasks[child["id"]].consumer_ack_sequence == 0

    empty = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{parent['id']}/wait",
        params={"subtree": True, "timeout_seconds": 0},
    )
    assert empty.json() == []

    fresh = WorkspaceManager()
    assert fresh.tasks[parent["id"]].consumer_ack_sequence == sequence
    assert (
        fresh.task_mailbox.wait(
            workspace_id,
            make_task_consumer_key(parent["id"]),
            subtree=True,
        )
        == []
    )
    assert fresh.agent_tree._runs == {}
    assert _runs_dump(manager) == runs_before


def test_followup_call_id_idempotent_and_conflict(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "call-id")
    task = _create_task(client, workspace_id, "solo")
    path = f"/api/workspaces/{workspace_id}/tasks/{task['id']}/followup"

    first = client.post(path, json={"message": "same", "call_id": "retry-1"})
    second = client.post(path, json={"message": "same", "call_id": "retry-1"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["sequence"] == second.json()["sequence"]
    assert first.json()["call_id"] == "retry-1"
    mailbox = client.get(f"/api/workspaces/{workspace_id}/tasks/{task['id']}/events")
    assert [item["call_id"] for item in mailbox.json()] == ["retry-1"]

    conflict = client.post(path, json={"message": "other", "call_id": "retry-1"})
    assert conflict.status_code == 409
    assert "already used" in conflict.json()["detail"]
    bad_ack = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/ack",
        json={"sequence": 99},
    )
    assert bad_ack.status_code == 400
    assert _runs_dump(manager) == runs_before


def test_cross_workspace_task_paths_rejected(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    ws_a = _create_workspace(client, tmp_path, "ws-a")
    ws_b = _create_workspace(client, tmp_path, "ws-b")
    task = _create_task(client, ws_a, "owned-by-a")

    assert client.get(f"/api/workspaces/{ws_b}/tasks/{task['id']}/tree").status_code == 404
    assert client.get(f"/api/workspaces/{ws_b}/tasks/{task['id']}/events").status_code == 404
    assert client.post(f"/api/workspaces/{ws_b}/tasks/{task['id']}/wait").status_code == 404
    assert (
        client.post(
            f"/api/workspaces/{ws_b}/tasks/{task['id']}/ack",
            json={"sequence": 1},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/workspaces/{ws_b}/tasks/{task['id']}/followup",
            json={"message": "nope", "call_id": "cross"},
        ).status_code
        == 404
    )
    assert _runs_dump(manager) == runs_before
    assert manager.agent_tree._runs == {}


def test_root_task_ack_advances_task_cursor_only(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "root-task")
    task = _create_task(client, workspace_id, "rootish")
    followup = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/followup",
        json={"message": "for root inbox", "call_id": "fu-root"},
    )
    assert followup.status_code == 200
    sequence = followup.json()["sequence"]

    waited = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/wait",
        params={"subtree": True},
    )
    assert [item["call_id"] for item in waited.json()] == ["fu-root"]
    acked = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/ack",
        json={"sequence": sequence},
    )
    assert acked.status_code == 200
    assert acked.json()["consumer_ack_sequence"] == sequence
    assert manager.tasks[task["id"]].consumer_ack_sequence == sequence
    assert manager.agent_tree._runs == {}
    empty = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/wait",
        params={"subtree": True, "timeout_seconds": 0},
    )
    assert empty.json() == []


def test_legacy_resident_mailbox_routes_are_gone(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "legacy-resident")
    for method, path in (
        ("get", f"/api/workspaces/{workspace_id}/resident/events"),
        ("post", f"/api/workspaces/{workspace_id}/resident/wait"),
        ("post", f"/api/workspaces/{workspace_id}/resident/ack"),
    ):
        if method == "get":
            response = client.get(path)
        else:
            response = client.post(path, json={"sequence": 1})
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_public_wait_wakes_on_pre_append_followup(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "wait-wake")
    task = _create_task(client, workspace_id, "solo")
    consumer = make_task_consumer_key(task["id"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        wait_task = asyncio.create_task(
            async_client.post(
                f"/api/workspaces/{workspace_id}/tasks/{task['id']}/wait",
                params={"timeout_seconds": 2, "since_sequence": 0},
            )
        )
        for _ in range(50):
            if consumer in manager.task_mailbox._waiters.events:
                break
            await asyncio.sleep(0.02)
        else:
            wait_task.cancel()
            pytest.fail("Task wait did not register a consumer Event")
        await asyncio.sleep(0.02)
        followup = await async_client.post(
            f"/api/workspaces/{workspace_id}/tasks/{task['id']}/followup",
            json={"message": "wake", "call_id": "wake-pre-append"},
        )
        assert followup.status_code == 200, followup.text
        waited = await wait_task

    assert waited.status_code == 200, waited.text
    assert [item["call_id"] for item in waited.json()] == ["wake-pre-append"]
    assert _runs_dump(manager) == runs_before
    assert manager.agent_tree._runs == {}


@pytest.mark.asyncio
async def test_root_task_subtree_wait_wakes_on_pre_append_followup(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "root-wake")
    task = _create_task(client, workspace_id, "rootish")
    consumer = make_task_consumer_key(task["id"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        wait_task = asyncio.create_task(
            async_client.post(
                f"/api/workspaces/{workspace_id}/tasks/{task['id']}/wait",
                params={"timeout_seconds": 2, "subtree": True, "since_sequence": 0},
            )
        )
        for _ in range(50):
            if consumer in manager.task_mailbox._waiters.events:
                break
            await asyncio.sleep(0.02)
        else:
            wait_task.cancel()
            pytest.fail("Root task subtree wait did not register a consumer Event")
        await asyncio.sleep(0.02)
        followup = await async_client.post(
            f"/api/workspaces/{workspace_id}/tasks/{task['id']}/followup",
            json={"message": "wake root", "call_id": "wake-root"},
        )
        assert followup.status_code == 200, followup.text
        waited = await wait_task

    assert waited.status_code == 200, waited.text
    assert [item["call_id"] for item in waited.json()] == ["wake-root"]
    assert _runs_dump(manager) == runs_before


def test_public_wait_timeout_returns_empty(
    persist_api: tuple[WorkspaceManager, Path], tmp_path: Path
) -> None:
    manager, _ = persist_api
    runs_before = _runs_dump(manager)
    client = _client()
    workspace_id = _create_workspace(client, tmp_path, "wait-timeout")
    task = _create_task(client, workspace_id, "solo")
    started = time.monotonic()
    response = client.post(
        f"/api/workspaces/{workspace_id}/tasks/{task['id']}/wait",
        params={"timeout_seconds": 0.15, "since_sequence": 0},
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json() == []
    assert elapsed >= 0.1
    assert _runs_dump(manager) == runs_before
    assert manager.agent_tree._runs == {}
