"""Dot execution API, credential replacement and tab/schedule ownership."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from claude_hub.api import agent_stream, feishu_bot
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ScheduledTaskCreate,
    ScheduledTaskKind,
    ScheduledTaskRun,
    ScheduledTaskRunStatus,
    ScheduledTaskUpdate,
)
from claude_hub.models.schemas import User
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.dot import DotService, DotStore, execution, reset_dot_service


@pytest.fixture()
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[SimpleNamespace, None, None]:
    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.workspace_manager"),
        "STATE_ROOT",
        tmp_path / "state",
    )
    monkeypatch.setattr(workspace_manager, "scheduled_tasks", {})
    monkeypatch.setattr(workspace_manager, "scheduled_task_runs", {})
    monkeypatch.setattr(workspace_manager, "_save_scheduled_tasks", lambda: None)
    monkeypatch.setattr(
        feishu_bot, "_pool", SimpleNamespace(snapshot=lambda: SimpleNamespace(bots=[]))
    )
    goal = SimpleNamespace(current=lambda tab_id: None)
    monkeypatch.setattr(execution, "get_goal_manager", lambda: goal)
    manager = SimpleNamespace(get_tailer=lambda sid: None, forget_session=AsyncMock())
    monkeypatch.setattr(agent_stream, "_get_tab_tailer_manager", lambda: manager)
    service = DotService(DotStore(tmp_path / "dot"))
    service.store.ensure()
    reset_dot_service(service)
    tabs: dict[str, Any] = {}
    created: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> SimpleNamespace:
        created.append(kwargs)
        tab = SimpleNamespace(
            id=f"dot-{len(created)}",
            archived=False,
            port=0,
            is_active=True,
            created_at=datetime.now(),
            managed_session_id=None,
            workspace_role=None,
            **kwargs,
        )
        tabs[tab.id] = tab
        return tab

    async def archive(tab_id: str) -> Any:
        if tab_id in tabs:
            tabs[tab_id].archived = True
        return tabs.get(tab_id)

    async def unarchive(tab_id: str) -> Any:
        if tab_id in tabs:
            tabs[tab_id].archived = False
        return tabs.get(tab_id)

    async def switch_env(tab_id: str, env: dict[str, str], *, solo_mode: bool | None = None) -> Any:
        tab = tabs[tab_id]
        tab.env = dict(env)
        if solo_mode is not None:
            tab.solo_mode = solo_mode
        return tab

    monkeypatch.setattr(ttyd_manager, "create_tab", create)
    monkeypatch.setattr(ttyd_manager, "get_tab", tabs.get)
    monkeypatch.setattr(ttyd_manager, "archive_tab", archive)
    monkeypatch.setattr(ttyd_manager, "unarchive_tab", unarchive)
    monkeypatch.setattr(ttyd_manager, "switch_env", switch_env)
    monkeypatch.setattr(
        importlib.import_module("claude_hub.api.tabs"),
        "_terminal_tab_stream_session",
        lambda _tab_id: None,
    )
    user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)
    app.dependency_overrides[get_current_user] = lambda: user
    yield SimpleNamespace(
        client=TestClient(app),
        service=service,
        tabs=tabs,
        created=created,
        manager=manager,
        goal=goal,
        create=create,
        archive=archive,
        unarchive=unarchive,
    )
    app.dependency_overrides.pop(get_current_user, None)
    reset_dot_service(None)


def scheduled_run(schedule_id: str, tab_id: str, run_id: str) -> ScheduledTaskRun:
    now = datetime.now()
    return ScheduledTaskRun(
        id=run_id,
        scheduled_task_id=schedule_id,
        tab_id=tab_id,
        client_turn_id=f"turn-{run_id}",
        message="[Dot wake] pending",
        status=ScheduledTaskRunStatus.WAITING,
        scheduled_for=now,
        queued_at=now,
    )


def apply(setup: SimpleNamespace, provider: str, env: dict[str, str] | None = None) -> Any:
    revision = setup.client.get("/api/dot/execution").json()["revision"]
    return setup.client.put(
        "/api/dot/execution",
        json={
            "expected_revision": revision,
            "agent_type": provider,
            "env": env or {},
        },
    )


def test_initial_config_is_read_only_and_secret_response_is_not_cached(
    setup: SimpleNamespace,
) -> None:
    before = setup.service.store.read_state()
    result = setup.client.get("/api/dot/execution")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["tab_id"] is None
    assert result.json()["supported_agent_types"] == [
        "claude",
        "codex",
        "cursor",
        "antigravity",
        "traex",
        "maso",
    ]
    assert setup.service.store.read_state() == before
    assert setup.created == []


def test_antigravity_switches_with_its_own_env_and_never_inherits_old_credentials(
    setup: SimpleNamespace,
) -> None:
    first = apply(setup, "claude", {"ANTHROPIC_API_KEY": "old-secret"})
    assert first.status_code == 200, first.text
    result = apply(setup, "antigravity", {"ANTIGRAVITY_MODEL": "gemini-3-pro"})
    assert result.status_code == 200, result.text
    assert result.json()["agent_type"] == "antigravity"
    assert result.json()["env"] == {"ANTIGRAVITY_MODEL": "gemini-3-pro"}
    assert setup.created[-1]["agent_type"] == AgentType.ANTIGRAVITY
    assert setup.created[-1]["env"] == {"ANTIGRAVITY_MODEL": "gemini-3-pro"}


def test_switch_archives_old_tab_and_preserves_memory_and_schedule_choices(
    setup: SimpleNamespace,
) -> None:
    first = apply(
        setup, "claude", {"ANTHROPIC_API_KEY": "test-old-secret", "ANTHROPIC_MODEL": "old-model"}
    )
    assert first.status_code == 200, first.text
    old_id = first.json()["tab_id"]
    setup.service.store.add_focus("Remember this priority")
    setup.service.store.upsert_work("task-1", title="Keep tracking", watched=True)
    state = setup.service.store.read_state()
    wake = workspace_manager.scheduled_tasks[state["wake_schedule_id"]]
    tidy = workspace_manager.scheduled_tasks[state["tidy_schedule_id"]]
    wake.enabled = False
    wake.message = "My custom wake text"
    tidy.cron = "15 3 * * *"
    second = apply(setup, "codex", {"CODEX_MODEL": "new-model"})
    assert second.status_code == 200, second.text
    new_id = second.json()["tab_id"]
    assert new_id != old_id and setup.tabs[old_id].archived
    assert second.json()["env"] == {"CODEX_MODEL": "new-model"}
    assert setup.created[-1]["env"] == {"CODEX_MODEL": "new-model"}
    assert "agent_session_id" not in setup.created[-1]
    assert setup.service.tab_id() == new_id
    assert setup.service.store.list_focus()[0]["title"] == "Remember this priority"
    assert setup.service.store.list_work()[0]["task_id"] == "task-1"
    wake = workspace_manager.scheduled_tasks[state["wake_schedule_id"]]
    tidy = workspace_manager.scheduled_tasks[state["tidy_schedule_id"]]
    assert wake.tab_id == new_id and not wake.enabled and wake.message == "My custom wake text"
    assert tidy.tab_id == new_id and tidy.cron == "15 3 * * *"
    assert setup.service.store.read_state().get("session_conversation_id") is None
    assert "test-old-secret" not in (setup.service.store.home / "state.json").read_text()
    assert apply(setup, "codex", {"CODEX_MODEL": "new-model"}).json()["tab_id"] == new_id
    assert len(setup.created) == 2


def test_stale_revision_and_missing_replacement_env_cannot_switch(setup: SimpleNamespace) -> None:
    old = setup.client.get("/api/dot/execution").json()
    assert apply(setup, "claude").status_code == 200
    request = {"expected_revision": old["revision"], "agent_type": "maso", "env": {}}
    assert setup.client.put("/api/dot/execution", json=request).status_code == 409
    request.pop("env")
    assert setup.client.put("/api/dot/execution", json=request).status_code == 422
    request["env"] = {}
    request["agent_type"] = "terminal"
    assert setup.client.put("/api/dot/execution", json=request).status_code == 422
    assert len(setup.created) == 1


@pytest.mark.parametrize("conflict", ["turn", "goal", "feishu", "schedule", "queued", "uncertain"])
def test_busy_ownership_rejection_keeps_original_dot(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    first = apply(setup, "claude")
    assert first.status_code == 200, first.text
    tab_id = setup.service.tab_id()
    if conflict == "turn":
        setup.manager.get_tailer = lambda sid: SimpleNamespace(
            native_transport=SimpleNamespace(turn_in_flight=True)
        )
    elif conflict == "goal":
        setup.goal.current = lambda tid: SimpleNamespace(status=SimpleNamespace(value="paused"))
    elif conflict == "feishu":
        monkeypatch.setattr(
            feishu_bot,
            "_pool",
            SimpleNamespace(
                snapshot=lambda: SimpleNamespace(
                    bots=[SimpleNamespace(binding=SimpleNamespace(tab_id=tab_id))]
                )
            ),
        )
    elif conflict == "schedule":
        workspace_manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="Legacy terminal schedule",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                tab_id=tab_id,
                interval_seconds=3600,
                message="External work",
            )
        )
    elif conflict == "queued":
        workspace_manager.scheduled_task_runs["run"] = scheduled_run(
            setup.service.store.read_state()["wake_schedule_id"], tab_id, "run"
        )
    else:
        stream = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
        path = stream.path.with_suffix(".maso-delivery.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"uncertain": true}')
    result = apply(setup, "maso")
    assert result.status_code == 409, result.text
    assert setup.service.tab_id() == tab_id
    assert not setup.tabs[tab_id].archived
    assert len(setup.created) == 1


def test_creation_failure_leaves_previous_identity_and_schedules(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert apply(setup, "claude").status_code == 200
    before = setup.service.store.read_state()
    monkeypatch.setattr(
        ttyd_manager, "create_tab", AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    response = apply(setup, "maso")
    assert response.status_code == 503
    assert setup.service.store.read_state() == before
    assert not setup.tabs[before["tab_id"]].archived


def test_git_history_failure_does_not_report_a_committed_switch_as_failed(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert apply(setup, "claude").status_code == 200
    old_id = setup.service.tab_id()

    def fail_history(message: str) -> bool:
        raise RuntimeError("Git history unavailable")

    monkeypatch.setattr(setup.service.store, "commit", fail_history)
    result = apply(setup, "codex")
    assert result.status_code == 200, result.text
    assert result.json()["agent_type"] == "codex"
    assert setup.service.tab_id() != old_id
    assert setup.tabs[old_id].archived
    assert setup.service.store.read_state().get("pending_execution") is None


def test_state_commit_failure_rolls_back_schedule_and_old_tab(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert apply(setup, "claude").status_code == 200
    before = setup.service.store.read_state()
    update = setup.service.store.update_state

    def fail_commit(**changes: Any) -> Any:
        if changes.get("execution_agent_type") == "codex":
            raise RuntimeError("state write failed")
        return update(**changes)

    monkeypatch.setattr(setup.service.store, "update_state", fail_commit)
    response = apply(setup, "codex")
    assert response.status_code == 503, response.text
    assert setup.service.tab_id() == before["tab_id"]
    assert not setup.tabs[before["tab_id"]].archived
    assert setup.tabs["dot-2"].archived
    assert setup.service.store.read_state().get("pending_execution") is None
    for key in ("wake_schedule_id", "tidy_schedule_id"):
        assert workspace_manager.scheduled_tasks[before[key]].tab_id == before["tab_id"]


@pytest.mark.asyncio
async def test_old_history_is_synced_and_never_copied_as_foreign_provider_id(
    setup: SimpleNamespace,
) -> None:
    result = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    tab_id = result["tab_id"]
    stream = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
    for kind, payload in (
        (AgentStreamEventType.TURN_STARTED, {"summary": "Remember our project"}),
        (
            AgentStreamEventType.TURN_COMPLETED,
            {"assistant_text": "I remember it", "status": "completed"},
        ),
    ):
        await stream.append(
            AgentStreamEvent(
                stream_sequence=0,
                session_id=f"terminal-tab-{tab_id}",
                tab_id=tab_id,
                agent_type=AgentType.CLAUDE,
                turn_id="turn-1",
                type=kind,
                payload=payload,
                created_at=datetime.now(timezone.utc),
            )
        )
    result = await execution.switch_execution(
        setup.service,
        expected_revision=result["revision"],
        agent_type=AgentType.MASO,
        env={"MASO_BASE_URL": "http://localhost:9000"},
    )
    assert result["tab_id"] != tab_id
    assert await stream.count() == 2
    journal = "\n".join(setup.service.store.recent_journal())
    assert "Remember our project" in journal and "I remember it" in journal
    assert "agent_session_id" not in setup.created[-1]


def test_legacy_setup_cannot_seed_claude_credentials_into_other_backends(
    setup: SimpleNamespace,
) -> None:
    assert apply(setup, "codex").status_code == 200
    response = setup.client.post(
        "/api/dot/setup", json={"env": {"ANTHROPIC_API_KEY": "never-copy"}}
    )
    assert response.status_code == 200, response.text
    assert setup.tabs[setup.service.tab_id()].env == {}


@pytest.mark.asyncio
async def test_concurrent_switches_share_setup_lock_and_reject_stale_draft(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, resume = asyncio.Event(), asyncio.Event()

    async def gated_create(**kwargs: Any) -> Any:
        entered.set()
        await resume.wait()
        return await setup.create(**kwargs)

    monkeypatch.setattr(ttyd_manager, "create_tab", gated_create)
    revision = execution.execution_view(setup.service)["revision"]
    first = asyncio.create_task(
        execution.switch_execution(
            setup.service, expected_revision=revision, agent_type=AgentType.CODEX, env={}
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        execution.switch_execution(
            setup.service, expected_revision=revision, agent_type=AgentType.MASO, env={}
        )
    )
    resume.set()
    assert (await first)["agent_type"] == "codex"
    with pytest.raises(execution.DotExecutionConflict, match="configuration changed"):
        await second
    assert len(setup.created) == 1


@pytest.mark.asyncio
async def test_setup_recovers_interrupted_switch_and_keeps_disabled_schedule(
    setup: SimpleNamespace,
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    state = setup.service.store.read_state()
    wake_id = state["wake_schedule_id"]
    workspace_manager.scheduled_tasks[wake_id].enabled = False
    snapshot = execution._schedule_snapshot(setup.service)
    candidate = await setup.create(
        name="Dot",
        cwd=str(setup.service.store.home),
        solo_mode=True,
        session_kind=setup.tabs[old["tab_id"]].session_kind,
        agent_type=AgentType.MASO,
        env={},
    )
    setup.service.store.update_state(
        pending_execution={
            "candidate_tab_id": candidate.id,
            "previous_tab_id": old["tab_id"],
            "previous_archived": False,
            "schedules": snapshot,
        }
    )
    setup.service.ensure_schedules(candidate.id, migrate=True)
    await setup.archive(old["tab_id"])
    assert execution.execution_pending_for(setup.service, candidate.id)
    candidate_run = scheduled_run(wake_id, candidate.id, "candidate-wake")
    old_run = scheduled_run(wake_id, old["tab_id"], "old-wake")
    workspace_manager.scheduled_task_runs[candidate_run.id] = candidate_run
    workspace_manager.scheduled_task_runs[old_run.id] = old_run
    assert await setup.service.ensure_tab() == old["tab_id"]
    assert candidate_run.status == ScheduledTaskRunStatus.CANCELLED
    assert old_run.status == ScheduledTaskRunStatus.WAITING
    old_run.status = ScheduledTaskRunStatus.CANCELLED
    assert not setup.service._wake_in_flight()
    assert candidate.archived and not setup.tabs[old["tab_id"]].archived
    assert workspace_manager.scheduled_tasks[wake_id].tab_id == old["tab_id"]
    assert not workspace_manager.scheduled_tasks[wake_id].enabled
    assert setup.service.store.read_state().get("pending_execution") is None


@pytest.mark.asyncio
async def test_cancellation_finishes_rollback_before_releasing_old_chat(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    entered, release_cleanup = asyncio.Event(), asyncio.Event()

    async def archive(tab_id: str) -> Any:
        if tab_id == old["tab_id"]:
            setup.tabs[tab_id].archived = True
            entered.set()
            await asyncio.Event().wait()
        await release_cleanup.wait()
        return await setup.archive(tab_id)

    monkeypatch.setattr(ttyd_manager, "archive_tab", archive)
    operation = asyncio.create_task(
        execution.switch_execution(
            setup.service, expected_revision=old["revision"], agent_type=AgentType.CODEX, env={}
        )
    )
    await entered.wait()
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert setup.service.tab_id() == old["tab_id"]
    assert not setup.tabs[old["tab_id"]].archived
    assert setup.tabs["dot-2"].archived
    assert setup.service.store.read_state().get("pending_execution") is None


@pytest.mark.asyncio
async def test_late_old_tab_input_is_rejected_after_switch(setup: SimpleNamespace) -> None:
    from fastapi import HTTPException

    from claude_hub.api.routes.agent_stream.dispatch import (
        AgentStreamSendRequest,
        dispatch_tab_stream_input,
    )

    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    await execution.switch_execution(
        setup.service, expected_revision=old["revision"], agent_type=AgentType.CODEX, env={}
    )
    with pytest.raises(HTTPException, match="archived") as captured:
        await dispatch_tab_stream_input(
            old["tab_id"],
            AgentStreamSendRequest(text="Late message", client_turn_id="late"),
            SimpleNamespace(),
        )
    assert captured.value.status_code == 409


@pytest.mark.asyncio
async def test_missing_previous_tab_late_failure_rolls_back_and_retries(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    old_id = first["tab_id"]
    state = setup.service.store.read_state()
    wake_id = state["wake_schedule_id"]
    tidy_id = state["tidy_schedule_id"]
    wake = workspace_manager.scheduled_tasks[wake_id]
    tidy = workspace_manager.scheduled_tasks[tidy_id]
    wake.enabled = False
    wake.message = "Keep this wake text"
    wake.interval_seconds = 777
    tidy.message = "Keep this tidy text"
    tidy.cron = "45 4 * * *"
    setup.tabs.pop(old_id)
    missing_revision = execution.execution_view(setup.service)["revision"]

    original_update = setup.service.store.update_state
    fail_once = True

    def fail_final_identity(**changes: Any) -> Any:
        nonlocal fail_once
        if fail_once and changes.get("execution_agent_type") == "codex":
            fail_once = False
            raise RuntimeError("late state write failed")
        return original_update(**changes)

    monkeypatch.setattr(setup.service.store, "update_state", fail_final_identity)
    with pytest.raises(RuntimeError, match="late state write failed"):
        await execution.switch_execution(
            setup.service,
            expected_revision=missing_revision,
            agent_type=AgentType.CODEX,
            env={},
        )

    assert setup.service.store.read_state().get("pending_execution") is None
    assert setup.tabs["dot-2"].archived
    wake = workspace_manager.scheduled_tasks[wake_id]
    tidy = workspace_manager.scheduled_tasks[tidy_id]
    assert (wake.tab_id, wake.enabled, wake.agent_type) == (
        old_id,
        False,
        AgentType.CLAUDE,
    )
    assert (wake.message, wake.interval_seconds) == ("Keep this wake text", 777)
    assert (tidy.tab_id, tidy.agent_type, tidy.message, tidy.cron) == (
        old_id,
        AgentType.CLAUDE,
        "Keep this tidy text",
        "45 4 * * *",
    )

    retried = await execution.switch_execution(
        setup.service,
        expected_revision=missing_revision,
        agent_type=AgentType.CODEX,
        env={},
    )
    assert retried["agent_type"] == "codex"
    assert workspace_manager.scheduled_tasks[wake_id].tab_id == retried["tab_id"]
    assert workspace_manager.scheduled_tasks[wake_id].agent_type == AgentType.CODEX


@pytest.mark.asyncio
async def test_edit_resend_sequence_rewind_orphan_blocks_switch(setup: SimpleNamespace) -> None:
    first = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    tab_id = first["tab_id"]
    stream = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")

    async def append(
        event_type: AgentStreamEventType, turn_id: str, payload: dict[str, Any]
    ) -> None:
        await stream.append(
            AgentStreamEvent(
                stream_sequence=0,
                session_id=f"terminal-tab-{tab_id}",
                tab_id=tab_id,
                agent_type=AgentType.CLAUDE,
                turn_id=turn_id,
                type=event_type,
                payload=payload,
                created_at=datetime.now(timezone.utc),
            )
        )

    await append(AgentStreamEventType.TURN_STARTED, "one", {"summary": "one"})
    await append(
        AgentStreamEventType.TURN_COMPLETED,
        "one",
        {"assistant_text": "done", "status": "completed"},
    )
    await append(AgentStreamEventType.TURN_STARTED, "two", {"summary": "two"})
    await append(
        AgentStreamEventType.TURN_COMPLETED,
        "two",
        {"assistant_text": "done", "status": "completed"},
    )
    await setup.service.sync_journal()
    assert setup.service.store.read_state()["journal_seq"] == 3

    await stream.truncate_before(2)
    await append(
        AgentStreamEventType.TURN_STARTED,
        "edited-orphan",
        {"summary": "edited"},
    )
    with pytest.raises(execution.DotExecutionConflict, match="unfinished turn"):
        await execution.switch_execution(
            setup.service,
            expected_revision=first["revision"],
            agent_type=AgentType.CODEX,
            env={},
        )
    assert setup.service.tab_id() == tab_id
    assert not setup.tabs[tab_id].archived
    assert len(setup.created) == 1


@pytest.mark.asyncio
async def test_old_archive_api_waits_before_pending_marker(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import tabs as tabs_api

    first = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_candidate_creation(**kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await setup.create(**kwargs)

    monkeypatch.setattr(ttyd_manager, "create_tab", block_candidate_creation)
    switch = asyncio.create_task(
        execution.switch_execution(
            setup.service,
            expected_revision=first["revision"],
            agent_type=AgentType.CODEX,
            env={},
        )
    )
    await entered.wait()
    assert setup.service.store.read_state().get("pending_execution") is None
    user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)
    archive = asyncio.create_task(tabs_api.archive_tab(first["tab_id"], user))
    await asyncio.sleep(0)
    assert not archive.done()
    assert not setup.tabs[first["tab_id"]].archived

    release.set()
    switched = await switch
    await archive
    assert switched["tab_id"] != first["tab_id"]
    assert setup.tabs[first["tab_id"]].archived


@pytest.mark.asyncio
async def test_candidate_archive_api_waits_for_execution_switch(setup: SimpleNamespace) -> None:
    from claude_hub.api import tabs as tabs_api

    first = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_old_retirement(_session_id: str) -> None:
        entered.set()
        await release.wait()

    setup.manager.forget_session.side_effect = block_old_retirement
    switch = asyncio.create_task(
        execution.switch_execution(
            setup.service,
            expected_revision=first["revision"],
            agent_type=AgentType.CODEX,
            env={},
        )
    )
    await entered.wait()
    candidate = setup.service.store.read_state()["pending_execution"]["candidate_tab_id"]
    user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)
    archive = asyncio.create_task(tabs_api.archive_tab(candidate, user))
    await asyncio.sleep(0)
    assert not archive.done()
    assert not setup.tabs[candidate].archived

    release.set()
    switched = await switch
    assert switched["tab_id"] == candidate
    await archive
    assert setup.tabs[candidate].archived


def test_generic_dot_execution_mutations_use_dedicated_api(setup: SimpleNamespace) -> None:
    assert apply(setup, "claude").status_code == 200
    tab_id = setup.service.tab_id()
    assert tab_id is not None

    provider = setup.client.put(f"/api/tabs/{tab_id}", json={"agent_type": "codex"})
    assert provider.status_code == 409
    assert "Dot" in provider.json()["detail"] and "Execution backend" in provider.json()["detail"]
    env = setup.client.put(f"/api/tabs/{tab_id}", json={"env": {"ANTHROPIC_MODEL": "changed"}})
    assert env.status_code == 409
    hot_swap = setup.client.post(
        f"/api/tabs/{tab_id}/switch-env", json={"env": {"ANTHROPIC_API_KEY": "changed"}}
    )
    assert hot_swap.status_code == 409
    assert setup.tabs[tab_id].agent_type == AgentType.CLAUDE
    assert setup.tabs[tab_id].env == {}


@pytest.mark.parametrize(
    ("provider", "model_env"),
    [
        ("claude", {"ANTHROPIC_MODEL": "model-next"}),
        ("codex", {"CODEX_MODEL": "model-next", "CODEX_REASONING_EFFORT": "high"}),
        ("cursor", {"CURSOR_MODEL": "model-next"}),
        ("antigravity", {"ANTIGRAVITY_MODEL": "model-next"}),
        ("traex", {"CODEX_MODEL": "model-next", "CODEX_REASONING_EFFORT": "high"}),
        ("maso", {"MASO_MODEL": "model-next"}),
    ],
)
def test_dot_composer_model_picker_preserves_execution_ownership(
    setup: SimpleNamespace, provider: str, model_env: dict[str, str]
) -> None:
    initial_env = {"KEEP": "configured-value"}
    if provider == "traex":
        initial_env["TRAEX_REASONING_EFFORT"] = "low"
    assert apply(setup, provider, initial_env).status_code == 200
    tab_id = setup.service.tab_id()
    before = execution.execution_view(setup.service)
    schedules = execution._schedule_snapshot(setup.service)
    env = {"KEEP": "configured-value", **model_env}

    response = setup.client.post(f"/api/tabs/{tab_id}/switch-env", json={"env": env})

    assert response.status_code == 200, response.text
    assert response.json()["env"] == env
    assert setup.service.tab_id() == tab_id
    assert setup.service.store.read_state()["execution_agent_type"] == provider
    assert execution._schedule_snapshot(setup.service) == schedules
    assert execution.execution_view(setup.service)["revision"] != before["revision"]
    assert len(setup.created) == 1

    for payload in (
        {"env": {**env, "KEEP": "changed-credential"}},
        {"env": {**env, "ANTHROPIC_BASE_URL": "https://changed.invalid"}},
        {"env": env, "solo_mode": False},
    ):
        rejected = setup.client.post(f"/api/tabs/{tab_id}/switch-env", json=payload)
        assert rejected.status_code == 409, rejected.text
        assert setup.tabs[tab_id].env == env


def test_legacy_setup_still_seeds_current_claude_env(setup: SimpleNamespace) -> None:
    assert apply(setup, "claude").status_code == 200
    tab_id = setup.service.tab_id()
    response = setup.client.post(
        "/api/dot/setup", json={"env": {"ANTHROPIC_API_KEY": "setup-secret"}}
    )
    assert response.status_code == 200, response.text
    assert setup.service.tab_id() == tab_id
    assert setup.tabs[tab_id].env == {"ANTHROPIC_API_KEY": "setup-secret"}


@pytest.mark.asyncio
async def test_setup_env_seed_rechecks_current_tab_after_competing_switch(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api.dot import SetupRequest, dot_setup

    first = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    original_ensure = setup.service.ensure_tab
    ensured = asyncio.Event()
    release = asyncio.Event()

    async def gated_ensure(env: dict[str, str] | None = None) -> str:
        tab_id = await original_ensure(env)
        ensured.set()
        await release.wait()
        return str(tab_id)

    monkeypatch.setattr(setup.service, "ensure_tab", gated_ensure)
    user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)
    setup_call = asyncio.create_task(
        dot_setup(SetupRequest(env={"ANTHROPIC_API_KEY": "stale-secret"}), user)
    )
    await ensured.wait()
    switched = await execution.switch_execution(
        setup.service,
        expected_revision=first["revision"],
        agent_type=AgentType.CODEX,
        env={"CODEX_MODEL": "new"},
    )
    release.set()
    result = await setup_call

    assert result["tab_id"] == switched["tab_id"]
    assert setup.tabs[switched["tab_id"]].env == {"CODEX_MODEL": "new"}
    assert "stale-secret" not in str(setup.tabs[switched["tab_id"]].env)


@pytest.mark.asyncio
async def test_deleted_dot_tab_pending_run_blocks_migration_until_settled(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert apply(setup, "claude").status_code == 200
    old_id = setup.service.tab_id()
    assert old_id is not None
    wake_id = setup.service.store.read_state()["wake_schedule_id"]
    run = scheduled_run(wake_id, old_id, "deleted-tab-run")
    workspace_manager.scheduled_task_runs[run.id] = run

    async def delete_tab(tab_id: str) -> bool:
        # The tab owner removes its record; schedules/runs are separate owners.
        return setup.tabs.pop(tab_id, None) is not None

    monkeypatch.setattr(ttyd_manager, "delete_tab", delete_tab)
    deleted = setup.client.delete(f"/api/tabs/{old_id}")
    assert deleted.status_code == 204, deleted.text
    assert workspace_manager.scheduled_task_runs[run.id].status == ScheduledTaskRunStatus.WAITING

    rejected = apply(setup, "codex")
    assert rejected.status_code == 409, rejected.text
    assert "pending scheduled turns" in rejected.json()["detail"]
    assert len(setup.created) == 1
    assert workspace_manager.scheduled_tasks[wake_id].tab_id == old_id

    workspace_manager.drop_pending_scheduled_task_runs(wake_id, "settled", tab_id=old_id)
    switched = apply(setup, "codex")
    assert switched.status_code == 200, switched.text
    new_id = switched.json()["tab_id"]
    await workspace_manager._drain_scheduled_chat_runs(old_id)
    assert workspace_manager.scheduled_tasks[wake_id].tab_id == new_id
    assert workspace_manager.scheduled_tasks[wake_id].enabled


def custom_schedules(tab_id: str) -> dict[str, dict[str, Any]]:
    now = datetime.now()
    result = {}
    for name, enabled, timing in (
        ("recurring", True, {"interval_seconds": 1234}),
        ("disabled", False, {"cron": "17 8 * * *"}),
        ("one-shot", True, {"run_at": now + timedelta(days=1)}),
        ("completed", False, {"run_at": now - timedelta(days=1)}),
    ):
        task = workspace_manager.create_scheduled_task(
            ScheduledTaskCreate(
                name=name,
                kind=ScheduledTaskKind.CHAT_TURN,
                tab_id=tab_id,
                message=f"User requested {name}",
                enabled=enabled,
                **timing,
            )
        )
        task.run_count = 4
        task.last_run_at = now - timedelta(days=2)
        result[task.id] = task.model_dump(exclude={"updated_at"})
    return result


def assert_schedule_targets(
    before: dict[str, dict[str, Any]], tab_id: str, provider: AgentType
) -> None:
    for schedule_id, fields in before.items():
        assert workspace_manager.scheduled_tasks[schedule_id].model_dump(
            exclude={"updated_at"}
        ) == {**fields, "tab_id": tab_id, "agent_type": provider}


@pytest.mark.parametrize("deleted", [False, True], ids=["switch", "deleted-tab"])
@pytest.mark.asyncio
async def test_all_custom_chat_schedules_follow_dot_without_rearming(
    setup: SimpleNamespace, deleted: bool
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    before = custom_schedules(old["tab_id"])
    unrelated_tab = await setup.create(**setup.created[0])
    unrelated = custom_schedules(unrelated_tab.id)
    finished = scheduled_run(next(iter(before)), old["tab_id"], "historical")
    finished.status = ScheduledTaskRunStatus.COMPLETED
    workspace_manager.scheduled_task_runs[finished.id] = finished
    if deleted:
        setup.tabs.pop(old["tab_id"])
        new_id = await setup.service.ensure_tab()
        provider = AgentType.CLAUDE
    else:
        replacement = await execution.switch_execution(
            setup.service,
            expected_revision=old["revision"],
            agent_type=AgentType.MASO,
            env={},
        )
        new_id, provider = replacement["tab_id"], AgentType.MASO
    assert new_id != old["tab_id"]
    assert_schedule_targets(before, new_id, provider)
    assert_schedule_targets(unrelated, unrelated_tab.id, AgentType.CLAUDE)
    assert finished.tab_id == old["tab_id"], "history is not rewritten"
    assert all(not setup.service.is_system_schedule(key) for key in before)
    assert not set(before).intersection(setup.service.store.read_state()), "no custom state keys"


@pytest.mark.parametrize("failure", ["partial", "commit", "deleted-tab"])
@pytest.mark.asyncio
async def test_custom_migration_failure_rolls_back_and_is_retryable(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    before = custom_schedules(old["tab_id"])
    update_schedule = workspace_manager.update_scheduled_task
    update_state = setup.service.store.update_state
    if failure == "deleted-tab":
        setup.tabs.pop(old["tab_id"])

    def fail_schedule(schedule_id: str, payload: ScheduledTaskUpdate) -> Any:
        result = update_schedule(schedule_id, payload)
        if schedule_id == next(iter(before)):
            raise RuntimeError("migration failed")
        return result

    def fail_commit(**changes: Any) -> Any:
        if "execution_agent_type" in changes:
            raise RuntimeError("migration failed")
        return update_state(**changes)

    if failure == "partial":
        monkeypatch.setattr(workspace_manager, "update_scheduled_task", fail_schedule)
    else:
        monkeypatch.setattr(setup.service.store, "update_state", fail_commit)
    with pytest.raises(RuntimeError, match="migration failed"):
        if failure == "deleted-tab":
            await setup.service.ensure_tab()
        else:
            await execution.switch_execution(
                setup.service,
                expected_revision=old["revision"],
                agent_type=AgentType.MASO,
                env={},
            )
    assert_schedule_targets(before, old["tab_id"], AgentType.CLAUDE)
    assert setup.service.tab_id() == old["tab_id"]
    assert setup.service.store.read_state().get("pending_execution") is None
    assert setup.tabs["dot-2"].archived
    monkeypatch.setattr(workspace_manager, "update_scheduled_task", update_schedule)
    monkeypatch.setattr(setup.service.store, "update_state", update_state)
    if failure == "deleted-tab":
        new_id = await setup.service.ensure_tab()
        provider = AgentType.CLAUDE
    else:
        result = await execution.switch_execution(
            setup.service,
            expected_revision=old["revision"],
            agent_type=AgentType.MASO,
            env={},
        )
        new_id, provider = result["tab_id"], AgentType.MASO
    assert_schedule_targets(before, new_id, provider)


@pytest.mark.asyncio
async def test_interrupted_custom_migration_recovers_and_cancels_only_candidate_runs(
    setup: SimpleNamespace,
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    before = custom_schedules(old["tab_id"])
    candidate = await setup.create(**{**setup.created[0], "agent_type": AgentType.MASO})
    setup.service.store.update_state(
        pending_execution={
            "candidate_tab_id": candidate.id,
            "previous_tab_id": old["tab_id"],
            "previous_archived": False,
            "schedules": execution._schedule_snapshot(setup.service),
        }
    )
    setup.service.ensure_schedules(candidate.id, migrate=True)
    await setup.archive(old["tab_id"])
    schedule_id = next(iter(before))
    candidate_run = scheduled_run(schedule_id, candidate.id, "candidate-custom")
    old_run = scheduled_run(schedule_id, old["tab_id"], "old-custom")
    workspace_manager.scheduled_task_runs.update(
        {candidate_run.id: candidate_run, old_run.id: old_run}
    )
    assert await setup.service.ensure_tab() == old["tab_id"]
    assert_schedule_targets(before, old["tab_id"], AgentType.CLAUDE)
    assert candidate_run.status == ScheduledTaskRunStatus.CANCELLED
    assert old_run.status == ScheduledTaskRunStatus.WAITING
    assert candidate.archived


@pytest.mark.asyncio
async def test_custom_schedule_created_during_retirement_is_in_rollback_snapshot(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    before = {}

    async def create_during_retirement(_session_id: str) -> None:
        before.update(custom_schedules(old["tab_id"]))

    setup.manager.forget_session.side_effect = create_during_retirement
    update = setup.service.store.update_state

    def fail_commit(**changes: Any) -> Any:
        if changes.get("execution_agent_type") == "maso":
            raise RuntimeError("commit failed")
        return update(**changes)

    monkeypatch.setattr(setup.service.store, "update_state", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await execution.switch_execution(
            setup.service,
            expected_revision=old["revision"],
            agent_type=AgentType.MASO,
            env={},
        )
    assert len(before) == 4
    assert_schedule_targets(before, old["tab_id"], AgentType.CLAUDE)


def test_pending_custom_run_still_blocks_dot_replacement(setup: SimpleNamespace) -> None:
    assert apply(setup, "claude").status_code == 200
    tab_id = setup.service.tab_id()
    before = custom_schedules(tab_id)
    run = scheduled_run(next(iter(before)), tab_id, "pending-custom")
    workspace_manager.scheduled_task_runs[run.id] = run
    assert apply(setup, "maso").status_code == 409
    assert_schedule_targets(before, tab_id, AgentType.CLAUDE)
    assert run.status == ScheduledTaskRunStatus.WAITING


@pytest.mark.parametrize("edit", ["disable", "retarget"])
@pytest.mark.parametrize("phase", ["retirement-failure", "recovery-before", "recovery-after"])
@pytest.mark.asyncio
async def test_rollback_preserves_concurrent_custom_schedule_edits(
    setup: SimpleNamespace, edit: str, phase: str
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    before = custom_schedules(old["tab_id"])
    schedule_id = next(iter(before))
    unrelated = await setup.create(**setup.created[0])
    update = (
        ScheduledTaskUpdate(enabled=False)
        if edit == "disable"
        else ScheduledTaskUpdate(tab_id=unrelated.id)
    )

    if phase == "retirement-failure":

        async def fail_retirement(_session_id: str) -> None:
            workspace_manager.update_scheduled_task(schedule_id, update)
            raise RuntimeError("retirement failed")

        setup.manager.forget_session.side_effect = fail_retirement
        with pytest.raises(RuntimeError, match="retirement failed"):
            await execution.switch_execution(
                setup.service,
                expected_revision=old["revision"],
                agent_type=AgentType.MASO,
                env={},
            )
    else:
        candidate = await setup.create(**{**setup.created[0], "agent_type": AgentType.MASO})
        setup.service.store.update_state(
            pending_execution={
                "candidate_tab_id": candidate.id,
                "previous_tab_id": old["tab_id"],
                "previous_archived": False,
                "schedules": execution._schedule_snapshot(setup.service),
            }
        )
        if phase == "recovery-after":
            setup.service.ensure_schedules(candidate.id, migrate=True)
        workspace_manager.update_scheduled_task(schedule_id, update)
        assert await setup.service.ensure_tab() == old["tab_id"]

    expected = before[schedule_id]
    if edit == "disable":
        expected["enabled"] = False
    else:
        expected["tab_id"] = unrelated.id
    assert (
        workspace_manager.scheduled_tasks[schedule_id].model_dump(exclude={"updated_at"})
        == expected
    )
    assert setup.service.store.read_state().get("pending_execution") is None


@pytest.mark.parametrize("migrated", [False, True], ids=["previous", "candidate"])
@pytest.mark.asyncio
async def test_pending_replacement_does_not_consume_a_due_one_shot(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, migrated: bool
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    due = datetime.now() + timedelta(hours=1)
    task = workspace_manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="One reminder",
            kind=ScheduledTaskKind.CHAT_TURN,
            tab_id=old["tab_id"],
            run_at=due,
            message="Do not lose this reminder",
        )
    )
    candidate = await setup.create(**{**setup.created[0], "agent_type": AgentType.MASO})
    setup.service.store.update_state(
        pending_execution={
            "candidate_tab_id": candidate.id,
            "previous_tab_id": old["tab_id"],
            "previous_archived": False,
            "schedules": execution._schedule_snapshot(setup.service),
        }
    )
    if migrated:
        setup.service.ensure_schedules(candidate.id, migrate=True)
    # Exercise real timer stamping and queue creation, not provider delivery.
    drain = AsyncMock()
    monkeypatch.setattr(workspace_manager, "_drain_scheduled_chat_runs", drain)
    task = workspace_manager.scheduled_tasks[task.id]
    before = task.model_dump()
    assert await workspace_manager._fire_scheduled_task(task, due) is None
    assert workspace_manager.scheduled_tasks[task.id].model_dump() == before
    with pytest.raises(RuntimeError, match="Dot.*replacement"):
        await workspace_manager.run_scheduled_task(task.id)
    assert not workspace_manager.scheduled_task_runs
    drain.assert_not_awaited()

    assert await setup.service.ensure_tab() == old["tab_id"]
    task = workspace_manager.scheduled_tasks[task.id]
    run = await workspace_manager._fire_scheduled_task(task, due)
    assert run is not None and run.tab_id == old["tab_id"]
    assert task.run_count == 1 and not task.enabled and task.next_run_at is None
    assert await workspace_manager._fire_scheduled_task(task, due + timedelta(seconds=5)) is None
    assert len(workspace_manager.scheduled_task_runs) == 1
    drain.assert_awaited_once_with(old["tab_id"])


@pytest.mark.parametrize("interrupted", [False, True], ids=["deleted", "rolled-back"])
@pytest.mark.asyncio
async def test_deleted_dot_schedules_survive_tick_before_setup(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    custom = custom_schedules(old["tab_id"])
    before = {
        key: task.model_dump(exclude={"updated_at"})
        for key, task in workspace_manager.scheduled_tasks.items()
    }
    snapshot = execution._schedule_snapshot(setup.service)
    setup.tabs.pop(old["tab_id"])
    if interrupted:
        candidate = await setup.create(**setup.created[0])
        setup.service.store.update_state(
            pending_execution={
                "candidate_tab_id": candidate.id,
                "previous_tab_id": old["tab_id"],
                "previous_archived": False,
                "schedules": snapshot,
            }
        )
        setup.service.ensure_schedules(candidate.id, migrate=True)
        async with setup.service.setup_lock():
            await execution.recover_execution_locked(setup.service)
    assert setup.service.store.read_state().get("pending_execution") is None
    monkeypatch.setattr(workspace_manager, "_scheduled_chat_recovery_pending", False)
    monkeypatch.setattr(workspace_manager, "_reap_stale_scheduled_chat_runs", AsyncMock())
    monkeypatch.setattr(workspace_manager, "_drain_scheduled_chat_runs", AsyncMock())
    monkeypatch.setattr(
        workspace_manager, "_runtime_now", lambda: datetime.now() + timedelta(days=2)
    )
    await workspace_manager._tick_scheduled_tasks()
    assert_schedule_targets(before, old["tab_id"], AgentType.CLAUDE)
    assert not workspace_manager.scheduled_task_runs
    with pytest.raises(RuntimeError, match="Dot.*replacement"):
        await workspace_manager.run_scheduled_task(next(iter(custom)))
    new_id = await setup.service.ensure_tab()
    assert_schedule_targets(custom, new_id, AgentType.CLAUDE)
    for key in ("wake_schedule_id", "tidy_schedule_id"):
        task = workspace_manager.scheduled_tasks[setup.service.store.read_state()[key]]
        assert task.tab_id == new_id and task.enabled
        assert task.next_run_at is not None and task.next_run_at > workspace_manager._runtime_now()
    assert await setup.service.wake(), "the standing wake remains usable"


@pytest.mark.asyncio
async def test_deleted_dot_setup_upgrades_retired_standing_defaults(setup: SimpleNamespace) -> None:
    from claude_hub.services.dot.prompts import (
        RETIRED_TIDY_MESSAGES,
        RETIRED_WAKE_MESSAGES,
        TIDY_MESSAGE,
        WAKE_MESSAGE,
    )

    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    state = setup.service.store.read_state()
    wake = workspace_manager.scheduled_tasks[state["wake_schedule_id"]]
    tidy = workspace_manager.scheduled_tasks[state["tidy_schedule_id"]]
    wake.interval_seconds, wake.cron = None, "0 9 * * *"
    wake.message = RETIRED_WAKE_MESSAGES[0]
    tidy.cron, tidy.message = "12 5 * * *", RETIRED_TIDY_MESSAGES[0]
    custom = custom_schedules(old["tab_id"])
    setup.tabs.pop(old["tab_id"])
    new_id = await setup.service.ensure_tab()
    wake = workspace_manager.scheduled_tasks[wake.id]
    tidy = workspace_manager.scheduled_tasks[tidy.id]
    assert wake.enabled and wake.message == WAKE_MESSAGE
    assert wake.interval_seconds == setup.service._wake_interval_seconds() and wake.cron is None
    assert tidy.enabled and tidy.message == TIDY_MESSAGE and tidy.cron == "12 5 * * *"
    assert_schedule_targets(custom, new_id, AgentType.CLAUDE)


def test_deleted_dot_setup_conflict_is_http_409(setup: SimpleNamespace) -> None:
    assert apply(setup, "claude").status_code == 200
    old_id = setup.service.tab_id()
    state = setup.service.store.read_state()
    run = scheduled_run(state["wake_schedule_id"], old_id, "pending-deleted-dot")
    workspace_manager.scheduled_task_runs[run.id] = run
    setup.tabs.pop(old_id)
    response = setup.client.post("/api/dot/setup", json={})
    assert response.status_code == 409
    assert "pending scheduled turns" in response.json()["detail"]
    assert setup.service.tab_id() == old_id and len(setup.created) == 1


@pytest.mark.parametrize("failure", ["runtime", "maso", "startup"])
def test_deleted_dot_setup_runtime_error_is_http_503(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable
    from claude_hub.services.ttyd_manager import TabStartupTimeoutError

    assert apply(setup, "claude").status_code == 200
    old_id = setup.service.tab_id()
    setup.tabs.pop(old_id)
    error_type = {
        "runtime": RuntimeError,
        "maso": MasoServiceUnavailable,
        "startup": TabStartupTimeoutError,
    }[failure]
    monkeypatch.setattr(
        ttyd_manager, "create_tab", AsyncMock(side_effect=error_type("replacement unavailable"))
    )
    response = setup.client.post("/api/dot/setup", json={})
    assert response.status_code == 503
    assert "replacement unavailable" in response.json()["detail"]
    assert setup.service.tab_id() == old_id


@pytest.mark.asyncio
async def test_deleted_dot_setup_rearms_standing_schedules_but_not_custom_ones(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = await execution.switch_execution(
        setup.service,
        expected_revision=execution.execution_view(setup.service)["revision"],
        agent_type=AgentType.CLAUDE,
        env={},
    )
    custom = custom_schedules(old["tab_id"])
    state = setup.service.store.read_state()
    stale = datetime.now() - timedelta(days=1)
    for key in ("wake_schedule_id", "tidy_schedule_id"):
        task = workspace_manager.scheduled_tasks[state[key]]
        task.enabled = False
        task.last_error = "target Chat was deleted"
        task.next_run_at = stale
    setup.tabs.pop(old["tab_id"])
    new_id = await setup.service.ensure_tab()
    for key in ("wake_schedule_id", "tidy_schedule_id"):
        task = workspace_manager.scheduled_tasks[state[key]]
        assert task.tab_id == new_id and task.enabled
        assert task.next_run_at is not None and task.next_run_at > datetime.now()
    assert_schedule_targets(custom, new_id, AgentType.CLAUDE)
    monkeypatch.setattr(workspace_manager, "_drain_scheduled_chat_runs", AsyncMock())
    assert await setup.service.wake()
