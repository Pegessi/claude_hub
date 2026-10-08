"""Initiator Tasks persist progress without acquiring managed execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    AutonomyPolicy,
    ManagedSession,
    ManagedSessionStatus,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    ReviewDecision,
    ReviewProfile,
    User,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.models.agent_stream import AgentStreamEvent, AgentStreamEventType
from claude_hub.models.schemas import (
    TaskExecutionControl,
    TaskExecutionHandoffRequest,
    TaskExecutionReference,
    TaskManualProgressRequest,
    TaskProgressRequest,
    TaskRuntimeObservation,
    TaskSourceReference,
)
from claude_hub.services.agent_stream.tailer import SessionTailer
from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager._dispatch import _DispatchMixin
from claude_hub.services.workspace_manager._reports import _ReportsMixin
from claude_hub.services.workspace_manager._task_execution import (
    TaskExecutionConflict,
    TaskReporterForbidden,
)

_wm = import_module("claude_hub.services.workspace_manager")
_api = import_module("claude_hub.api.workspaces")
KEY = "A" * 48
NEW_KEY = "B" * 48


def forbidden(*args, **kwargs):
    pytest.fail("unexpected external transport or managed execution side effect")


@pytest.fixture(autouse=True)
def no_default_http(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture()
def manager(monkeypatch, tmp_path):
    root = tmp_path / "state"
    for module in (_wm, _wm._state, _wm._persistence, _wm._scheduling):
        monkeypatch.setattr(module, "STATE_ROOT", root, raising=False)
        monkeypatch.setattr(module, "INDEX_FILE", root / "index.json", raising=False)
        monkeypatch.setattr(module, "LEGACY_STATE_FILE", root / "absent-legacy.json", raising=False)
        monkeypatch.setattr(module, "SCHEDULED_TASKS_FILE", root / "scheduled.json", raising=False)
    monkeypatch.setattr(_wm.ttyd_manager, "list_tab_agent_statuses", AsyncMock(return_value=[]))
    result = WorkspaceManager()
    for name in (
        "ensure_workspace_agent",
        "_select_or_create_reviewer",
        "dispatch_workspace",
        "send_session_message",
        "_best_effort_delete_session",
    ):
        monkeypatch.setattr(result, name, AsyncMock(side_effect=forbidden))
    return result


@pytest.fixture()
def workspace(manager, tmp_path):
    return manager.create_workspace(WorkspaceCreate(name="Execution ownership", path=str(tmp_path)))


def creation(**overrides):
    values = {
        "title": "one task",
        "prompt": "complete one bounded objective",
        "execution_control": "initiator",
        "request_key": "request-1",
        "reporter_key": KEY,
        "source": TaskSourceReference(kind="chat", tab_id="caller-tab"),
    }
    values.update(overrides)
    return WorkspaceTaskCreate(**values)


async def register(manager, workspace, **overrides):
    task, replayed = await manager.register_task(workspace.id, creation(**overrides), "actor-a")
    assert not replayed
    return task


def progress(task, state="working", call_id="progress-1", **overrides):
    values = {
        "call_id": call_id,
        "expected_execution_epoch": task.execution_epoch,
        "expected_progress_revision": task.progress_revision,
        "state": state,
        "summary": f"explicit {state} report",
    }
    values.update(overrides)
    return TaskProgressRequest(**values)


def handoff(task, target="workspace", call_id="handoff-1", **overrides):
    values = {
        "call_id": call_id,
        "expected_execution_epoch": task.execution_epoch,
        "expected_progress_revision": task.progress_revision,
        "execution_control": target,
    }
    if target == "initiator":
        values["new_reporter_key"] = NEW_KEY
    values.update(overrides)
    return TaskExecutionHandoffRequest(**values)


@pytest.mark.asyncio
async def test_registration_is_scoped_idempotent_and_survives_restart(manager, workspace):
    task = await register(manager, workspace)
    same, replayed = await manager.register_task(workspace.id, creation(), "actor-a")
    assert replayed and same.id == task.id
    other, replayed = await manager.register_task(workspace.id, creation(), "actor-b")
    assert not replayed and other.id != task.id
    for changed in ({"title": "different"}, {"reporter_key": NEW_KEY}):
        with pytest.raises(TaskExecutionConflict, match="request_key_conflict"):
            await manager.register_task(workspace.id, creation(**changed), "actor-a")
    state_text = manager._workspace_state_file(workspace.id).read_text()
    assert KEY not in state_text
    assert hashlib.sha256(KEY.encode()).hexdigest() in state_text
    assert "reporter_key_hash" not in task.model_dump(mode="json")
    fresh = WorkspaceManager()
    same, replayed = await fresh.register_task(workspace.id, creation(), "actor-a")
    assert replayed and same.id == task.id
    result = await fresh.record_task_progress(workspace.id, task.id, progress(same), KEY)
    assert result.task.status == WorkspaceTaskStatus.WORKING
    assert not fresh.sessions


@pytest.mark.asyncio
async def test_progress_is_atomic_scoped_and_has_no_managed_side_effects(manager, workspace):
    task = await register(manager, workspace)
    request = progress(task)
    first, second = await asyncio.gather(
        manager.record_task_progress(workspace.id, task.id, request, KEY),
        manager.record_task_progress(workspace.id, task.id, request, KEY),
    )
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert first.event.sequence == second.event.sequence
    assert manager.tasks[task.id].progress_revision == 1
    assert len(manager.task_mailbox._events[workspace.id]) == 1
    with pytest.raises(TaskReporterForbidden):
        await manager.record_task_progress(workspace.id, task.id, request, NEW_KEY)
    with pytest.raises(TaskExecutionConflict):
        await manager.record_task_progress(
            workspace.id, task.id, request.model_copy(update={"summary": "changed"}), KEY
        )
    with pytest.raises(TaskExecutionConflict):
        await manager.record_task_progress(
            workspace.id, task.id, request.model_copy(update={"call_id": "stale-new-id"}), KEY
        )
    task = manager.tasks[task.id]
    completed = await manager.record_task_progress(
        workspace.id, task.id, progress(task, "completed", "complete-1"), KEY
    )
    assert completed.task.status == WorkspaceTaskStatus.DONE
    assert completed.task.human_accepted_at is None
    assert completed.task.review_requested_at is None
    assert completed.task.session_id is None
    assert not manager.sessions and not manager.reports
    cleanup = await manager.cleanup_task_session(task.id)
    assert cleanup.action == "skipped"


@pytest.mark.asyncio
async def test_manual_close_is_not_executor_release_and_handoff_retains_policy(manager, workspace):
    task = await register(
        manager,
        workspace,
        task_mode=WorkspaceTaskMode.AUTONOMOUS,
        review_profiles=[ReviewProfile.BOUNDARY],
        autonomy_policy=AutonomyPolicy(max_iterations=5),
    )
    assert task.autonomous_run is None
    request = TaskManualProgressRequest(
        **progress(task, "completed").model_dump(exclude_unset=True)
    )
    result = await manager.record_manual_task_progress(workspace.id, task.id, request)
    assert result.event.actor_role.value == "human"
    assert result.task.status == WorkspaceTaskStatus.DONE
    assert not result.task.execution_released
    with pytest.raises(TaskExecutionConflict, match="not_released"):
        await manager.handoff_task_execution(workspace.id, task.id, handoff(result.task))
    released = await manager.record_task_progress(
        workspace.id, task.id, progress(result.task, "released", "release-1"), KEY
    )
    transfer = handoff(released.task)
    moved = await manager.handoff_task_execution(workspace.id, task.id, transfer)
    repeated = await manager.handoff_task_execution(workspace.id, task.id, transfer)
    assert repeated.replayed and repeated.event.sequence == moved.event.sequence
    assert moved.task.id == task.id and moved.task.execution_epoch == 2
    assert moved.task.status == WorkspaceTaskStatus.TODO
    assert moved.task.review_profiles == [ReviewProfile.BOUNDARY]
    assert moved.task.task_mode == WorkspaceTaskMode.AUTONOMOUS
    assert moved.task.autonomy_policy.max_iterations == 5
    assert moved.task.autonomous_run is None
    assert moved.task.reporter_key_hash is None
    assert not manager.sessions


@pytest.mark.asyncio
async def test_restarted_initiator_epoch_rejects_old_writer(manager, workspace):
    task = await register(manager, workspace)
    old_request = progress(task)
    released = await manager.record_task_progress(
        workspace.id, task.id, progress(task, "released", "release-1"), KEY
    )
    moved = await manager.handoff_task_execution(
        workspace.id, task.id, handoff(released.task, "initiator")
    )
    with pytest.raises(TaskReporterForbidden):
        await manager.record_task_progress(workspace.id, task.id, old_request, KEY)
    with pytest.raises(TaskExecutionConflict, match="epoch"):
        await manager.record_task_progress(workspace.id, task.id, old_request, NEW_KEY)
    result = await manager.record_task_progress(
        workspace.id, task.id, progress(moved.task, call_id="new-epoch"), NEW_KEY
    )
    assert result.task.progress_revision == moved.task.progress_revision + 1


@pytest.mark.asyncio
async def test_save_failure_rolls_back_task_and_event(manager, workspace, monkeypatch):
    task = await register(manager, workspace)
    before = manager._workspace_state_file(workspace.id).read_bytes()
    original_save = manager._save_state
    monkeypatch.setattr(manager, "_save_state", lambda: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)
    assert manager.tasks[task.id].progress_revision == 0
    assert not manager.task_mailbox._events.get(workspace.id)
    assert manager._workspace_state_file(workspace.id).read_bytes() == before
    monkeypatch.setattr(manager, "_save_state", original_save)
    result = await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)
    assert result.task.progress_revision == 1


@pytest.mark.asyncio
async def test_metadata_edit_and_control_actions_do_not_touch_source(manager, workspace):
    now = _wm._now()
    source = ManagedSession(
        id="caller",
        workspace_id=workspace.id,
        tab_id="caller-tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="caller",
        workspace_path=workspace.path,
        tmux_session="not-used",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[source.id] = source
    task = await register(manager, workspace)
    before = source.model_dump(mode="json")
    renamed = await manager.update_task(task.id, WorkspaceTaskUpdate(title="renamed"))
    assert renamed.title == "renamed"
    assert manager.sessions[source.id].model_dump(mode="json") == before
    for operation in (
        lambda: manager.start_task(task.id),
        lambda: manager.continue_task(task.id),
        lambda: manager.request_task_review(task.id),
        lambda: manager.abort_task(task.id, ManualTaskControlRequest(reason="stop")),
        lambda: manager.update_task(task.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE)),
    ):
        with pytest.raises(TaskExecutionConflict):
            await operation()
    manager.delete_task(task.id)
    assert manager.sessions[source.id].model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_managed_operation_reservation_blocks_handoff_without_holding_workspace_lock(
    manager, workspace, monkeypatch
):
    task = manager.create_task(
        workspace.id, WorkspaceTaskCreate(title="managed", prompt="manual dispatch")
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_start(task_id, payload=None):
        entered.set()
        await release.wait()
        return manager.tasks[task_id]

    monkeypatch.setattr(manager, "_start_task_workspace", blocked_start)
    pending = asyncio.create_task(manager.start_task(task.id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(TaskExecutionConflict, match="not_released"):
            await asyncio.wait_for(
                manager.handoff_task_execution(workspace.id, task.id, handoff(task, "initiator")),
                1,
            )
    finally:
        release.set()
        await asyncio.wait_for(pending, 1)
    assert not manager._task_execution_operations
    result = await manager.handoff_task_execution(
        workspace.id, task.id, handoff(manager.tasks[task.id], "initiator")
    )
    assert result.task.execution_control == TaskExecutionControl.INITIATOR


@pytest.mark.asyncio
async def test_runtime_activity_never_completes_task_or_grows_mailbox(manager, workspace):
    ref = TaskExecutionReference(session_id="caller", turn_id="turn-1", run_epoch=1)
    task = await register(manager, workspace, execution_ref=ref)
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    events_before = len(manager.task_mailbox._events[workspace.id])
    for sequence in range(1, 101):
        activity = TaskRuntimeObservation(
            execution_epoch=task.execution_epoch,
            stream_sequence=sequence,
            status="active",
            observed_at=_wm._now(),
            turn_id="turn-1",
            detail="activity",
        )
        changed = await manager.record_task_activity(workspace.id, task.id, ref, activity)
        assert changed is (sequence == 1)
    stale = activity.model_copy(update={"stream_sequence": 50, "status": "idle"})
    assert not await manager.record_task_activity(workspace.id, task.id, ref, stale)
    ended = activity.model_copy(update={"stream_sequence": 101, "status": "idle"})
    assert await manager.record_task_activity(workspace.id, task.id, ref, ended)
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[task.id].progress_revision == task.progress_revision
    assert len(manager.task_mailbox._events[workspace.id]) == events_before


@pytest.mark.asyncio
async def test_receipts_are_bounded_and_expired_retries_do_not_mutate(manager, workspace):
    task = await register(manager, workspace)
    first = progress(task, call_id="step-0")
    for index in range(130):
        task = manager.tasks[task.id]
        await manager.record_task_progress(
            workspace.id, task.id, progress(task, call_id=f"step-{index}"), KEY
        )
    assert len(manager.tasks[task.id].execution_call_fingerprints) == 128
    with pytest.raises(TaskExecutionConflict):
        await manager.record_task_progress(workspace.id, task.id, first, KEY)
    assert manager.tasks[task.id].progress_revision == 130


@pytest.mark.asyncio
async def test_restart_rejects_managed_assignment_on_record_task(manager, workspace):
    task = await register(manager, workspace)
    path = manager._workspace_state_file(workspace.id)
    data = json.loads(path.read_text())
    data["tasks"][0]["session_id"] = "forged-session"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="managed execution"):
        WorkspaceManager()


@pytest.fixture()
def api_client(manager, workspace, monkeypatch):
    monkeypatch.setattr(_api, "workspace_manager", manager)
    monkeypatch.setattr(_api.settings, "feishu_app_id", "test-oauth-app")
    api = FastAPI()
    api.include_router(_api.router)
    api.dependency_overrides[get_current_user] = lambda: User(
        open_id="test-user", name="test", email="test@example.invalid"
    )
    with TestClient(api) as client:
        yield client


def api_create_body(**overrides):
    body = {
        "title": "api record",
        "prompt": "track only",
        "execution_control": "initiator",
        "request_key": "api-request-1",
        "reporter_key": KEY,
    }
    body.update(overrides)
    return body


def test_api_create_replay_and_validation_never_echo_credentials(api_client, workspace):
    url = f"/api/workspaces/{workspace.id}/tasks"
    first = api_client.post(url, json=api_create_body())
    second = api_client.post(url, json=api_create_body())
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["X-Task-Replayed"] == "true"
    conflict = api_client.post(url, json=api_create_body(reporter_key=NEW_KEY))
    assert conflict.status_code == 409
    for body in (
        api_create_body(reporter_key="short-secret"),
        api_create_body(source={"kind": "invalid", "nested": KEY}),
        api_create_body(reporter_key=KEY + "\n"),
    ):
        response = api_client.post(url, json=body)
        assert response.status_code in {400, 422}
        assert KEY not in response.text and "short-secret" not in response.text
    for response in (first, second, conflict):
        assert KEY not in response.text and NEW_KEY not in response.text
        assert hashlib.sha256(KEY.encode()).hexdigest() not in response.text


def test_api_manual_close_cannot_release_or_handoff(api_client, workspace):
    base = f"/api/workspaces/{workspace.id}/tasks"
    task = api_client.post(base, json=api_create_body()).json()
    url = f"{base}/{task['id']}"
    body = {
        "call_id": "manual-1",
        "expected_execution_epoch": 1,
        "expected_progress_revision": 0,
        "state": "completed",
        "summary": "human closed record",
    }
    assert api_client.post(url + "/progress", json=body).status_code == 403
    closed = api_client.post(url + "/progress/manual", json=body)
    assert closed.status_code == 200
    assert closed.json()["task"]["status"] == "done"
    assert closed.json()["task"]["human_accepted_at"] is None
    assert closed.json()["event"]["actor_role"] == "human"
    bad_release = api_client.post(url + "/progress/manual", json={**body, "state": "released"})
    assert bad_release.status_code == 422
    moved = api_client.post(
        url + "/handoff",
        json={
            "call_id": "move-1",
            "expected_execution_epoch": 1,
            "expected_progress_revision": 1,
            "execution_control": "workspace",
        },
    )
    assert moved.status_code == 409
    bad_key = api_client.post(
        url + "/handoff",
        json={
            "call_id": "move-2",
            "expected_execution_epoch": 1,
            "expected_progress_revision": 1,
            "execution_control": "initiator",
            "new_reporter_key": "private-short-secret",
        },
    )
    assert bad_key.status_code == 422
    assert "private-short-secret" not in bad_key.text


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_tag", [None, "archive-audit"])
@pytest.mark.parametrize("handed_off", [False, True])
async def test_archive_excludes_private_state_fields_and_preserves_agent_tag(
    manager, workspace, monkeypatch, tmp_path, agent_tag, handed_off
):
    task = await register(manager, workspace, agent_tag=agent_tag)
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    if handed_off:
        task = (
            await manager.record_task_progress(
                workspace.id, task.id, progress(task, "released", "release-for-archive"), KEY
            )
        ).task
        task = (
            await manager.handoff_task_execution(
                workspace.id, task.id, handoff(task, call_id="handoff-for-archive")
            )
        ).task
        assert task.execution_control == TaskExecutionControl.WORKSPACE

    private_state = manager._task_dump_for_state(task)
    private_names = {
        "reporter_key_hash",
        "creation_request_key",
        "creation_actor_key",
        "creation_fingerprint",
        "execution_call_fingerprints",
    }
    assert private_state["creation_request_key"]
    assert private_state["creation_actor_key"]
    assert private_state["creation_fingerprint"]
    assert private_state["execution_call_fingerprints"]
    if not handed_off:
        assert private_state["reporter_key_hash"] == hashlib.sha256(KEY.encode()).hexdigest()

    archive_dir = tmp_path / "task-archives"
    monkeypatch.setattr(manager, "_workspace_task_records_dir", lambda _workspace_id: archive_dir)
    # Exercise public serialization without starting or completing managed work.
    archived = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.DONE,
            "completed_at": _wm._now(),
        }
    )
    manager._write_task_record(archived)
    paths = list(archive_dir.glob("*.json"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    public_task = json.loads(text)["task"]
    assert private_names.isdisjoint(public_task)
    assert KEY not in text
    for name in ("creation_request_key", "creation_actor_key", "creation_fingerprint"):
        assert private_state[name] not in text
    assert hashlib.sha256(KEY.encode()).hexdigest() not in text
    if agent_tag is None:
        assert "agent_tag" not in public_task
    else:
        assert public_task["agent_tag"] == agent_tag
    assert manager._task_dump_for_state(task) == private_state


def _observed_event(sequence=1, **overrides):
    values = {
        "stream_sequence": sequence,
        "session_id": "observed-session",
        "tab_id": "caller-tab",
        "agent_type": AgentType.CLAUDE,
        "type": AgentStreamEventType.TURN_STARTED,
        "run_epoch": 7,
        "turn_id": "observed-turn",
        "payload": {"_hub_main_thread_id": "main-thread"},
        "created_at": _wm._now(),
    }
    values.update(overrides)
    return AgentStreamEvent(**values)


def _observed_ref(**overrides):
    values = {
        "provider": "claude",
        "session_id": "observed-session",
        "thread_id": "main-thread",
        "turn_id": "observed-turn",
        "run_epoch": 7,
    }
    values.update(overrides)
    return TaskExecutionReference(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": "other-session"},
        {"run_epoch": 8},
        {"turn_id": "other-turn"},
        {"agent_type": AgentType.CODEX},
        {"payload": {"_hub_main_thread_id": "other-main-thread"}},
        {"payload": {"_hub_main_thread_id": "main-thread", "subagent_thread": "child-thread"}},
    ],
)
async def test_stream_activity_requires_exact_observed_execution(manager, workspace, changes):
    task = await register(manager, workspace, execution_ref=_observed_ref())
    await manager.record_task_stream_event(_observed_event(**changes))
    assert manager.tasks[task.id].runtime_observation is None
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.TODO
    await manager.record_task_stream_event(_observed_event(2))
    assert manager.tasks[task.id].runtime_observation.status == "active"
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.TODO
    assert not manager.sessions


@pytest.mark.asyncio
async def test_main_ref_without_thread_does_not_claim_child_activity(manager, workspace):
    task = await register(manager, workspace, execution_ref=_observed_ref(thread_id=None))
    await manager.record_task_stream_event(_observed_event(payload={"subagent_thread": "child"}))
    assert manager.tasks[task.id].runtime_observation is None
    await manager.record_task_stream_event(_observed_event(2, payload={}))
    assert manager.tasks[task.id].runtime_observation.status == "active"


@pytest.mark.asyncio
async def test_explicit_child_ref_only_receives_that_child(manager, workspace):
    task = await register(manager, workspace, execution_ref=_observed_ref(thread_id="child-a"))
    await manager.record_task_stream_event(_observed_event(payload={"subagent_thread": "child-b"}))
    assert manager.tasks[task.id].runtime_observation is None
    await manager.record_task_stream_event(_observed_event())
    assert manager.tasks[task.id].runtime_observation is None
    await manager.record_task_stream_event(
        _observed_event(3, payload={"subagent_thread": "child-a"})
    )
    assert manager.tasks[task.id].runtime_observation.status == "active"
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.TODO


@pytest.mark.asyncio
async def test_stream_end_and_plan_do_not_complete_task(manager, workspace):
    task = await register(manager, workspace, execution_ref=_observed_ref())
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    await manager.record_task_stream_event(_observed_event(1))
    await manager.record_task_stream_event(
        _observed_event(
            2,
            type=AgentStreamEventType.TEXT_DELTA,
            payload={"text": "- [x] all steps", "plan": True, "plan_kind": "progress"},
        )
    )
    assert manager.tasks[task.id].runtime_observation.stream_sequence == 1
    await manager.record_task_stream_event(
        _observed_event(
            3,
            type=AgentStreamEventType.TURN_COMPLETED,
            payload={"status": "completed", "_hub_main_thread_id": "main-thread"},
        )
    )
    current = manager.tasks[task.id]
    assert current.runtime_observation.status == "idle"
    assert current.status == WorkspaceTaskStatus.WORKING
    assert current.progress_revision == task.progress_revision
    assert current.completed_at is None
    assert current.human_accepted_at is None


@pytest.mark.asyncio
async def test_restart_marks_activity_unknown_without_starting_runtime(
    manager, workspace, monkeypatch
):
    task = await register(manager, workspace, execution_ref=_observed_ref())
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    await manager.record_task_stream_event(_observed_event())
    assert manager.tasks[task.id].runtime_observation.status == "active"
    monkeypatch.setattr(
        WorkspaceManager, "ensure_workspace_agent", AsyncMock(side_effect=forbidden)
    )
    monkeypatch.setattr(WorkspaceManager, "dispatch_workspace", AsyncMock(side_effect=forbidden))
    fresh = WorkspaceManager()
    restored = fresh.tasks[task.id]
    assert restored.runtime_observation.status == "unknown"
    assert restored.runtime_observation.stream_sequence == 1
    assert restored.status == WorkspaceTaskStatus.WORKING
    assert restored.progress_revision == task.progress_revision
    assert restored.execution_ref == task.execution_ref
    assert not fresh.sessions


@pytest.mark.asyncio
async def test_live_publish_observes_high_level_events_only(manager, workspace):
    task = await register(manager, workspace, execution_ref=_observed_ref())
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    observer = AsyncMock(side_effect=manager.record_task_stream_event)
    store = SimpleNamespace(append=AsyncMock(side_effect=lambda event: event))
    tailer = SimpleNamespace(
        _store=store,
        _is_live=True,
        _fanout=Mock(),
        _task_activity_observer=observer,
        _native_transport=SimpleNamespace(active_thread_id="main-thread"),
    )
    event = _observed_event(
        10, type=AgentStreamEventType.TURN_COMPLETED, payload={"status": "completed"}
    )
    await SessionTailer._persist_and_fanout(tailer, event)
    observer.assert_awaited_once()
    assert manager.tasks[task.id].runtime_observation.status == "idle"
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert "_hub_main_thread_id" not in event.payload
    assert store.append.call_args.args[0] is event
    tailer._fanout.assert_called_once_with(event)
    for event_type in (AgentStreamEventType.TEXT_DELTA, AgentStreamEventType.THINKING_DELTA):
        await SessionTailer._persist_and_fanout(tailer, _observed_event(11, type=event_type))
    assert observer.await_count == 1
    tailer._is_live = False
    await SessionTailer._persist_and_fanout(tailer, _observed_event(12))
    assert observer.await_count == 1


@pytest.mark.asyncio
async def test_observer_error_does_not_fail_completion_publish(caplog):
    async def failed_observer(event):
        raise RuntimeError("private-observer-detail")

    event = _observed_event(
        1, type=AgentStreamEventType.TURN_COMPLETED, payload={"status": "completed"}
    )
    store = SimpleNamespace(append=AsyncMock(side_effect=lambda value: value))
    tailer = SimpleNamespace(
        _store=store,
        _is_live=True,
        _fanout=Mock(),
        _task_activity_observer=failed_observer,
        _native_transport=SimpleNamespace(active_thread_id="main-thread"),
    )
    await SessionTailer._persist_and_fanout(tailer, event)
    store.append.assert_awaited_once_with(event)
    tailer._fanout.assert_called_once_with(event)
    assert event.payload == {"status": "completed"}
    assert "RuntimeError" in caplog.text
    assert "private-observer-detail" not in caplog.text


def _context_tailer(session_id="terminal-tab-caller-tab"):
    runtime = SimpleNamespace(
        session=SimpleNamespace(id=session_id, tab_id="caller-tab", agent_type=AgentType.CLAUDE),
        turn_in_flight=True,
        active_thread_id="main-thread",
    )
    tailer = SimpleNamespace(
        _native_transport=runtime,
        session_id=session_id,
        _active_turn_id="observed-turn",
        _run_epoch=7,
        _hard_failed=False,
        is_running=lambda: True,
    )
    tailer.peek_task_execution_ref = lambda tab_id: SessionTailer.peek_task_execution_ref(
        tailer, tab_id
    )
    return tailer


def test_tab_context_uses_existing_runtime_only(manager, workspace, monkeypatch):
    stream_api = import_module("claude_hub.api.agent_stream")
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(
        stream_api.ttyd_manager, "get_tab", lambda tab_id: SimpleNamespace(id=tab_id)
    )
    monkeypatch.setattr(stream_api, "_get_tailer_manager", forbidden)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", forbidden)
    monkeypatch.setattr(stream_api, "_terminal_tab_stream_session", forbidden)
    monkeypatch.setattr(stream_api, "_tailer_manager", None)
    monkeypatch.setattr(stream_api, "_tab_tailer_manager", None)
    api = FastAPI()
    api.include_router(stream_api.router)
    api.dependency_overrides[get_current_user] = lambda: User(
        open_id="test-user", name="test", email="test@example.invalid"
    )
    with TestClient(api) as client:
        url = "/api/workspaces/tabs/caller-tab/task-context"
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["reason"] == "not_observed"
        assert response.json()["execution_ref"] is None
        tailer = _context_tailer()
        monkeypatch.setattr(
            stream_api,
            "_tab_tailer_manager",
            SimpleNamespace(_tailers={"terminal-tab-caller-tab": tailer}),
        )
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["reason"] == "active"
        assert response.json()["execution_ref"] == {
            "provider": "claude",
            "session_id": "terminal-tab-caller-tab",
            "thread_id": "main-thread",
            "turn_id": "observed-turn",
            "run_epoch": 7,
        }
        assert response.json()["workspace_ids"] == []
        assert not manager.sessions
        tailer._native_transport.turn_in_flight = False
        response = client.get(url)
        assert response.json()["reason"] == "inactive"
        assert response.json()["execution_ref"] is None
        monkeypatch.setattr(stream_api.ttyd_manager, "get_tab", lambda tab_id: None)
        assert client.get(url).status_code == 404


def test_tab_context_rejects_multiple_live_runtime_candidates(manager, workspace, monkeypatch):
    stream_api = import_module("claude_hub.api.agent_stream")
    now = _wm._now()
    managed = ManagedSession(
        id="existing-managed",
        workspace_id=workspace.id,
        tab_id="caller-tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        title="existing",
        workspace_path=workspace.path,
        tmux_session="not-used",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[managed.id] = managed
    before = managed.model_dump(mode="json")
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(
        stream_api.ttyd_manager, "get_tab", lambda tab_id: SimpleNamespace(id=tab_id)
    )
    monkeypatch.setattr(stream_api, "TailerManager", forbidden)
    monkeypatch.setattr(stream_api, "_get_tailer_manager", forbidden)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", forbidden)
    monkeypatch.setattr(stream_api, "_terminal_tab_stream_session", forbidden)
    monkeypatch.setattr(
        stream_api,
        "_tab_tailer_manager",
        SimpleNamespace(_tailers={"terminal-tab-caller-tab": _context_tailer()}),
    )
    monkeypatch.setattr(
        stream_api,
        "_tailer_manager",
        SimpleNamespace(_tailers={managed.id: _context_tailer(managed.id)}),
    )
    api = FastAPI()
    api.include_router(stream_api.router)
    api.dependency_overrides[get_current_user] = lambda: User(
        open_id="test-user", name="test", email="test@example.invalid"
    )
    with TestClient(api) as client:
        response = client.get("/api/workspaces/tabs/caller-tab/task-context")
    assert response.status_code == 200
    assert response.json()["reason"] == "ambiguous"
    assert response.json()["execution_ref"] is None
    assert manager.sessions[managed.id].model_dump(mode="json") == before
    assert len(manager.sessions) == 1


def _managed_task_for_review_entry(manager, workspace):
    task = manager.create_task(
        workspace.id, WorkspaceTaskCreate(title="review entry", prompt="check the report contract")
    )
    now = _wm._now()
    worker = ManagedSession(
        id="review-entry-worker",
        workspace_id=workspace.id,
        tab_id="review-entry-tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        task_id=task.id,
        current_task_id=task.id,
        title="worker",
        workspace_path=workspace.path,
        tmux_session="not-used",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[worker.id] = worker
    task = task.model_copy(update={"session_id": worker.id, "status": WorkspaceTaskStatus.WORKING})
    manager.tasks[task.id] = task
    return task


@pytest.mark.asyncio
async def test_public_review_preserves_dispatch_body_and_forwards_report_helper(
    manager, workspace, monkeypatch
):
    assert (
        WorkspaceManager._request_task_review_workspace
        is _DispatchMixin._request_task_review_workspace
    )
    assert (
        WorkspaceManager._request_task_review_workspace_report
        is _ReportsMixin._request_task_review_workspace_report
    )
    task = _managed_task_for_review_entry(manager, workspace)
    report_helper = AsyncMock(return_value=None)
    monkeypatch.setattr(manager, "_request_task_review_workspace_report", report_helper)
    result = await manager.request_task_review(
        task.id, RequestTaskReviewRequest(message="  check the boundary  ")
    )
    report_helper.assert_awaited_once()
    forwarded_task, report = report_helper.await_args.args
    assert forwarded_task is manager.tasks[task.id]
    assert report.task_id == task.id
    assert report.session_id == task.session_id
    assert report.state == AgentReportState.READY_FOR_REVIEW
    assert report.review_decision == ReviewDecision.REQUEST
    assert report.message == "check the boundary"
    assert manager.reports[report.id] is report
    assert result is manager.tasks[task.id]
    manager._select_or_create_reviewer.assert_not_awaited()
    assert not manager._task_execution_operations


@pytest.mark.asyncio
async def test_private_review_preserves_reports_body_and_does_not_hit_dispatch_helper(
    manager, workspace, monkeypatch
):
    class ReviewBodyReached(RuntimeError):
        pass

    task = _managed_task_for_review_entry(manager, workspace)
    report = AgentReport(
        id="review-entry-report",
        workspace_id=workspace.id,
        task_id=task.id,
        session_id=task.session_id,
        state=AgentReportState.READY_FOR_REVIEW,
        message="private entry",
        created_at=_wm._now(),
    )
    selector = AsyncMock(side_effect=ReviewBodyReached("stop before reviewer creation"))
    monkeypatch.setattr(manager, "_select_or_create_reviewer", selector)
    dispatch_helper = AsyncMock(side_effect=forbidden)
    monkeypatch.setattr(manager, "_request_task_review_workspace", dispatch_helper)
    before = task.model_dump(mode="json")
    with pytest.raises(ReviewBodyReached):
        await manager._request_task_review(task, report)
    selector.assert_awaited_once_with(workspace, task)
    dispatch_helper.assert_not_awaited()
    assert manager.tasks[task.id].model_dump(mode="json") == before
    assert not manager.reports
    assert not manager._task_execution_operations


@pytest.mark.asyncio
async def test_delete_rejects_illegal_initiator_backref_before_any_mutation(
    manager, workspace, monkeypatch
):
    task = await register(manager, workspace)
    task = (await manager.record_task_progress(workspace.id, task.id, progress(task), KEY)).task
    now = _wm._now()
    session = ManagedSession(
        id="invalid-backref",
        workspace_id=workspace.id,
        tab_id="not-used",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        task_id=task.id,
        current_task_id=task.id,
        title="source",
        workspace_path=workspace.path,
        tmux_session="not-used",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    before = [event.model_dump(mode="json") for event in manager.task_mailbox._events[workspace.id]]
    disk = manager._workspace_state_file(workspace.id).read_bytes()
    with pytest.raises(TaskExecutionConflict, match="initiator_task_has_managed_session_reference"):
        manager.delete_task(task.id)
    assert manager.tasks[task.id] is task
    assert manager.sessions[session.id] is session
    assert [
        event.model_dump(mode="json") for event in manager.task_mailbox._events[workspace.id]
    ] == before
    assert manager._workspace_state_file(workspace.id).read_bytes() == disk
