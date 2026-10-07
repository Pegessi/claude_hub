"""Read-only legacy ChatWork compatibility and explicit control tests."""

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
from claude_hub.models import (
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    ScheduledTask,
    ScheduledTaskKind,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.models.schemas import (
    ChatWorkCreate,
    ChatWorkReport,
    ChatWorkUpdate,
    TaskExecutionControl,
)
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_scheduled_tasks import (
    AsyncNoop,
    _install_live_terminal_fakes,
    _make_session,
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


def _install_legacy_work(
    manager: WorkspaceManager, setup_work, *, kind: str = "monitor"
) -> tuple[ScheduledTask, WorkspaceTask, ManagedSession]:
    """Persist one pre-retirement graph without calling the retired launcher."""
    workspace, source, _, _ = setup_work
    now = datetime.now()
    work = ScheduledTask(
        id=f"legacy-{kind}",
        name="Legacy work",
        kind=ScheduledTaskKind.HUB_TASK,
        enabled=kind == "monitor",
        run_at=now if kind == "task" else None,
        interval_seconds=60 if kind == "monitor" else None,
        workspace_id=workspace.id,
        agent_type=AgentType.CODEX,
        message="Observe eval and report changes",
        task_title="Watch eval",
        source_tab_id=source.id,
        source_request_key="legacy-request",
        source_request_fingerprint="legacy-fingerprint",
        work_kind=kind,
        work_task_mode=WorkspaceTaskMode.REVIEWED,
        work_cwd=workspace.path,
        work_inherit_source_env=True,
        next_run_at=now - timedelta(seconds=1) if kind == "monitor" else None,
        last_run_at=now - timedelta(minutes=1),
        last_status="running",
        run_count=1,
        created_at=now,
        updated_at=now,
    )
    manager.scheduled_tasks[work.id] = work
    task = manager._create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title=work.task_title or "Legacy task",
            prompt=work.message or "Legacy prompt",
            agent_type=work.agent_type,
            task_mode=work.work_task_mode,
        ),
        system_internal=kind == "monitor",
        internal_kind="scheduled" if kind == "monitor" else None,
        source_work_id=work.id,
    )
    session = _make_session(
        workspace, session_id=f"legacy-session-{kind}", caller_owned_ephemeral=True
    ).model_copy(
        update={
            "agent_type": task.agent_type,
            "ephemeral": True,
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
            "task_id": task.id,
            "current_task_id": task.id,
            "env": dict(source.env),
        }
    )
    manager.sessions[session.id] = session
    task = task.model_copy(
        update={
            "execution_control": TaskExecutionControl.WORKSPACE,
            "legacy_work_detached": False,
            "status": WorkspaceTaskStatus.WORKING,
            "session_id": session.id,
            "started_at": now,
            "chat_work_owned_session_id": session.id,
            "chat_work_owned_tab_id": session.tab_id,
            "updated_at": now,
        }
    )
    manager.tasks[task.id] = task
    manager._save_state()
    manager._save_scheduled_tasks()
    return work, task, session


async def test_chat_work_create_is_retired_without_side_effects(manager, setup_work):
    _, source, payload, _ = setup_work
    with pytest.raises(ValueError, match="legacy_chat_work_read_only_use_workspace_tasks"):
        await manager.create_chat_work(source.id, payload())
    assert manager.scheduled_tasks == {}
    assert manager.tasks == {}
    assert manager.sessions == {}


async def test_legacy_work_never_fires_or_reconciles(manager, setup_work, monkeypatch):
    work, task, session = _install_legacy_work(manager, setup_work)
    before_work, before_task = work.model_dump(mode="json"), task.model_dump(mode="json")

    async def forbidden(*args, **kwargs):
        pytest.fail("retired ChatWork must not dispatch, ensure, or clean up")

    monkeypatch.setattr(manager, "ensure_workspace_agent", forbidden)
    monkeypatch.setattr(manager, "_dispatch_task_to_session", forbidden)
    monkeypatch.setattr(manager, "_best_effort_delete_session", forbidden)
    await manager._reconcile_chat_work()
    await manager._tick_scheduled_tasks()
    assert manager.scheduled_tasks[work.id].model_dump(mode="json") == before_work
    assert manager.tasks[task.id].model_dump(mode="json") == before_task
    assert manager.sessions[session.id].current_task_id == task.id
    with pytest.raises(ValueError, match="legacy_chat_work_read_only_use_workspace_tasks"):
        await manager._fire_scheduled_task(work, datetime.now(), manual=True)


