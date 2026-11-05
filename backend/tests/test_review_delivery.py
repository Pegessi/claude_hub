"""Lost reviewer verdicts recover through the sole Task liveness owner."""

import asyncio
from datetime import datetime, timedelta
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from claude_hub.main import app
from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import WorkspaceManager

wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def delivery_env(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(wm, "STATE_ROOT", root)
    monkeypatch.setattr(wm, "INDEX_FILE", root / "index.json")
    monkeypatch.setattr(wm._persistence, "INDEX_FILE", root / "index.json")
    monkeypatch.setattr(wm._state, "INDEX_FILE", root / "index.json")
    clock = [datetime(2026, 10, 21, 16)]
    monkeypatch.setattr(WorkspaceManager, "_runtime_now", lambda self: clock[0])
    manager = WorkspaceManager()
    old = clock[0] - timedelta(seconds=181)
    manager.workspaces["w"] = Workspace(
        id="w",
        name="test",
        path=str(tmp_path),
        default_branch="main",
        session_prefix="test",
        created_at=old,
        updated_at=old,
    )
    manager.tasks["t"] = WorkspaceTask(
        id="t",
        workspace_id="w",
        title="review",
        prompt="test",
        agent_type=AgentType.CODEX,
        status=WorkspaceTaskStatus.REVIEW,
        session_id="worker",
        review_session_id="reviewer",
        review_attempts=1,
        review_requested_at=old,
        created_at=old,
        updated_at=old,
    )
    manager.sessions["reviewer"] = ManagedSession(
        id="reviewer",
        workspace_id="w",
        tab_id="tab",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        task_id="t",
        current_task_id="t",
        title="reviewer",
        workspace_path=str(tmp_path),
        tmux_session="claude-hub-tab",
        created_at=old,
        updated_at=old,
        last_activity_at=old,
    )
    sent = []

    async def fake_send(tmux_session, message, call_id):
        sent.append((message, call_id))

    async def ready(session):
        pass

    monkeypatch.setattr(
        WorkspaceManager, "_send_tmux_message_with_receipt", lambda self, *args: fake_send(*args)
    )
    monkeypatch.setattr(
        WorkspaceManager, "_ensure_session_ready_for_send", lambda self, *args: ready(*args)
    )

    async def capture(self, *_args):
        return "> "

    monkeypatch.setattr(WorkspaceManager, "_capture_tmux_output", capture)
    monkeypatch.setattr(wm.settings, "review_verdict_idle_timeout_seconds", 180)
    manager._save_state()
    return manager, clock, sent


async def tick(manager):
    return await manager._check_task_liveness("t")


def test_two_nudges_then_escalation_across_restart(delivery_env):
    manager, clock, sent = delivery_env
    original = manager.tasks["t"]
    asyncio.run(tick(manager))
    asyncio.run(tick(manager))
    assert len(sent) == 1
    message, call_id = sent[0]
    assert "/api/workspaces/sessions/reviewer/reports" in message
    assert '"state":"review_passed"' in message
    assert "acked_call_ids" in message and call_id in message
    assert manager.tasks["t"].review_cycle == original.review_cycle
    assert manager.tasks["t"].review_attempts == original.review_attempts
    assert manager.tasks["t"].review_requested_at == original.review_requested_at
    reloaded = WorkspaceManager()
    asyncio.run(tick(reloaded))
    assert len(sent) == 1
    # A clean-idle sample after the reminder, with no report or terminal work.
    clock[0] += timedelta(seconds=181)
    s = reloaded.sessions["reviewer"]
    reloaded.sessions[s.id] = s.model_copy(
        update={"status": ManagedSessionStatus.IDLE, "runtime_status": AgentRuntimeStatus.IDLE}
    )
    asyncio.run(tick(reloaded))
    assert len(sent) == 2
    clock[0] += timedelta(seconds=181)
    session = reloaded.sessions["reviewer"]
    reloaded.sessions[session.id] = session.model_copy(
        update={"status": ManagedSessionStatus.IDLE, "runtime_status": AgentRuntimeStatus.IDLE}
    )
    asyncio.run(tick(reloaded))
    events = reloaded.task_mailbox._events["w"]
    alerts = [e for e in events if e.action == "liveness:attention"]
    assert len(alerts) == 1 and alerts[0].type.value == "needs_input"
    assert alerts[0].review_cycle == original.review_cycle
    assert alerts[0].payload["attempts"] == 2
    assert reloaded.tasks["t"].review_completed_at is None
    assert reloaded.tasks["t"].reviewed_cycle == 0
    again = WorkspaceManager()
    asyncio.run(tick(again))
    assert len(sent) == 2
    assert (
        len([e for e in again.task_mailbox._events["w"] if e.action == "liveness:attention"]) == 1
    )


@pytest.mark.parametrize(
    "case",
    [
        "busy",
        "fresh",
        "recent_activity",
        "recent_report",
        "verdict",
        "sealed",
        "closed",
        "cross_bound",
        "failed",
    ],
)
def test_no_false_nudge(delivery_env, case):
    manager, clock, sent = delivery_env
    t, s = manager.tasks["t"], manager.sessions["reviewer"]
    if case == "busy":
        manager.sessions[s.id] = s.model_copy(update={"runtime_status": AgentRuntimeStatus.WORKING})
    elif case == "fresh":
        manager.tasks[t.id] = t.model_copy(update={"review_requested_at": clock[0]})
    elif case == "recent_activity":
        manager.sessions[s.id] = s.model_copy(update={"last_activity_at": clock[0]})
    elif case in {"recent_report", "verdict"}:
        manager.reports["r"] = AgentReport(
            id="r",
            workspace_id="w",
            task_id="t",
            session_id=s.id,
            state=(
                AgentReportState.REVIEW_STARTED
                if case == "recent_report"
                else AgentReportState.REVIEW_PASSED
            ),
            message="evidence",
            review_cycle=1,
            created_at=clock[0],
        )
    elif case == "sealed":
        manager.tasks[t.id] = t.model_copy(update={"reviewed_cycle": 1})
    elif case == "closed":
        manager.tasks[t.id] = t.model_copy(update={"review_completed_at": clock[0]})
    elif case == "cross_bound":
        manager.sessions[s.id] = s.model_copy(update={"current_task_id": "other"})
    elif case == "failed":
        manager.tasks[t.id] = t.model_copy(update={"status": WorkspaceTaskStatus.FAILED})
    asyncio.run(tick(manager))
    assert not sent
    assert not manager.tasks[t.id].liveness or manager.tasks[t.id].liveness.action != "nudge"


@pytest.mark.parametrize("restart_delay", [0, 500])
def test_intent_saved_before_enqueue_crash_resumes_same_message(
    delivery_env, monkeypatch, restart_delay
):
    manager, clock, sent = delivery_env

    async def crash(*args, **kwargs):
        raise RuntimeError("pre-enqueue crash")

    monkeypatch.setattr(manager, "send_session_message", crash)
    asyncio.run(tick(manager))
    saved = manager.tasks["t"].liveness.message
    assert saved and not sent
    clock[0] += timedelta(seconds=restart_delay)
    reloaded = WorkspaceManager()
    # A changed port cannot change the fingerprint of an already-intended message.
    monkeypatch.setattr(wm.settings, "port", 9999)
    asyncio.run(tick(reloaded))
    if restart_delay < 180:
        assert len(sent) == 1 and sent[0][0].split("\n", 1)[1] == saved, (
            reloaded.tasks["t"].liveness,
            reloaded.sessions["reviewer"],
        )
    else:
        assert not sent
        assert reloaded.tasks["t"].liveness.escalated_at is not None


def test_wrong_paths_stay_404_without_state_effects(delivery_env, monkeypatch):
    manager, _, _ = delivery_env
    # Route closures use the application singleton, so assert its state too.
    for name in ("tasks", "sessions", "reports", "workspaces", "task_mailbox"):
        monkeypatch.setattr(wm.workspace_manager, name, getattr(manager, name))
    before = {k: v.model_dump() for k, v in manager.tasks.items()}
    client = TestClient(app, raise_server_exceptions=False)
    for path in [
        "/api/sessions/reviewer/preview-report",
        "/api/sessions/reviewer/reports",
        "/api/workspaces/sessions/reviewer/preview-report",
    ]:
        response = client.post(
            path, json={"task_id": "t", "state": "review_passed", "call_id": "wrong-path"}
        )
        # No API route must consume these requests. A checkout with
        # frontend/dist mounted at "/" answers an unmatched POST 405; a
        # dist-less checkout (e.g. isolated verify env) returns 404. Both are
        # safe rejections — the state-equality assertions below are the real
        # guarantee.
        assert response.status_code in (404, 405)
    assert {k: v.model_dump() for k, v in manager.tasks.items()} == before
    assert not manager.reports
    assert not manager.task_mailbox._events


def test_busy_or_reporting_after_nudge_postpones_escalation(delivery_env):
    manager, clock, sent = delivery_env
    asyncio.run(tick(manager))
    clock[0] += timedelta(seconds=181)
    session = manager.sessions["reviewer"]
    manager.sessions[session.id] = session.model_copy(
        update={
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )
    asyncio.run(tick(manager))
    assert manager.tasks["t"].liveness.escalated_at is None
    manager.sessions[session.id] = session.model_copy(
        update={"status": ManagedSessionStatus.IDLE, "runtime_status": AgentRuntimeStatus.IDLE}
    )
    manager.reports["r"] = AgentReport(
        id="r",
        workspace_id="w",
        task_id="t",
        session_id=session.id,
        state=AgentReportState.REVIEW_STARTED,
        message="still reviewing",
        review_cycle=1,
        created_at=clock[0],
    )
    asyncio.run(tick(manager))
    assert manager.tasks["t"].liveness.escalated_at is None
    assert len(sent) == 1


def test_later_dispatch_gets_one_distinct_reminder(delivery_env):
    manager, clock, sent = delivery_env
    asyncio.run(tick(manager))
    task = manager.tasks["t"]
    clock[0] += timedelta(seconds=500)
    old = clock[0] - timedelta(seconds=181)
    manager.tasks[task.id] = task.model_copy(
        update={"review_cycle": 2, "review_attempts": 2, "review_requested_at": old}
    )
    session = manager.sessions["reviewer"]
    manager.sessions[session.id] = session.model_copy(
        update={
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "last_activity_at": old,
        }
    )
    asyncio.run(tick(manager))
    asyncio.run(tick(manager))
    assert len(sent) == 2 and sent[0][1] != sent[1][1]


def test_ambiguous_send_is_not_replayed_after_restart(delivery_env, monkeypatch):
    manager, _, sent = delivery_env

    async def uncertain(self, *args):
        sent.append(args)
        raise RuntimeError("ambiguous transport failure")

    monkeypatch.setattr(WorkspaceManager, "_send_tmux_message_with_receipt", uncertain)
    asyncio.run(tick(manager))
    assert len(sent) == 1
    assert manager.sessions["reviewer"].uncertain_call_ids
    reloaded = WorkspaceManager()
    asyncio.run(tick(reloaded))
    assert len(sent) == 1


def test_escalation_save_crash_retries_same_incident(delivery_env, monkeypatch):
    manager, clock, sent = delivery_env
    for _ in range(2):
        asyncio.run(tick(manager))
        clock[0] += timedelta(seconds=181)
        session = manager.sessions["reviewer"]
        manager.sessions[session.id] = session.model_copy(
            update={"status": ManagedSessionStatus.IDLE, "runtime_status": AgentRuntimeStatus.IDLE}
        )
    manager._save_workspace_state("w")

    def crash(*args):
        raise RuntimeError("before atomic state save")

    monkeypatch.setattr(manager, "_save_workspace_state", crash)
    with pytest.raises(RuntimeError, match="atomic state save"):
        asyncio.run(tick(manager))
    reloaded = WorkspaceManager()
    asyncio.run(tick(reloaded))
    asyncio.run(tick(reloaded))
    assert len(sent) == 2
    alerts = [e for e in reloaded.task_mailbox._events["w"] if e.action == "liveness:attention"]
    assert len(alerts) == 1
    assert reloaded.tasks["t"].liveness.escalated_at == clock[0]
