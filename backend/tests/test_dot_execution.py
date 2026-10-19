"""Dot execution API, credential replacement and tab/schedule ownership."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone
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
            id=f"dot-{len(created)}", archived=False, workspace_role=None, **kwargs
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

    monkeypatch.setattr(ttyd_manager, "create_tab", create)
    monkeypatch.setattr(ttyd_manager, "get_tab", tabs.get)
    monkeypatch.setattr(ttyd_manager, "archive_tab", archive)
    monkeypatch.setattr(ttyd_manager, "unarchive_tab", unarchive)
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
    assert result.json()["supported_agent_types"] == ["claude", "codex", "cursor", "traex", "maso"]
    assert setup.service.store.read_state() == before
    assert setup.created == []


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
                name="External schedule",
                kind=ScheduledTaskKind.CHAT_TURN,
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