async def test_report_completes_quiet_check_and_retry_is_idempotent(manager, setup_work):
    work, task, session = _install_legacy_work(manager, setup_work)
    body = ChatWorkReport(
        task_id=task.id,
        session_id=session.id,
        kind="no_change",
        summary="Still running",
        validation="Job status remains RUNNING",
    )
    view = await manager.report_chat_work(work.source_tab_id, work.id, body)
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
    assert session.id not in manager.sessions
    assert view.latest_result is None
    assert view.active_task_id is None
    assert view.executions[0].outcome == "no_change"
    again = await manager.report_chat_work(work.source_tab_id, work.id, body)
    assert again.executions == view.executions
    assert len([r for r in manager.reports.values() if r.chat_work_outcome == "no_change"]) == 1
    assert manager.scheduled_tasks[work.id].enabled


async def test_objective_completion_retains_legacy_report_evidence(manager, setup_work):
    work, task, session = _install_legacy_work(manager, setup_work)
    view = await manager.report_chat_work(
        work.source_tab_id,
        work.id,
        ChatWorkReport(
            task_id=task.id,
            session_id=session.id,
            kind="completed",
            summary="Eval finished",
            validation="Eval endpoint returned DONE; scores artifact recorded",
        ),
    )
    assert view.status == "completed"
    assert view.latest_result.report_id in manager.reports
    assert "endpoint" in view.latest_result.validation
    assert not manager.scheduled_tasks[work.id].enabled


async def test_legacy_one_shot_keeps_review_gate(manager, setup_work, monkeypatch):
    work, task, session = _install_legacy_work(manager, setup_work, kind="task")

    async def no_review(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "_after_report_recorded", no_review)
    view = await manager.report_chat_work(
        work.source_tab_id,
        work.id,
        ChatWorkReport(
            task_id=task.id,
            session_id=session.id,
            kind="completed",
            summary="Patch ready",
            validation="Focused tests passed",
        ),
    )
    assert not manager.tasks[task.id].system_internal
    assert manager.tasks[task.id].status != WorkspaceTaskStatus.DONE
    assert view.status != "completed"
    assert view.latest_result.kind == "decision"
    assert manager.tasks[task.id].human_accepted_at is None


async def test_scope_rejects_wrong_tab_and_foreign_execution(manager, setup_work):
    work, task, _ = _install_legacy_work(manager, setup_work)
    with pytest.raises(KeyError):
        await manager.update_chat_work("wrong-tab", work.id, ChatWorkUpdate(action="stop"))
    with pytest.raises(ValueError, match="belong"):
        await manager.report_chat_work(
            work.source_tab_id,
            work.id,
            ChatWorkReport(
                task_id="foreign-task",
                report_id="foreign-report",
                kind="completed",
                summary="wrong",
            ),
        )
    assert manager.list_chat_work("wrong-tab") == []
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING


async def test_legacy_pause_and_stop_remain_but_edit_and_resume_are_retired(
    manager, setup_work, monkeypatch
):
    work, task, _ = _install_legacy_work(manager, setup_work)
    paused = await manager.update_chat_work(
        work.source_tab_id, work.id, ChatWorkUpdate(action="pause")
    )
    assert paused.status == "paused"
    assert paused.active_task_id == task.id
    assert paused.next_run_at is None
    with pytest.raises(ValueError, match="legacy_chat_work_read_only_use_workspace_tasks"):
        await manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="resume"))
    with pytest.raises(ValueError, match="legacy_chat_work_read_only_use_workspace_tasks"):
        await manager.update_chat_work(
            work.source_tab_id, work.id, ChatWorkUpdate(prompt="new instructions")
        )
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    stopped = await manager.update_chat_work(
        work.source_tab_id, work.id, ChatWorkUpdate(action="stop")
    )
    assert stopped.status == "stopped"
    assert manager.tasks[task.id].manual_aborted_at is not None


