"""Regression coverage for linked-work recovery and concurrent controls."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    ManagedSessionStatus,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.models.schemas import ChatWorkCreate, ChatWorkReport, ChatWorkUpdate
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_chat_work import manager, setup_work, state_root
from tests.test_scheduled_tasks import AsyncNoop, _wm


async def _one_shot(manager, setup_work, mode=WorkspaceTaskMode.REVIEWED):
    workspace, source, _, _ = setup_work
    work = await manager.create_chat_work(
        source.id,
        ChatWorkCreate(
            request_key="one-shot",
            workspace_id=workspace.id,
            title="Recover a linked task",
            prompt="Perform one bounded change",
            task_mode=mode,
        ),
    )
    return source, work, manager.tasks[work.active_task_id]


def _lose_worker(manager, task, state):
    worker = manager.sessions[task.session_id]
    if state == "missing":
        manager.sessions.pop(worker.id)
    else:
        manager.sessions[worker.id] = worker.model_copy(
            update={
                "status": (
                    ManagedSessionStatus.STOPPED
                    if state == "stopped"
                    else ManagedSessionStatus.IDLE
                ),
                "runtime_status": (
                    AgentRuntimeStatus.OFFLINE if state == "offline" else AgentRuntimeStatus.IDLE
                ),
            }
        )
    return worker


@pytest.mark.parametrize("mode", [WorkspaceTaskMode.REVIEWED, WorkspaceTaskMode.DIRECT])
@pytest.mark.parametrize("state", ["missing", "stopped", "offline"])
async def test_one_shot_orphan_fails_without_redispatch(manager, setup_work, mode, state):
    source, work, task = await _one_shot(manager, setup_work, mode)
    _, _, _, recorded = setup_work
    worker = _lose_worker(manager, task, state)
    before = list(recorded["sent"])
    now = task.updated_at + timedelta(seconds=61)
    await manager._converge_orphaned_hub_tasks(now - timedelta(seconds=2))
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    await manager._converge_orphaned_hub_tasks(now)
    failed = manager.tasks[task.id]
    assert failed.status == WorkspaceTaskStatus.FAILED
    assert "resume explicitly" in failed.failure_reason
    assert failed.failed_at == now
    assert failed.human_acceptance_requested_at == now
    assert failed.human_accepted_at is None
    assert failed.source_work_id == work.id
    assert failed.task_mode == mode
    assert not failed.system_internal and failed.internal_kind is None
    assert failed.dispatch_attempt == task.dispatch_attempt
    assert worker.id not in manager.sessions
    if state != "missing":
        assert worker.tab_id in recorded["deleted_tabs"]
    assert manager.list_chat_work(source.id)[0].status == "failed"
    reloaded = WorkspaceManager()
    assert reloaded.tasks[task.id].status == WorkspaceTaskStatus.FAILED
    await manager._converge_orphaned_hub_tasks(now + timedelta(minutes=2))
    await manager._fire_scheduled_task(manager.scheduled_tasks[work.id], now + timedelta(minutes=2))
    assert len(manager.tasks) == 1
    assert recorded["sent"] == before


@pytest.mark.parametrize("action", ["stop", "accept"])
async def test_orphan_waiter_preserves_control(manager, setup_work, monkeypatch, action):
    source, work, task = await _one_shot(manager, setup_work, WorkspaceTaskMode.DIRECT)
    _lose_worker(manager, task, "stopped")
    monkeypatch.setattr(manager, "_interrupt_session", AsyncNoop.dispatch_noop)
    lock = manager._hubtask_orphan_locks.setdefault(task.id, asyncio.Lock())
    await lock.acquire()
    pending = asyncio.create_task(
        manager._converge_orphaned_hub_tasks(task.updated_at + timedelta(seconds=61))
    )
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        if action == "stop":
            await manager.update_chat_work(source.id, work.id, ChatWorkUpdate(action="stop"))
        else:
            await manager.report_chat_work(
                source.id,
                work.id,
                ChatWorkReport(
                    task_id=task.id,
                    session_id=task.session_id,
                    kind="completed",
                    summary="Evidence arrived before recovery",
                ),
            )
            assert manager.tasks[task.id].human_acceptance_requested_at is not None
            await manager.update_task_status(task.id, WorkspaceTaskStatus.DONE)
        expected = manager.tasks[task.id].model_copy(deep=True)
    finally:
        lock.release()
        await asyncio.wait_for(pending, 5)
    assert manager.tasks[task.id] == expected
    assert len(manager.tasks) == 1


@pytest.mark.parametrize("action", ["pause", "stop"])
async def test_waiting_fire_observes_control(manager, setup_work, monkeypatch, action):
    _, source, payload, recorded = setup_work
    work = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[work.active_task_id]
    stale = manager.scheduled_tasks[work.id]
    now = stale.next_run_at
    before = list(recorded["sent"])
    entered, release = asyncio.Event(), asyncio.Event()
    create_report = manager.create_report

    async def gated_report(session_id, body):
        entered.set()
        await release.wait()
        return await create_report(session_id, body)

    monkeypatch.setattr(manager, "create_report", gated_report)
    reporting = asyncio.create_task(
        manager.report_chat_work(
            source.id,
            work.id,
            ChatWorkReport(
                task_id=task.id, session_id=task.session_id, kind="no_change", summary="Unchanged"
            ),
        )
    )
    calls = [reporting]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        control = asyncio.create_task(
            manager.update_chat_work(source.id, work.id, ChatWorkUpdate(action=action))
        )
        calls.append(control)
        await asyncio.sleep(0)
        firing = asyncio.create_task(manager._fire_scheduled_task(stale, now))
        calls.append(firing)
        await asyncio.sleep(0)
        assert not control.done() and not firing.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*calls), 5)
    live = manager.scheduled_tasks[work.id]
    assert live is not stale
    assert not live.enabled and live.next_run_at is None
    assert live.run_count == 1
    assert len(manager.tasks) == 1
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
    assert recorded["sent"] == before
    assert WorkspaceManager().list_chat_work(source.id)[0].status == (
        "paused" if action == "pause" else "stopped"
    )


@pytest.mark.parametrize("manual", [False, True])
async def test_waiting_fire_ignores_deleted_schedule(manager, setup_work, manual):
    _, source, payload, recorded = setup_work
    work = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[work.active_task_id]
    await manager.update_task_status(task.id, WorkspaceTaskStatus.DONE)
    stale = manager.scheduled_tasks[work.id]
    lock = manager._sched_fire_locks[work.id]
    await lock.acquire()
    before = list(recorded["sent"])
    pending = asyncio.create_task(
        manager._fire_scheduled_task(stale, stale.next_run_at, manual=manual)
    )
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        assert manager.delete_scheduled_task(work.id)
    finally:
        lock.release()
    if manual:
        with pytest.raises(KeyError, match=work.id):
            await asyncio.wait_for(pending, 5)
    else:
        assert await asyncio.wait_for(pending, 5) is None
    assert work.id not in manager.scheduled_tasks
    assert len(manager.tasks) == 1
    assert recorded["sent"] == before


@pytest.mark.parametrize("victim_kind", ["schedule", "done_task", "todo_task"])
async def test_reconcile_skips_deleted_entry(manager, setup_work, monkeypatch, victim_kind):
    _, source, payload, _ = setup_work
    work = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[work.active_task_id]
    tab_id = manager.sessions[task.session_id].tab_id
    await manager.update_task_status(task.id, WorkspaceTaskStatus.DONE)
    if victim_kind == "schedule":
        victim = await manager.create_chat_work(
            source.id, payload().model_copy(update={"request_key": "second"})
        )
    else:
        victim = manager._create_task(
            task.workspace_id,
            WorkspaceTaskCreate(title="Older check", prompt="Older check"),
            source_work_id=work.id,
        )
        manager.tasks[victim.id] = victim.model_copy(
            update={
                "created_at": task.created_at - timedelta(seconds=1),
                "status": (
                    WorkspaceTaskStatus.DONE
                    if victim_kind == "done_task"
                    else WorkspaceTaskStatus.TODO
                ),
            }
        )
    entered, release = asyncio.Event(), asyncio.Event()
    delete_tab = ttyd_manager.delete_tab

    async def gated_delete(candidate):
        if candidate == tab_id:
            entered.set()
            await release.wait()
        await delete_tab(candidate)

    monkeypatch.setattr(ttyd_manager, "delete_tab", gated_delete)
    drain = AsyncMock()
    monkeypatch.setattr(manager, "_drain_scheduled_chat_runs", drain)
    manager._scheduled_chat_recovery_pending = False
    monkeypatch.setattr(_wm, "_now", lambda: task.created_at)
    pending = asyncio.create_task(manager._tick_scheduled_tasks())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if victim_kind == "schedule":
            assert manager.delete_scheduled_task(victim.id)
        else:
            manager.delete_task(victim.id)
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    drain.assert_awaited_once()
    reloaded = WorkspaceManager()
    if victim_kind == "schedule":
        assert victim.id not in reloaded.scheduled_tasks
    else:
        assert victim.id not in reloaded.tasks


async def test_cleanup_rechecks_deleted_task_after_lock(manager, setup_work):
    _, source, payload, recorded = setup_work
    work = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[work.active_task_id]
    await manager.update_task_status(task.id, WorkspaceTaskStatus.DONE)
    lock = manager._workspace_mutation_lock(task.workspace_id)
    await lock.acquire()
    pending = asyncio.create_task(manager._cleanup_chat_work_session(task))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        manager.delete_task(task.id)
    finally:
        lock.release()
        await asyncio.wait_for(pending, 5)
    assert task.id not in manager.tasks
    assert task.session_id in manager.sessions
    assert not recorded["deleted_tabs"]


async def test_stop_tolerates_task_deleted_during_interrupt(manager, setup_work, monkeypatch):
    _, source, payload, _ = setup_work
    work = await manager.create_chat_work(source.id, payload())
    task = manager.tasks[work.active_task_id]
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_interrupt(session):
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "_interrupt_session", gated_interrupt)
    pending = asyncio.create_task(
        manager.update_chat_work(source.id, work.id, ChatWorkUpdate(action="stop"))
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        manager.delete_task(task.id)
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    assert task.id not in manager.tasks
    assert manager.list_chat_work(source.id)[0].status == "stopped"


@pytest.mark.parametrize("change", ["shared", "reincarnated"])
async def test_orphan_cleanup_preserves_unowned_worker(manager, setup_work, change):
    _, _, task = await _one_shot(manager, setup_work)
    _, _, _, recorded = setup_work
    worker = _lose_worker(manager, task, "stopped")
    updates = (
        {"caller_owned_ephemeral": False}
        if change == "shared"
        else {"tab_id": "different-incarnation"}
    )
    manager.sessions[worker.id] = manager.sessions[worker.id].model_copy(update=updates)
    await manager._converge_orphaned_hub_tasks(task.updated_at + timedelta(seconds=61))
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.FAILED
    assert worker.id in manager.sessions
    assert not recorded["deleted_tabs"]
