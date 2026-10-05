"""Chat work contracts with real task/report lifecycle and hermetic terminal I/O."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli
from claude_hub.models import AgentType, ExecutionTarget, SessionKind, WorkspaceTaskStatus
from claude_hub.models.schemas import ChatWorkCreate, ChatWorkReport, ChatWorkUpdate
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_scheduled_tasks import (
    AsyncNoop,
    _install_live_terminal_fakes,
    _make_workspace,
    manager,
    state_root,
)


@pytest.fixture()
def setup_work(manager, tmp_path, monkeypatch):
    workspace = _make_workspace(manager, tmp_path)
    recorded = _install_live_terminal_fakes(monkeypatch, tmp_path)
    source = SimpleNamespace(
        id="source-chat",
        session_kind=SessionKind.CHAT,
        workspace_role=None,
        agent_type=AgentType.CODEX,
        cwd=workspace.path,
        target=ExecutionTarget.LOCAL,
        env={"CODEX_MODEL": "explicit-test-model"},
    )
    monkeypatch.setattr(
        ttyd_manager, "get_tab", lambda tab_id: source if tab_id == source.id else None
    )
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncNoop.dispatch_noop)

    def payload(**kwargs):
        return ChatWorkCreate(
            request_key="watch-one",
            workspace_id=workspace.id,
            title="Watch eval",
            prompt="Observe eval and report changes",
            kind="monitor",
            interval_seconds=60,
            **kwargs,
        )

    return workspace, source, payload, recorded


async def test_retries_overlap_and_launch_settings(manager, setup_work, monkeypatch):
    workspace, source, payload, recorded = setup_work
    first, second = await asyncio.gather(
        manager.create_chat_work(source.id, payload()),
        manager.create_chat_work(source.id, payload()),
    )
    assert first.id == second.id
    assert len(manager.tasks) == 1
    work = manager.scheduled_tasks[first.id]
    task = next(iter(manager.tasks.values()))
    session = manager.sessions[task.session_id]
    assert session.agent_type == AgentType.CODEX
    assert session.workspace_path == workspace.path
    assert session.env["CODEX_MODEL"] == "explicit-test-model"
    assert task.source_work_id == work.id
    assert session.ephemeral and session.caller_owned_ephemeral
    await manager._fire_scheduled_task(work, datetime.now() + timedelta(minutes=3))
    assert work.run_count == 1 and len(manager.tasks) == 1
    assert work.next_run_at is not None
    with pytest.raises(ValueError, match="different input"):
        await manager.create_chat_work(source.id, payload(model="other"))
    reloaded = WorkspaceManager()
    assert reloaded.list_chat_work(source.id)[0].active_task_id == task.id
    assert reloaded.tasks[task.id].source_work_id == work.id


async def test_report_completes_check_quietly_and_retries_after_cleanup(manager, setup_work):
    _, source, payload, recorded = setup_work
    created = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[created.active_task_id]
    worker_id = task.session_id
    body = ChatWorkReport(
        task_id=task.id,
        session_id=worker_id,
        kind="no_change",
        summary="Still running",
        validation="Job status remains RUNNING",
    )
    view = await manager.report_chat_work(source.id, created.id, body)
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
    assert worker_id not in manager.sessions
    assert view.latest_result is None
    assert view.active_task_id is None
    assert view.executions[0].outcome == "no_change"
    again = await manager.report_chat_work(source.id, created.id, body)
    assert again.executions == view.executions
    assert len([r for r in manager.reports.values() if r.chat_work_outcome == "no_change"]) == 1
    assert manager.scheduled_tasks[created.id].enabled
    assert recorded["deleted_tabs"]


async def test_objective_completion_disables_monitor_with_report_evidence(manager, setup_work):
    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[created.active_task_id]
    view = await manager.report_chat_work(
        source.id,
        created.id,
        ChatWorkReport(
            task_id=task.id,
            session_id=task.session_id,
            kind="completed",
            summary="Eval finished",
            validation="Eval endpoint returned DONE; scores artifact recorded",
        ),
    )
    assert view.status == "completed"
    assert view.latest_result.report_id in manager.reports
    assert "endpoint" in view.latest_result.validation
    assert not manager.scheduled_tasks[created.id].enabled
    with pytest.raises(ValueError, match="cannot be resumed"):
        await manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="resume"))


async def test_one_shot_preserves_review_gate(manager, setup_work, monkeypatch):
    workspace, source, _, _ = setup_work

    # Existing reviewer dispatch is tested elsewhere; retain intake transition
    # and suppress only follow-on reviewer launch in this lifecycle test.
    async def no_review(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "_after_report_recorded", no_review)
    created = await manager.create_chat_work(
        source.id,
        ChatWorkCreate(
            request_key="fix", workspace_id=workspace.id, title="Fix", prompt="Fix regression"
        ),
    )
    task = manager.tasks[created.active_task_id]
    assert not task.system_internal
    view = await manager.report_chat_work(
        source.id,
        created.id,
        ChatWorkReport(
            task_id=task.id,
            session_id=task.session_id,
            kind="completed",
            summary="Patch ready",
            validation="Focused tests passed",
        ),
    )
    assert manager.tasks[task.id].status != WorkspaceTaskStatus.DONE
    assert view.status != "completed"
    assert view.latest_result.kind == "decision"
    assert manager.tasks[task.id].human_accepted_at is None


async def test_scope_rejects_wrong_tab_and_foreign_execution(manager, setup_work):
    workspace, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    with pytest.raises(KeyError):
        await manager.update_chat_work("wrong-tab", created.id, ChatWorkUpdate(action="stop"))
    with pytest.raises(ValueError, match="belong"):
        await manager.report_chat_work(
            source.id,
            created.id,
            ChatWorkReport(
                task_id="foreign-task",
                report_id="foreign-report",
                kind="completed",
                summary="wrong",
            ),
        )
    assert manager.list_chat_work("wrong-tab") == []


async def test_pause_edit_resume_and_stop_retains_evidence(manager, setup_work, monkeypatch):
    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[created.active_task_id]
    paused = await manager.update_chat_work(
        source.id, created.id, ChatWorkUpdate(action="pause", interval_seconds=120)
    )
    assert paused.status == "paused"  # in-flight execution is allowed to finish
    assert paused.active_task_id == task.id
    assert paused.next_run_at is None
    await manager.report_chat_work(
        source.id,
        created.id,
        ChatWorkReport(
            task_id=task.id, session_id=task.session_id, kind="no_change", summary="Same"
        ),
    )
    assert manager.list_chat_work(source.id)[0].status == "paused"
    resumed = await manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="resume"))
    assert resumed.next_run_at is not None
    work = manager.scheduled_tasks[created.id]
    work.last_run_at = datetime.now() - timedelta(minutes=10)
    await manager._fire_scheduled_task(work, datetime.now(), manual=True)
    active = manager._chat_work_active(work)
    assert active is not None
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    stopped = await manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="stop"))
    assert stopped.status == "stopped" and stopped.next_run_at is None
    assert manager.tasks[active.id].manual_aborted_at is not None
    assert task.id in manager.tasks
    assert manager.reports


async def test_failed_save_cannot_be_replayed_as_phantom(manager, setup_work, monkeypatch):
    _, source, payload, _ = setup_work
    save = manager._save_scheduled_tasks

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save_scheduled_tasks", fail)
    with pytest.raises(OSError):
        await manager.create_chat_work(source.id, payload())
    assert not manager.scheduled_tasks and not manager.tasks
    monkeypatch.setattr(manager, "_save_scheduled_tasks", save)
    created = await manager.create_chat_work(source.id, payload())
    assert created.active_task_id and len(manager.tasks) == 1


async def test_interrupted_prelaunch_fails_closed(manager, setup_work, monkeypatch):
    _, source, payload, _ = setup_work

    async def crash(work):
        from claude_hub.models import WorkspaceTaskCreate

        manager._create_task(
            work.workspace_id,
            WorkspaceTaskCreate(title="check", prompt="check"),
            system_internal=True,
            internal_kind="scheduled",
            source_work_id=work.id,
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "_fire_chat_work_task", crash)
    with pytest.raises(asyncio.CancelledError):
        await manager.create_chat_work(source.id, payload())
    reloaded = WorkspaceManager()
    await reloaded._reconcile_chat_work()
    assert reloaded.list_chat_work(source.id)[0].status == "failed"
    assert len(reloaded.tasks) == 1
    assert not next(iter(reloaded.scheduled_tasks.values())).enabled


async def test_api_scope_and_cli_env_contract(manager, setup_work, monkeypatch):
    from claude_hub.api import chat_work
    from claude_hub.auth.dependencies import get_current_user

    _, source, payload, _ = setup_work
    monkeypatch.setattr(chat_work, "workspace_manager", manager)
    app = FastAPI()
    app.include_router(chat_work.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tabs/{source.id}/work", json=payload().model_dump(mode="json")
        )
        assert response.status_code == 201
        work_id = response.json()["id"]
        wrong = await client.get(f"/api/tabs/foreign/work/{work_id}")
        assert wrong.status_code == 404
        assert (await client.get(f"/api/tabs/{source.id}/work")).json()[0]["id"] == work_id
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://test", transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli, ["work", "list"], env={"CLAUDE_HUB_TAB_ID": source.id})
    assert result.exit_code == 0, result.output
    assert requests[0].url.path == f"/api/tabs/{source.id}/work"
    invalid = CliRunner().invoke(
        cli,
        [
            "work",
            "create",
            "--workspace-id",
            "w",
            "--request-key",
            "k",
            "--title",
            "T",
            "--prompt",
            "P",
            "--kind",
            "monitor",
            "--interval",
            "1",
        ],
        env={"CLAUDE_HUB_TAB_ID": source.id},
    )
    assert invalid.exit_code != 0


async def test_notable_result_survives_more_than_twenty_quiet_checks(manager, setup_work):
    from claude_hub.models import AgentReport, AgentReportState, WorkspaceTaskCreate

    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    first = manager.tasks[created.active_task_id]
    await manager.report_chat_work(
        source.id,
        created.id,
        ChatWorkReport(
            task_id=first.id,
            session_id=first.session_id,
            kind="anomaly",
            summary="Score regressed",
            validation="Score artifact: 0.62",
        ),
    )
    for index in range(21):
        task = manager._create_task(
            first.workspace_id,
            WorkspaceTaskCreate(title="check", prompt="check"),
            system_internal=True,
            internal_kind="scheduled",
            source_work_id=created.id,
        )
        manager.tasks[task.id] = task.model_copy(
            update={"status": WorkspaceTaskStatus.DONE, "chat_work_outcome": "no_change"}
        )
    view = manager.chat_work_view(manager.scheduled_tasks[created.id])
    assert len(view.executions) == 20
    assert view.latest_result.summary == "Score regressed"
    assert view.latest_result.task_id == first.id


async def test_restart_recovers_objective_completion_before_next_fire(manager, setup_work):
    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[created.active_task_id]
    from claude_hub.models import AgentReportCreate, AgentReportState

    # Simulate process loss after canonical completion commit but before work's
    # separate schedule projection was updated.
    await manager.create_report(
        task.session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="Objective done",
            chat_work_outcome="completed",
        ),
    )
    assert manager.scheduled_tasks[created.id].enabled
    reloaded = WorkspaceManager()
    await reloaded._reconcile_chat_work()
    assert not reloaded.scheduled_tasks[created.id].enabled
    assert reloaded.list_chat_work(source.id)[0].status == "completed"


async def test_remote_source_and_unsupported_explicit_model_rejected(manager, setup_work):
    _, source, payload, _ = setup_work
    source.target = ExecutionTarget.REMOTE
    with pytest.raises(ValueError, match="local source"):
        await manager.create_chat_work(source.id, payload())
    source.target = ExecutionTarget.LOCAL
    with pytest.raises(ValueError, match="Claude and Codex"):
        await manager.create_chat_work(
            source.id, payload(agent_type=AgentType.CURSOR, model="unsupported")
        )
    assert not manager.tasks and not manager.scheduled_tasks


async def test_report_and_stop_serialize_without_stale_schedule_snapshot(
    manager, setup_work, monkeypatch
):
    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[created.active_task_id]
    entered, release = asyncio.Event(), asyncio.Event()
    real_create_report = manager.create_report

    async def gated(session_id, body):
        entered.set()
        await release.wait()
        return await real_create_report(session_id, body)

    monkeypatch.setattr(manager, "create_report", gated)
    report_call = asyncio.create_task(
        manager.report_chat_work(
            source.id,
            created.id,
            ChatWorkReport(
                task_id=task.id, session_id=task.session_id, kind="no_change", summary="Unchanged"
            ),
        )
    )
    await entered.wait()
    stop_call = asyncio.create_task(
        manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="stop"))
    )
    await asyncio.sleep(0)
    assert not stop_call.done()
    release.set()
    await report_call
    stopped = await stop_call
    assert stopped.status == "stopped"
    assert manager.list_chat_work(source.id)[0].status == "stopped"
    assert WorkspaceManager().list_chat_work(source.id)[0].status == "stopped"


async def test_invalid_edit_is_atomic_and_resume_active_monitor_is_safe(manager, setup_work):
    _, source, payload, _ = setup_work
    created = await manager.create_chat_work(source.id, payload())
    with pytest.raises(ValueError, match="blank"):
        await manager.update_chat_work(
            source.id, created.id, ChatWorkUpdate(interval_seconds=300, prompt=" ")
        )
    assert manager.scheduled_tasks[created.id].interval_seconds == 60
    paused = await manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="pause"))
    assert paused.status == "paused" and paused.active_task_id
    resumed = await manager.update_chat_work(source.id, created.id, ChatWorkUpdate(action="resume"))
    assert resumed.status == "running" and resumed.active_task_id == created.active_task_id
    work = manager.scheduled_tasks[created.id]
    await manager._fire_scheduled_task(work, datetime.now() + timedelta(minutes=5), manual=True)
    assert len(manager.tasks) == 1