async def test_chat_work_api_create_is_410_but_legacy_read_and_stop_remain(
    manager, setup_work, monkeypatch
):
    from claude_hub.api import chat_work
    from claude_hub.auth.dependencies import get_current_user

    work, task, _ = _install_legacy_work(manager, setup_work)
    _, source, payload, _ = setup_work
    monkeypatch.setattr(chat_work, "workspace_manager", manager)
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    api = FastAPI()
    api.include_router(chat_work.router)
    api.dependency_overrides[get_current_user] = lambda: object()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        before = (len(manager.scheduled_tasks), len(manager.tasks), len(manager.sessions))
        create = await client.post(
            f"/api/tabs/{source.id}/work", json=payload().model_dump(mode="json")
        )
        after = (len(manager.scheduled_tasks), len(manager.tasks), len(manager.sessions))
        listed = await client.get(f"/api/tabs/{source.id}/work")
        wrong = await client.get(f"/api/tabs/foreign/work/{work.id}")
        stopped = await client.patch(
            f"/api/tabs/{source.id}/work/{work.id}", json={"action": "stop"}
        )
    assert create.status_code == 410
    assert create.json() == {"detail": "legacy_chat_work_read_only_use_workspace_tasks"}
    assert after == before
    assert listed.status_code == 200 and listed.json()[0]["id"] == work.id
    assert wrong.status_code == 404
    assert stopped.status_code == 200
    assert manager.tasks[task.id].manual_aborted_at is not None


async def test_legacy_cli_lists_history_and_create_reports_410(manager, setup_work, monkeypatch):
    work, _, _ = _install_legacy_work(manager, setup_work)
    _, source, _, _ = setup_work
    requests: list[httpx.Request] = []
    view = manager.chat_work_view(work).model_dump(mode="json")

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(200, json=[view])
            if request.method == "GET"
            else httpx.Response(
                410, json={"detail": "legacy_chat_work_read_only_use_workspace_tasks"}
            )
        )

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://test", transport=httpx.MockTransport(handler)),
    )
    listed = CliRunner().invoke(cli, ["work", "list"], env={"CLAUDE_HUB_TAB_ID": source.id})
    create = CliRunner().invoke(
        cli,
        [
            "work",
            "create",
            "--workspace-id",
            "ws-1",
            "--request-key",
            "legacy-create",
            "--title",
            "T",
            "--prompt",
            "P",
            "--kind",
            "monitor",
            "--interval",
            "60",
        ],
        env={"CLAUDE_HUB_TAB_ID": source.id},
    )
    assert listed.exit_code == 0, listed.output
    assert work.id in listed.output
    assert create.exit_code != 0
    assert "legacy_chat_work_read_only_use_workspace_tasks" in create.output
    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", f"/api/tabs/{source.id}/work"),
        ("POST", f"/api/tabs/{source.id}/work"),
    ]


async def test_notable_result_survives_more_than_twenty_quiet_checks(manager, setup_work):
    work, first, session = _install_legacy_work(manager, setup_work)
    await manager.report_chat_work(
        work.source_tab_id,
        work.id,
        ChatWorkReport(
            task_id=first.id,
            session_id=session.id,
            kind="anomaly",
            summary="Score regressed",
            validation="Score artifact: 0.62",
        ),
    )
    for index in range(21):
        task = manager._create_task(
            first.workspace_id,
            WorkspaceTaskCreate(title=f"check-{index}", prompt="check"),
            system_internal=False,
            source_work_id=work.id,
        )
        manager.tasks[task.id] = task.model_copy(
            update={"status": WorkspaceTaskStatus.DONE, "chat_work_outcome": "no_change"}
        )
    manager._save_state()
    view = manager.chat_work_view(work)
    assert len(view.executions) == 20
    assert view.latest_result.summary == "Score regressed"
    assert view.latest_result.task_id == first.id


