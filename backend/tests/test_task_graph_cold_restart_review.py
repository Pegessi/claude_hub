"""P1-3: cold-restart regression for sealed review rounds (seq4 / reaper)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    TerminalTab,
    User,
    WorkspaceSessionRole,
)
from claude_hub.services.task_graph import tasks_in_subtree
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


def _parent_subtree_events(manager: WorkspaceManager, workspace_id: str, parent_id: str):
    root = manager.tasks[parent_id]
    subtree_ids = {task.id for task in tasks_in_subtree(manager.tasks.values(), workspace_id, root)}
    return [
        event
        for event in manager.task_mailbox._events.get(workspace_id, [])
        if event.task_id in subtree_ids
    ]


def _assert_all_report_ids_resolve(
    manager: WorkspaceManager,
    events: list[Any],
) -> None:
    for event in events:
        report_id = getattr(event, "report_id", None)
        if not report_id:
            continue
        assert report_id in manager.reports, (
            f"TaskEvent sequence={event.sequence} call_id={event.call_id!r} "
            f"references missing report_id={report_id!r}"
        )


@pytest.fixture()
def cold_review_env(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> Generator[tuple[Path, list[tuple[str, str]]], None, None]:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)

    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: str | None = None,
        cwd: str | None = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: str | None = None,
        remote_cwd: str | None = None,
        remote_reconnect: bool = True,
        remote_forward_port: int | None = None,
        workspace_id: str | None = None,
        workspace_name: str | None = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"tab-{len(sent_messages)}"
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12380,
            created_at=datetime.utcnow(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_send_with_receipt(tmux_session: str, message: str, call_id: str) -> None:
        await fake_send_tmux_message(tmux_session, message)

    async def fake_query_receipt(tmux_session: str, call_id: str) -> bool:
        return False

    async def fake_ensure_session_ready(_session) -> None:
        return None

    _reset_singleton()
    monkeypatch.setattr(_wm.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_send_tmux_message_with_receipt",
        fake_send_with_receipt,
    )
    monkeypatch.setattr(workspace_manager, "_query_tmux_receipt", fake_query_receipt)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    yield root, sent_messages
    app.dependency_overrides.pop(get_current_user, None)
    _reset_singleton()


def _run_review_cycle(
    client: TestClient,
    *,
    workspace_id: str,
    parent_id: str,
    child_id: str,
) -> tuple[list[Any], int]:
    started = client.post(f"/api/workspaces/tasks/{child_id}/start", json={}).json()
    worker_session_id = started["session_id"]

    ready = client.post(
        f"/api/workspaces/sessions/{worker_session_id}/reports",
        json={
            "task_id": child_id,
            "state": "ready_for_review",
            "message": "Child done; please review",
        },
    )
    assert ready.status_code == 201, ready.text

    child_task = workspace_manager.tasks[child_id]
    reviewer_session_id = child_task.review_session_id
    assert reviewer_session_id is not None

    review_started = client.post(
        f"/api/workspaces/sessions/{reviewer_session_id}/reports",
        json={
            "task_id": child_id,
            "state": "review_started",
            "message": "Reviewing child",
        },
    )
    assert review_started.status_code == 201, review_started.text

    passed = client.post(
        f"/api/workspaces/sessions/{reviewer_session_id}/reports",
        json={
            "task_id": child_id,
            "state": "review_passed",
            "message": "Child review passed",
        },
    )
    assert passed.status_code == 201, passed.text

    judged = workspace_manager.tasks[child_id]
    assert judged.reviewed_cycle >= judged.review_cycle, (
        f"precondition: sealed round review_cycle={judged.review_cycle} "
        f"reviewed_cycle={judged.reviewed_cycle}"
    )

    events = _parent_subtree_events(workspace_manager, workspace_id, parent_id)
    sequences = sorted(event.sequence for event in events)
    assert len(sequences) == 3, f"expected 3 target events before reload, got {sequences}"
    max_sequence = max(sequences)
    _assert_all_report_ids_resolve(workspace_manager, events)
    return events, max_sequence


def test_cold_restart_after_verdict_does_not_append_task_events(
    cold_review_env: tuple[Path, list[tuple[str, str]]],
    tmp_path: Path,
) -> None:
    """Same-cycle verdict survives cold reload: no reaper/backfill seq4 extras."""

    _, sent_messages = cold_review_env
    client = TestClient(app)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Cold Review", "path": str(repo), "session_prefix": "coldrev"},
    ).json()
    workspace_id = workspace["id"]
    parent = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Parent", "prompt": "Supervise child"},
    ).json()
    parent_id = parent["id"]
    child = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Child",
            "prompt": "Implement feature",
            "parent_task_id": parent_id,
        },
    ).json()
    child_id = child["id"]

    before_events, max_sequence = _run_review_cycle(
        client,
        workspace_id=workspace_id,
        parent_id=parent_id,
        child_id=child_id,
    )
    before_snapshots = {event.call_id: event.model_dump(mode="json") for event in before_events}
    state_file = cold_review_env[0] / workspace_id / "state.json"
    disk_before = json.loads(state_file.read_text(encoding="utf-8"))
    disk_event_count = len(disk_before.get("task_events") or [])

    _reset_singleton()
    reloaded = WorkspaceManager()
    after_load_events = _parent_subtree_events(reloaded, workspace_id, parent_id)
    assert [event.sequence for event in after_load_events] == [
        event.sequence for event in before_events
    ], "load/backfill must not append TaskMailbox rows for sealed verdict round"
    _assert_all_report_ids_resolve(reloaded, after_load_events)

    sent_messages.clear()
    asyncio.run(reloaded.dispatch_workspace(workspace_id, refresh_sessions=False))
    after_dispatch_events = _parent_subtree_events(reloaded, workspace_id, parent_id)
    assert len(after_dispatch_events) == len(before_events), (
        "fallback reaper must not append TaskMailbox events for sealed round; "
        f"before={len(before_events)} after={len(after_dispatch_events)} "
        f"sequences={[e.sequence for e in after_dispatch_events]}"
    )
    assert max(event.sequence for event in after_dispatch_events) == max_sequence
    assert all(
        "fallback reaper" not in msg.lower() for _, msg in sent_messages
    ), "reviewer must not be re-prompted after sealed verdict"
    for event in after_dispatch_events:
        assert event.call_id in before_snapshots
        assert event.model_dump(mode="json") == before_snapshots[event.call_id]

    disk_after = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(disk_after.get("task_events") or []) == disk_event_count


def test_cold_restart_repairs_stale_reviewed_cycle_when_verdict_present(
    cold_review_env: tuple[Path, list[tuple[str, str]]],
    tmp_path: Path,
) -> None:
    """Production root cause: persisted reviewed_cycle=0 with review_completed_at set."""

    _, sent_messages = cold_review_env
    client = TestClient(app)
    repo = tmp_path / "repo-stale"
    repo.mkdir()
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Stale Cycle", "path": str(repo), "session_prefix": "stale"},
    ).json()
    workspace_id = workspace["id"]
    parent = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Parent stale", "prompt": "Supervise"},
    ).json()
    parent_id = parent["id"]
    child = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Child stale",
            "prompt": "Work",
            "parent_task_id": parent_id,
        },
    ).json()
    child_id = child["id"]

    before_events, max_sequence = _run_review_cycle(
        client,
        workspace_id=workspace_id,
        parent_id=parent_id,
        child_id=child_id,
    )
    assert len(before_events) == 3

    state_file = cold_review_env[0] / workspace_id / "state.json"
    disk = json.loads(state_file.read_text(encoding="utf-8"))
    stale_at = (
        datetime.utcnow() - timedelta(seconds=_wm.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30)
    ).isoformat()
    for item in disk["tasks"]:
        if item.get("id") == child_id:
            item["reviewed_cycle"] = 0
            item["review_completed_at"] = stale_at
            item["review_requested_at"] = stale_at
            item["human_acceptance_requested_at"] = None
            break
    state_file.write_text(json.dumps(disk), encoding="utf-8")

    _reset_singleton()
    reloaded = WorkspaceManager()
    loaded_child = reloaded.tasks[child_id]
    assert (
        loaded_child.reviewed_cycle >= loaded_child.review_cycle
    ), "normalize must repair reviewed_cycle when a verdict timestamp is present"

    sent_messages.clear()
    asyncio.run(reloaded.dispatch_workspace(workspace_id, refresh_sessions=False))
    after_events = _parent_subtree_events(reloaded, workspace_id, parent_id)
    assert len(after_events) == len(before_events)
    assert max(event.sequence for event in after_events) == max_sequence
    assert all("fallback reaper" not in msg.lower() for _, msg in sent_messages)
    _assert_all_report_ids_resolve(reloaded, after_events)
