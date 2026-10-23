"""Lifecycle changes are recorded once, emitted once, and announced once."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.api import task_notifications
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    GoalPacket,
    ManagedSessionStatus,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.schemas import TaskPhase, TaskSourceReference
from claude_hub.services.dot.runtime.work import _lifecycle_hint
from claude_hub.services.task_lifecycle import (
    TaskNotice,
    attention_notice,
    plan_digest,
    plan_notice,
)
from claude_hub.services.workspace_manager import workspace_manager
from tests.test_task_lifecycle_board import isolated_hub  # noqa: F401  (autouse fixture)

NOW = datetime(2026, 10, 24, 9, 0, 0)


def _lifecycle_events(workspace_id: str) -> list[dict[str, Any]]:
    events = workspace_manager.task_mailbox.to_dict(workspace_id)["task_events"]
    return [event for event in events if event["type"] == "lifecycle"]


def _running_task(client: TestClient, repo: Path) -> tuple[dict, dict, dict]:
    workspace = client.post(
        "/api/workspaces", json={"name": "Notices", "path": str(repo), "session_prefix": "nt"}
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks", json={"title": "Investigate", "prompt": "work"}
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start", json={"target_session_id": worker["id"]}
    )
    return workspace, worker, task


def _fail_task(task_id: str) -> None:
    workspace_manager.tasks[task_id] = workspace_manager.tasks[task_id].model_copy(
        update={
            "status": WorkspaceTaskStatus.FAILED,
            "failed_at": datetime.now(),
            "failure_reason": "worker reported failure",
        }
    )


def _age_mark(task_id: str, seconds: int) -> None:
    task = workspace_manager.tasks[task_id]
    assert task.lifecycle_mark is not None
    mark = task.lifecycle_mark.model_copy(
        update={"changed_at": task.lifecycle_mark.changed_at - timedelta(seconds=seconds)}
    )
    workspace_manager.tasks[task_id] = task.model_copy(update={"lifecycle_mark": mark})


def test_phase_change_is_recorded_and_emitted_once(isolated_hub: Path) -> None:  # noqa: F811
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)

    assert workspace_manager._observe_task_lifecycles(workspace["id"]) == 1
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.phase == TaskPhase.RUNNING
    assert mark.notified_at is not None  # running needs no notice
    assert workspace_manager._observe_task_lifecycles(workspace["id"]) == 0

    # The worker's agent disappears: no report, no status write, yet one event.
    workspace_manager.sessions[worker["id"]] = workspace_manager.sessions[worker["id"]].model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    assert workspace_manager._observe_task_lifecycles(workspace["id"]) == 1
    events = _lifecycle_events(workspace["id"])
    assert [event["payload"]["phase"] for event in events] == ["running", "orphaned"]
    assert events[-1]["payload"]["from_phase"] == "running"
    assert events[-1]["payload"]["reason"] == "agent_gone"
    assert events[-1]["payload"]["attention"] is True
    # Agent loss is resolved by the reconciler; only its outcome is pushed.
    orphaned = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert orphaned is not None and orphaned.notified_at is not None


def test_attention_notice_waits_to_settle_then_sends_once(isolated_hub: Path) -> None:  # noqa: F811
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _fail_task(task["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    sent: list[TaskNotice] = []

    async def notifier(_task: WorkspaceTask, notice: TaskNotice) -> str:
        sent.append(notice)
        return "sent"

    workspace_manager.configure_task_notifier(notifier)
    try:
        assert asyncio.run(workspace_manager._deliver_task_notices(workspace["id"])) == 0
        _age_mark(task["id"], 60)
        assert asyncio.run(workspace_manager._deliver_task_notices(workspace["id"])) == 1
        assert asyncio.run(workspace_manager._deliver_task_notices(workspace["id"])) == 0
    finally:
        workspace_manager.configure_task_notifier(None)
    assert len(sent) == 1
    assert sent[0].kind == "attention"
    assert "執行失敗" in sent[0].text
    assert "worker reported failure" in sent[0].text
    assert task["id"] in sent[0].text


def test_failed_delivery_backs_off_and_unroutable_stops(isolated_hub: Path) -> None:  # noqa: F811
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _fail_task(task["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark(task["id"], 60)
    outcomes = ["failed", "unroutable"]
    calls: list[str] = []

    async def notifier(_task: WorkspaceTask, _notice: TaskNotice) -> str:
        calls.append(outcomes[len(calls)])
        return calls[-1]

    workspace_manager.configure_task_notifier(notifier)
    try:
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
        mark = workspace_manager.tasks[task["id"]].lifecycle_mark
        assert mark is not None
        assert mark.notify_attempts == 1 and mark.next_notify_at is not None
        assert mark.notified_at is None
        # Still backing off: not retried.
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
        assert calls == ["failed"]
        workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
            update={"lifecycle_mark": mark.model_copy(update={"next_notify_at": None})}
        )
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
    finally:
        workspace_manager.configure_task_notifier(None)
    assert calls == ["failed", "unroutable"]
    final = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert final is not None and final.notified_at is not None


def test_conditions_older_than_the_backfill_horizon_are_not_pushed(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager.sessions[worker["id"]] = workspace_manager.sessions[worker["id"]].model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    stored = workspace_manager.tasks[task["id"]]
    workspace_manager.tasks[task["id"]] = stored.model_copy(
        update={"started_at": stored.created_at - timedelta(days=3)}
    )
    # The derived "since" for agent_gone is unknown, so age the task itself.
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={"updated_at": datetime.now() - timedelta(days=3)}
    )

    workspace_manager._observe_task_lifecycles(workspace["id"])

    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.phase == TaskPhase.ORPHANED
    assert mark.notified_at is not None
    assert _lifecycle_events(workspace["id"]) == []


def test_new_plan_is_announced_once_as_a_checkpoint(isolated_hub: Path) -> None:  # noqa: F811
    client = TestClient(app)
    workspace, _worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "goal_packet": GoalPacket(
                objective="Find why requests with images fail",
                acceptance_criteria=["Reproduce with the original payload"],
                out_of_scope=["Changing the serving config"],
            )
        }
    )
    sent: list[TaskNotice] = []

    async def notifier(_task: WorkspaceTask, notice: TaskNotice) -> str:
        sent.append(notice)
        return "sent"

    workspace_manager.configure_task_notifier(notifier)
    try:
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
    finally:
        workspace_manager.configure_task_notifier(None)
    assert [notice.kind for notice in sent] == ["plan"]
    assert "Find why requests with images fail" in sent[0].text
    assert f"task followup {workspace['id']} {task['id']}" in sent[0].text


def _task(**changes: Any) -> WorkspaceTask:
    base: dict[str, Any] = dict(
        id="task-1",
        workspace_id="ws-1",
        title="Title",
        prompt="Prompt",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.REVIEW,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(changes)
    return WorkspaceTask(**base)


def test_attention_notice_names_reason_detail_and_next_step() -> None:
    notice = attention_notice(
        _task(), TaskPhase.NEEDS_USER, "reviewer_needs_input", "Which bucket may be deleted?"
    )
    assert notice.tone == "warning"
    assert "AI reviewer 有問題需要你回答" in notice.text
    assert "Which bucket may be deleted?" in notice.text
    assert "claude-hub task continue task-1" in notice.text


def test_plan_digest_tracks_objective_and_scope_only() -> None:
    packet = GoalPacket(objective="A", acceptance_criteria=["x"])
    assert plan_digest(_task()) is None
    assert plan_notice(_task()) is None
    first = plan_digest(_task(goal_packet=packet))
    assert first == plan_digest(
        _task(goal_packet=packet.model_copy(update={"validation_plan": ["y"]}))
    )
    assert first != plan_digest(_task(goal_packet=packet.model_copy(update={"objective": "B"})))


@pytest.mark.parametrize(
    ("bound", "pushed", "expected"),
    [(set(), True, "unroutable"), ({"chat-1"}, True, "sent"), ({"chat-1"}, False, "failed")],
)
def test_task_notice_routing(
    monkeypatch: MonkeyPatch, bound: set[str], pushed: bool, expected: str
) -> None:
    calls: list[tuple[str, str]] = []

    async def push(tab_id: str, text: str, *, title: str | None = None, tone: str = "info"):
        calls.append((tab_id, text))
        return pushed

    monkeypatch.setattr(task_notifications, "feishu_bound_to_tab", lambda tab: tab in bound)
    monkeypatch.setattr(task_notifications, "push_to_tab", push)
    monkeypatch.setattr(task_notifications, "_dot_tab_tracking", lambda _task_id: None)
    task = _task(source=TaskSourceReference(kind="chat", tab_id="chat-1"))
    notice = TaskNotice(kind="attention", title="T", text="token sk-abcdefghijklmnopqrstuvwx")

    assert asyncio.run(task_notifications.deliver_task_notice(task, notice)) == expected
    assert [tab for tab, _ in calls] == (["chat-1"] if bound else [])
    if calls:
        assert "sk-abcdefghijklmnopqrstuvwx" not in calls[0][1]


def test_dot_stall_hint_names_the_blocking_task() -> None:
    blocker = _task(id="blocker", title="Disk cleanup phase 4")
    queued = _task(id="queued", status=WorkspaceTaskStatus.QUEUED)
    lifecycles = {
        "queued": SimpleNamespace(blocked_by_task_id="blocker", reason="agent_held_by_other_task"),
        "blocker": SimpleNamespace(blocked_by_task_id=None, reason="reviewer_needs_input"),
    }
    manager = SimpleNamespace(
        tasks={"blocker": blocker, "queued": queued},
        task_lifecycles=lambda tasks: {task.id: lifecycles[task.id] for task in tasks},
    )
    hint = _lifecycle_hint(manager, queued)
    assert "Disk cleanup phase 4" in hint
    assert "AI reviewer has a question for you" in hint


def _stop_worker(worker_id: str) -> None:
    workspace_manager.sessions[worker_id] = workspace_manager.sessions[worker_id].model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )


def test_lifecycle_events_wake_task_wait(isolated_hub: Path) -> None:  # noqa: F811
    from claude_hub.services.task_graph import make_task_consumer_key

    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    key = make_task_consumer_key(task["id"])
    since = max(event["sequence"] for event in _lifecycle_events(workspace["id"]))

    async def wait_while_agent_dies() -> list[Any]:
        wait = asyncio.create_task(
            workspace_manager.task_mailbox.wait_for(
                workspace["id"], key, since_sequence=since, timeout_seconds=5
            )
        )
        await asyncio.sleep(0.05)
        _stop_worker(worker["id"])
        workspace_manager._observe_task_lifecycles(workspace["id"])
        return await asyncio.wait_for(wait, timeout=2)

    events = asyncio.run(wait_while_agent_dies())
    assert [event.payload["phase"] for event in events] == ["orphaned"]


def test_flapping_condition_is_not_announced_again_within_cooldown(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    sent: list[TaskNotice] = []

    async def notifier(_task: WorkspaceTask, notice: TaskNotice) -> str:
        sent.append(notice)
        return "sent"

    def set_worker(status: ManagedSessionStatus) -> None:
        workspace_manager.sessions[worker["id"]] = workspace_manager.sessions[
            worker["id"]
        ].model_copy(update={"status": status})

    workspace_manager.configure_task_notifier(notifier)
    try:
        for _ in range(3):
            set_worker(ManagedSessionStatus.NEEDS_INPUT)
            workspace_manager._observe_task_lifecycles(workspace["id"])
            _age_mark(task["id"], 200)
            asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
            set_worker(ManagedSessionStatus.WORKING)
            workspace_manager._observe_task_lifecycles(workspace["id"])
    finally:
        workspace_manager.configure_task_notifier(None)
    assert len(sent) == 1
    assert "agent 停在等待輸入或確認" in sent[0].text


def test_recent_failure_seen_first_as_failed_is_announced(isolated_hub: Path) -> None:  # noqa: F811
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Fail", "path": str(isolated_hub), "session_prefix": "fl"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks", json={"title": "Doomed", "prompt": "x"}
    ).json()
    now = datetime.now()
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.FAILED,
            "failed_at": now,
            "failure_reason": "dispatch failed",
        }
    )
    workspace_manager._observe_task_lifecycles(workspace["id"])
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.phase == TaskPhase.FAILED
    assert mark.notified_at is None


def test_self_reported_initiator_states_and_supervised_children_are_not_pushed(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, _worker, parent = _running_task(client, isolated_hub)
    child = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Child", "prompt": "sub", "parent_task_id": parent["id"]},
    ).json()
    workspace_manager.tasks[child["id"]] = workspace_manager.tasks[child["id"]].model_copy(
        update={"status": WorkspaceTaskStatus.FAILED, "failed_at": datetime.now()}
    )
    assert not workspace_manager._task_notice_wanted(workspace_manager.tasks[child["id"]], "failed")
    assert not workspace_manager._task_notice_wanted(
        workspace_manager.tasks[parent["id"]], "initiator_needs_input"
    )
    assert workspace_manager._task_notice_wanted(workspace_manager.tasks[parent["id"]], "failed")
    # Workspace agent loss is pushed only once the reconciler resolves it.
    assert not workspace_manager._task_notice_wanted(
        workspace_manager.tasks[parent["id"]], "agent_gone"
    )


def test_slow_notifier_times_out_without_blocking(
    isolated_hub: Path, monkeypatch: MonkeyPatch  # noqa: F811
) -> None:
    from claude_hub.services.workspace_manager.monitoring import lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "NOTICE_SEND_TIMEOUT_SECONDS", 0.05)
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _fail_task(task["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark(task["id"], 60)

    async def hanging(_task: WorkspaceTask, _notice: TaskNotice) -> str:
        await asyncio.sleep(10)
        return "sent"

    workspace_manager.configure_task_notifier(hanging)
    try:
        assert asyncio.run(workspace_manager._deliver_task_notices(workspace["id"])) == 0
    finally:
        workspace_manager.configure_task_notifier(None)
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.notify_attempts == 1


def test_working_task_next_step_uses_followup_not_continue() -> None:
    notice = attention_notice(
        _task(status=WorkspaceTaskStatus.WORKING), TaskPhase.NEEDS_USER, "agent_needs_input"
    )
    assert "claude-hub task followup ws-1 task-1" in notice.text
    assert "task continue" not in notice.text


def _age_mark_by(task_id: str, delta: timedelta) -> None:
    task = workspace_manager.tasks[task_id]
    assert task.lifecycle_mark is not None
    mark = task.lifecycle_mark.model_copy(
        update={"changed_at": task.lifecycle_mark.changed_at - delta}
    )
    workspace_manager.tasks[task_id] = task.model_copy(update={"lifecycle_mark": mark})


def test_reconciler_fails_a_task_whose_agent_stays_gone_and_continue_reassigns(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    _stop_worker(worker["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    # Within grace nothing is resolved, and agent loss is not pushed on its own.
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 0
    lost = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert lost is not None and lost.notified_at is not None

    _age_mark_by(task["id"], timedelta(minutes=6))
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 1
    failed = workspace_manager.tasks[task["id"]]
    assert failed.status == WorkspaceTaskStatus.FAILED
    assert "task continue" in (failed.failure_reason or "")
    workspace_manager._observe_task_lifecycles(workspace["id"])
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.phase == TaskPhase.FAILED and mark.notified_at is None

    resumed = client.post(f"/api/workspaces/tasks/{task['id']}/continue", json={})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["session_id"] not in (None, worker["id"])


def test_reconciler_waits_a_week_before_failing_an_initiator_task_without_its_chat(
    isolated_hub: Path, monkeypatch: MonkeyPatch  # noqa: F811
) -> None:
    from claude_hub.services.task_lifecycle import ChatTabFacts

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Init", "path": str(isolated_hub), "session_prefix": "in"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks", json={"title": "Chat work", "prompt": "x"}
    ).json()
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "execution_control": "initiator",
            "status": WorkspaceTaskStatus.WORKING,
            "source": TaskSourceReference(kind="chat", tab_id="gone-chat"),
        }
    )
    monkeypatch.setattr(
        workspace_manager, "_task_chat_tab_facts", lambda _task: ChatTabFacts(exists=False)
    )
    workspace_manager._observe_task_lifecycles(workspace["id"])
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.reason == "initiator_chat_gone"
    assert mark.notified_at is None  # Chat loss is announced right away

    _age_mark_by(task["id"], timedelta(days=6))
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 0
    _age_mark_by(task["id"], timedelta(days=2))
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 1
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.FAILED


def test_reconciler_leaves_subagent_tasks_to_their_own_handling(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={"task_mode": "subagent"}
    )
    _stop_worker(worker["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark_by(task["id"], timedelta(hours=1))
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 0


def _notifier_into(sent: list[TaskNotice]):
    async def notifier(_task: WorkspaceTask, notice: TaskNotice) -> str:
        sent.append(notice)
        return "sent"

    return notifier


def test_notice_in_flight_does_not_consume_a_newer_lifecycle_change(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    workspace_manager.sessions[worker["id"]] = workspace_manager.sessions[worker["id"]].model_copy(
        update={"status": ManagedSessionStatus.NEEDS_INPUT}
    )
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark(task["id"], 200)
    sent: list[TaskNotice] = []

    async def notifier(_task: WorkspaceTask, notice: TaskNotice) -> str:
        sent.append(notice)
        # While the question is being delivered, the Task fails.
        _fail_task(task["id"])
        workspace_manager._observe_task_lifecycles(workspace["id"])
        return "sent"

    workspace_manager.configure_task_notifier(notifier)
    try:
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
    finally:
        workspace_manager.configure_task_notifier(None)
    mark = workspace_manager.tasks[task["id"]].lifecycle_mark
    assert mark is not None and mark.phase == TaskPhase.FAILED
    assert mark.notified_at is None  # the failure is still to be announced
    assert len(sent) == 1


def test_a_second_real_failure_within_the_cooldown_is_announced(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, _worker, task = _running_task(client, isolated_hub)
    workspace_manager._observe_task_lifecycles(workspace["id"])
    sent: list[TaskNotice] = []
    workspace_manager.configure_task_notifier(_notifier_into(sent))
    try:
        for _ in range(2):
            _fail_task(task["id"])
            workspace_manager._observe_task_lifecycles(workspace["id"])
            _age_mark(task["id"], 60)
            asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
            # Continued after the failure.
            workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
                update={"status": WorkspaceTaskStatus.WORKING, "failed_at": None}
            )
            workspace_manager._observe_task_lifecycles(workspace["id"])
    finally:
        workspace_manager.configure_task_notifier(None)
    assert len(sent) == 2


def test_child_notice_waits_only_while_its_parent_is_working(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, _worker, parent = _running_task(client, isolated_hub)
    child = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Child", "prompt": "sub", "parent_task_id": parent["id"]},
    ).json()
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _fail_task(child["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark(child["id"], 60)
    sent: list[TaskNotice] = []
    workspace_manager.configure_task_notifier(_notifier_into(sent))
    try:
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
        assert sent == []
        workspace_manager.tasks[parent["id"]] = workspace_manager.tasks[parent["id"]].model_copy(
            update={"status": WorkspaceTaskStatus.REVIEW}
        )
        asyncio.run(workspace_manager._deliver_task_notices(workspace["id"]))
    finally:
        workspace_manager.configure_task_notifier(None)
    assert [notice.kind for notice in sent] == ["attention"]


def test_reconciler_skips_feedback_reaper_and_simultaneous_loss_of_every_agent(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace, worker, task = _running_task(client, isolated_hub)
    other = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    _stop_worker(worker["id"])
    _stop_worker(other["id"])
    workspace_manager._observe_task_lifecycles(workspace["id"])
    _age_mark_by(task["id"], timedelta(minutes=10))
    # Every agent vanished at once: treated as an outage, not as agent loss.
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 0

    workspace_manager.sessions[other["id"]] = workspace_manager.sessions[other["id"]].model_copy(
        update={"status": ManagedSessionStatus.IDLE}
    )
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={"system_internal": True, "internal_kind": "feedback_reaper"}
    )
    assert workspace_manager._reconcile_orphaned_tasks(workspace["id"]) == 0


def test_legacy_dispatch_pending_task_is_stranded_for_a_dedicated_agent(
    isolated_hub: Path,  # noqa: F811
) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Pending", "path": str(isolated_hub), "session_prefix": "pd"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks", json={"title": "Old", "prompt": "x"}
    ).json()
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.QUEUED,
            "dispatch_pending": True,
            "session_id": None,
            "queued_at": datetime.now(),
        }
    )
    assert [item.id for item in workspace_manager._stranded_queued_tasks(workspace["id"])] == [
        task["id"]
    ]


def test_stopping_the_monitor_cancels_background_jobs() -> None:
    async def scenario() -> bool:
        job = asyncio.create_task(asyncio.sleep(30))
        workspace_manager._task_notice_jobs["ws-x"] = job
        await workspace_manager._stop_monitor_jobs()
        return job.cancelled() and not workspace_manager._task_notice_jobs

    assert asyncio.run(scenario())