async def test_report_and_stop_serialize_without_stale_schedule_snapshot(
    manager, setup_work, monkeypatch
):
    work, task, session = _install_legacy_work(manager, setup_work)
    entered, release = asyncio.Event(), asyncio.Event()
    real_create_report = manager.create_report

    async def gated(session_id, body):
        entered.set()
        await release.wait()
        return await real_create_report(session_id, body)

    monkeypatch.setattr(manager, "create_report", gated)
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    report_call = asyncio.create_task(
        manager.report_chat_work(
            work.source_tab_id,
            work.id,
            ChatWorkReport(
                task_id=task.id, session_id=session.id, kind="no_change", summary="Unchanged"
            ),
        )
    )
    await entered.wait()
    stop_call = asyncio.create_task(
        manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    )
    await asyncio.sleep(0)
    assert not stop_call.done()
    release.set()
    await report_call
    stopped = await stop_call
    assert stopped.status == "stopped"
    assert manager.list_chat_work(work.source_tab_id)[0].status == "stopped"
    assert WorkspaceManager().list_chat_work(work.source_tab_id)[0].status == "stopped"


async def test_report_outcome_fingerprint_and_retry_survive_cleanup(manager, setup_work):
    work, task, session = _install_legacy_work(manager, setup_work)
    body = AgentReportCreate(state=AgentReportState.COMPLETED, message="Checked")
    no_change, complete = body.model_copy(
        update={"chat_work_outcome": "no_change"}
    ), body.model_copy(update={"chat_work_outcome": "completed"})
    assert manager._compute_report_fingerprint(body) != manager._compute_report_fingerprint(
        no_change
    )
    assert manager._compute_report_fingerprint(no_change) != manager._compute_report_fingerprint(
        complete
    )
    report = ChatWorkReport(
        task_id=task.id,
        session_id=session.id,
        kind="no_change",
        summary="Checked",
        call_id="  stable-check  ",
    )
    await manager.report_chat_work(work.source_tab_id, work.id, report)
    assert session.id not in manager.sessions
    again = await manager.report_chat_work(work.source_tab_id, work.id, report)
    assert again.latest_result is None
    persisted = next(item for item in manager.reports.values() if item.chat_work_outcome)
    with pytest.raises(ValueError, match="different outcome"):
        await manager.report_chat_work(
            work.source_tab_id,
            work.id,
            ChatWorkReport(
                task_id=task.id, report_id=persisted.id, kind="completed", summary="Checked"
            ),
        )


@pytest.mark.parametrize("kind", ["task", "monitor"])
async def test_legacy_progress_keeps_task_working_but_finishes_monitor_check(
    manager, setup_work, kind
):
    work, task, session = _install_legacy_work(manager, setup_work, kind=kind)
    view = await manager.report_chat_work(
        work.source_tab_id,
        work.id,
        ChatWorkReport(
            task_id=task.id,
            session_id=session.id,
            kind="progress",
            summary="First phase finished; more remains",
        ),
    )
    assert view.latest_result.kind == "progress"
    if kind == "task":
        assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
        assert session.id in manager.sessions
        assert view.active_task_id == task.id
    else:
        assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
        assert session.id not in manager.sessions
        assert view.active_task_id is None
        assert manager.scheduled_tasks[work.id].enabled
        assert not manager.scheduled_tasks[work.id].work_completed_at


async def test_failed_stop_cleanup_is_retried_only_by_explicit_stop(
    manager, setup_work, monkeypatch
):
    work, task, session = _install_legacy_work(manager, setup_work)

    async def fail_delete(_session_id):
        raise OSError("temporary delete failure")

    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    monkeypatch.setattr(manager, "delete_session", fail_delete)
    await manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    stopped = manager.tasks[task.id]
    assert stopped.manual_aborted_at and stopped.session_id is None
    assert stopped.chat_work_owned_session_id == session.id
    assert stopped.chat_work_owned_tab_id == session.tab_id
    assert session.id in manager.sessions
    reloaded = WorkspaceManager()
    await reloaded._reconcile_chat_work()
    assert session.id in reloaded.sessions
    await reloaded.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    assert session.id not in reloaded.sessions
    assert reloaded.list_chat_work(work.source_tab_id)[0].status == "stopped"


@pytest.mark.parametrize(
    "change", ["reincarnated", "reassigned", "referenced", "shared", "persistent", "dispatcher"]
)
async def test_stop_cleanup_does_not_delete_reassigned_or_shared_sessions(
    manager, setup_work, monkeypatch, change
):
    work, task, session = _install_legacy_work(manager, setup_work)
    delete_session = manager.delete_session

    async def fail_delete(_session_id):
        raise OSError("temporary delete failure")

    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    monkeypatch.setattr(manager, "delete_session", fail_delete)
    await manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    monkeypatch.setattr(manager, "delete_session", delete_session)
    if change == "dispatcher":
        manager.workspaces[task.workspace_id].dispatcher_session_id = session.id
    elif change == "referenced":
        manager.create_task(
            task.workspace_id,
            WorkspaceTaskCreate(title="Other", prompt="Other task", session_id=session.id),
        )
    else:
        updates = {
            "reincarnated": {"tab_id": "different-session-incarnation"},
            "reassigned": {"current_task_id": "another-task"},
            "shared": {"caller_owned_ephemeral": False},
            "persistent": {"ephemeral": False},
        }
        manager.sessions[session.id] = manager.sessions[session.id].model_copy(
            update=updates[change]
        )
    await manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    await manager._reconcile_chat_work()
    assert session.id in manager.sessions


@pytest.mark.parametrize("detached", [True, False])
async def test_legacy_stop_never_controls_detached_or_initiator_task(
    manager, setup_work, monkeypatch, detached
):
    work, task, session = _install_legacy_work(manager, setup_work)
    updates = (
        {"legacy_work_detached": True}
        if detached
        else {"execution_control": TaskExecutionControl.INITIATOR}
    )
    manager.tasks[task.id] = task.model_copy(update=updates)
    manager._save_state()
    interrupted: list[str] = []

    async def record_interrupt(session_id, *args, **kwargs):
        interrupted.append(session_id)

    monkeypatch.setattr(manager, "_interrupt_session", record_interrupt)
    stopped = await manager.update_chat_work(
        work.source_tab_id, work.id, ChatWorkUpdate(action="stop")
    )
    assert stopped.status == "stopped"
    assert interrupted == []
    assert manager.tasks[task.id].manual_aborted_at is None
    assert session.id in manager.sessions


async def test_stop_intent_is_not_replayed_by_restart_reconcile(manager, setup_work, monkeypatch):
    work, task, session = _install_legacy_work(manager, setup_work)

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "abort_task", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await manager.update_chat_work(work.source_tab_id, work.id, ChatWorkUpdate(action="stop"))
    reloaded = WorkspaceManager()

    async def forbidden(*args, **kwargs):
        pytest.fail("restart reconciliation must not replay legacy control")

    monkeypatch.setattr(reloaded, "abort_task", forbidden)
    monkeypatch.setattr(reloaded, "_best_effort_delete_session", forbidden)
    await reloaded._reconcile_chat_work()
    assert reloaded.tasks[task.id].manual_aborted_at is None
    assert session.id in reloaded.sessions
    assert reloaded.list_chat_work(work.source_tab_id)[0].status == "stopped"


async def test_nonterminal_generic_completion_claim_does_not_end_monitor(manager, setup_work):
    work, task, session = _install_legacy_work(manager, setup_work)
    await manager.create_report(
        session.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="Still running",
            chat_work_outcome="completed",
        ),
    )
    await manager._reconcile_chat_work()
    view = manager.list_chat_work(work.source_tab_id)[0]
    assert manager.scheduled_tasks[work.id].enabled
    assert view.status == "running" and view.active_task_id == task.id
    assert view.latest_result is None


@pytest.mark.parametrize("action", ["pause", "stop"])
@pytest.mark.parametrize("edit", [{"prompt": "replacement"}, {"interval_seconds": 120}])
async def test_legacy_control_cannot_smuggle_edits(manager, setup_work, monkeypatch, action, edit):
    work, task, session = _install_legacy_work(manager, setup_work)
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    before_work = work.model_dump(mode="json")
    before_task = task.model_dump(mode="json")
    with pytest.raises(ValueError, match="legacy_chat_work_read_only_use_workspace_tasks"):
        await manager.update_chat_work(
            work.source_tab_id, work.id, ChatWorkUpdate(action=action, **edit)
        )
    assert manager.scheduled_tasks[work.id].model_dump(mode="json") == before_work
    assert manager.tasks[task.id].model_dump(mode="json") == before_task
    assert manager.sessions[session.id] is session
