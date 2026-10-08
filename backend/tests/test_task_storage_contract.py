"""Reject unknown execution fields without rewriting durable source records."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from importlib import import_module
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentReport,
    AgentReportCreate,
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskUpdate,
    User,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
)
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_task_execution_control import (
    _wm,
    forbidden,
    manager,
    no_default_http,
    workspace,
)

STAMP = "2026-10-05T00:00:00"
TASK_UNKNOWN_FIELDS = (
    "legacy_work_detached",
    "source_work_id",
    "chat_work_outcome",
    "chat_work_report_id",
    "chat_work_summary",
    "chat_work_owned_session_id",
    "chat_work_owned_tab_id",
    "unknown_future_field",
)
SCHEDULE_UNKNOWN_FIELDS = (
    "source_tab_id",
    "source_request_key",
    "source_request_fingerprint",
    "work_kind",
    "work_task_mode",
    "work_cwd",
    "work_model",
    "work_env_preset",
    "work_inherit_source_env",
    "work_stopped_at",
    "work_completed_at",
    "work_pause_requested",
    "unknown_future_field",
)


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    for name in (
        "ensure_workspace_agent",
        "_dispatch_task_to_session",
        "_send_tab_message",
        "_fire_new_session",
        "_fire_hub_task",
        "_drain_scheduled_chat_runs",
    ):
        monkeypatch.setattr(WorkspaceManager, name, AsyncMock(side_effect=forbidden))


def main_task(workspace_id):
    # Full field names from the identical a0e4a617/c37b7f3 persisted model.
    nullable = """
        goal_packet agent_tag autonomy_policy autonomous_run session_id
        related_task_id clear_context parent_task_id dispatch_reason internal_kind
        review_session_id review_requested_at review_completed_at review_skipped_at
        review_skip_reason manual_aborted_at manual_abort_reason timeout_seconds
        failure_reason failed_at human_acceptance_requested_at human_accepted_at
        queued_at started_at reviewed_at completed_at
    """.split()
    arrays = """
        attachments review_profiles delivered_call_ids pending_call_ids
        processing_call_ids uncertain_call_ids feedback_lesson_ids
    """.split()
    return {
        **dict.fromkeys(nullable),
        **{key: [] for key in arrays},
        "id": "main-task",
        "workspace_id": workspace_id,
        "title": "main task",
        "prompt": "ordinary objective",
        "agent_type": "claude",
        "task_mode": "reviewed",
        "execution_complexity": "auto",
        "origin": "human",
        "status": "todo",
        "root_task_id": "main-task",
        "path": "main-task",
        "consumer_ack_sequence": 0,
        "dispatch_pending": False,
        "system_internal": False,
        "review_attempts": 0,
        "dispatch_attempt": 0,
        "review_cycle": 1,
        "reviewed_cycle": 0,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def main_schedule(kind, workspace_id):
    # Full persisted ScheduledTask shape from both deployed/cached main refs.
    return {
        "id": f"main-{kind}",
        "name": kind,
        "kind": kind,
        "enabled": True,
        "run_at": STAMP,
        "cron": None,
        "interval_seconds": None,
        "tab_id": "caller-tab" if kind in {"chat_turn", "tab_message"} else None,
        "workspace_id": workspace_id if kind in {"new_session", "hub_task"} else None,
        "agent_type": "claude",
        "message": "ordinary scheduled message",
        "task_title": "ordinary task" if kind == "hub_task" else None,
        "last_run_at": None,
        "next_run_at": STAMP,
        "last_status": None,
        "last_error": None,
        "last_run_id": None,
        "run_count": 0,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def write_tasks(manager, workspace, tasks, *, migrate=False):
    path = manager._workspace_state_file(workspace.id)
    data = json.loads(path.read_text())
    data["tasks"] = tasks
    if migrate:
        data["agent_runs"] = []
        data["agent_events"] = []
    path.write_text(json.dumps(data, indent=2))
    return path


def write_schedules(items):
    path = _wm._scheduling.SCHEDULED_TASKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scheduled_tasks": items, "scheduled_task_runs": []}, indent=2))
    return path


def test_main_task_and_all_schedule_shapes_load(manager, workspace):
    row = main_task(workspace.id)
    write_tasks(manager, workspace, [row])
    schedules = [
        main_schedule(kind, workspace.id)
        for kind in (
            "chat_turn",
            "tab_message",
            "new_session",
            "hub_task",
        )
    ]
    write_schedules(schedules)
    fresh = WorkspaceManager()
    assert set(row) <= WorkspaceTask.model_fields.keys()
    assert fresh.tasks[row["id"]].title == row["title"]
    assert fresh.tasks[row["id"]].execution_control.value == "workspace"
    assert set(fresh.scheduled_tasks) == {item["id"] for item in schedules}
    for item in schedules:
        assert fresh.scheduled_tasks[item["id"]].model_dump(mode="json") == item
    assert not fresh.sessions


@pytest.mark.parametrize("field", TASK_UNKNOWN_FIELDS)
@pytest.mark.parametrize("migrate", [False, True])
def test_unknown_task_field_preserves_source(manager, workspace, monkeypatch, field, migrate):
    path = write_tasks(
        manager, workspace, [{**main_task(workspace.id), field: None}], migrate=migrate
    )
    before = path.read_bytes()
    backup = path.with_suffix(".json.pre-migration-backup")
    assert not backup.exists()
    monkeypatch.setattr(WorkspaceManager, "_atomic_write_text", Mock(side_effect=forbidden))
    with pytest.raises(ValueError):
        WorkspaceManager()
    assert path.read_bytes() == before
    assert not backup.exists()


@pytest.mark.parametrize("bad_identity", [{"id": ""}, {"workspace_id": "different-workspace"}])
def test_migration_cannot_discard_unknown_task_row(manager, workspace, monkeypatch, bad_identity):
    row = {**main_task(workspace.id), **bad_identity, "unknown_future_field": None}
    path = write_tasks(manager, workspace, [row], migrate=True)
    before = path.read_bytes()
    monkeypatch.setattr(WorkspaceManager, "_atomic_write_text", Mock(side_effect=forbidden))
    with pytest.raises(ValueError):
        WorkspaceManager()
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.pre-migration-backup").exists()


def test_existing_agent_tree_migration_still_loads_valid_task(manager, workspace):
    row = {**main_task(workspace.id), "agent_run_id": None}
    path = write_tasks(manager, workspace, [row], migrate=True)
    original = path.read_bytes()
    fresh = WorkspaceManager()
    assert row["id"] in fresh.tasks
    assert path.with_suffix(".json.pre-migration-backup").read_bytes() == original
    migrated = json.loads(path.read_text())
    assert "agent_runs" not in migrated
    assert "agent_run_id" not in migrated["tasks"][0]


def test_unknown_flat_legacy_task_is_not_swallowed(manager, workspace, monkeypatch):
    path = _wm._state.LEGACY_STATE_FILE
    path.write_text(
        json.dumps(
            {
                "workspaces": [workspace.model_dump(mode="json")],
                "tasks": [{**main_task(workspace.id), "unknown_future_field": None}],
                "sessions": [],
                "reports": [],
            }
        )
    )
    before = path.read_bytes()
    monkeypatch.setattr(manager, "_save_state", Mock(side_effect=forbidden))
    with pytest.raises(ValueError):
        manager._load_legacy_state()
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", SCHEDULE_UNKNOWN_FIELDS)
def test_unknown_schedule_field_fails_before_core_load(manager, workspace, monkeypatch, field):
    valid = main_schedule("tab_message", workspace.id)
    invalid = {**main_schedule("hub_task", workspace.id), field: None}
    path = write_schedules([valid, invalid])
    before = path.read_bytes()
    monkeypatch.setattr(WorkspaceManager, "_load_state", Mock(side_effect=forbidden))
    monkeypatch.setattr(WorkspaceManager, "_save_scheduled_tasks", Mock(side_effect=forbidden))
    monkeypatch.setattr(WorkspaceManager, "_atomic_write_text", Mock(side_effect=forbidden))
    with pytest.raises(ValidationError) as caught:
        WorkspaceManager()
    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())
    assert path.read_bytes() == before


def test_non_extra_invalid_schedule_still_skips(manager, workspace):
    valid = main_schedule("tab_message", workspace.id)
    invalid = {**main_schedule("hub_task", workspace.id), "interval_seconds": 0}
    path = write_schedules([invalid, valid])
    before = path.read_bytes()
    fresh = WorkspaceManager()
    assert set(fresh.scheduled_tasks) == {valid["id"]}
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "model,body",
    [
        (WorkspaceTaskCreate, {"title": "main", "prompt": "goal"}),
        (WorkspaceTaskUpdate, {"title": "changed"}),
        (ScheduledTaskCreate, {"name": "main", "kind": "hub_task", "run_at": STAMP}),
        (ScheduledTaskUpdate, {"name": "changed"}),
    ],
)
def test_request_models_reject_unknown_fields(model, body):
    with pytest.raises(ValidationError) as caught:
        model.model_validate({**body, "unknown_future_field": None})
    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


def api_app(manager, monkeypatch):
    workspaces_api = import_module("claude_hub.api.workspaces")
    schedules_api = import_module("claude_hub.api.scheduled_tasks")
    root_api = import_module("claude_hub.api")
    monkeypatch.setattr(workspaces_api, "workspace_manager", manager)
    monkeypatch.setattr(schedules_api, "workspace_manager", manager)
    app = FastAPI()
    app.include_router(root_api.api_router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="test-user", name="test", email="test@example.invalid"
    )
    return app


@pytest.mark.parametrize("field", ["source_work_id", "unknown_future_field"])
def test_task_api_rejects_unknown_fields_before_mutation(manager, workspace, monkeypatch, field):
    create = AsyncMock(side_effect=forbidden)
    update = AsyncMock(side_effect=forbidden)
    monkeypatch.setattr(manager, "register_task", create)
    monkeypatch.setattr(manager, "update_task", update)
    path = manager._workspace_state_file(workspace.id)
    before = path.read_bytes()
    with TestClient(api_app(manager, monkeypatch)) as client:
        response = client.post(
            f"/api/workspaces/{workspace.id}/tasks",
            json={
                "title": "main",
                "prompt": "ordinary task",
                field: None,
            },
        )
        assert response.status_code == 422
        response = client.patch(
            "/api/workspaces/tasks/unneeded-id",
            json={
                "title": "changed",
                field: None,
            },
        )
        assert response.status_code == 422
    create.assert_not_awaited()
    update.assert_not_awaited()
    assert path.read_bytes() == before
    assert not manager.tasks and not manager.sessions


@pytest.mark.parametrize("field", ["source_tab_id", "work_kind", "unknown_future_field"])
def test_schedule_api_rejects_unknown_fields_before_mutation(
    manager, workspace, monkeypatch, field
):
    create = Mock(side_effect=forbidden)
    update = Mock(side_effect=forbidden)
    monkeypatch.setattr(manager, "create_scheduled_task", create)
    monkeypatch.setattr(manager, "update_scheduled_task", update)
    path = write_schedules([])
    before = path.read_bytes()
    with TestClient(api_app(manager, monkeypatch)) as client:
        response = client.post(
            "/api/scheduled-tasks",
            json={
                "name": "ordinary",
                "kind": "hub_task",
                "run_at": STAMP,
                "workspace_id": workspace.id,
                "task_title": "task",
                "message": "goal",
                field: None,
            },
        )
        assert response.status_code == 422
        response = client.patch(
            "/api/scheduled-tasks/unneeded-id",
            json={
                "name": "changed",
                field: None,
            },
        )
        assert response.status_code == 422
    create.assert_not_called()
    update.assert_not_called()
    assert path.read_bytes() == before
    assert not manager.scheduled_tasks and not manager.sessions


def test_removed_work_routes_and_schema_have_no_compatibility_surface(
    manager, workspace, monkeypatch
):
    app = api_app(manager, monkeypatch)
    schema = app.openapi()
    assert not any("/api/tabs/{tab_id}/work" in path for path in schema["paths"])
    assert not any(name.startswith("ChatWork") for name in schema["components"]["schemas"])
    with TestClient(app) as client:
        for method, path in (
            ("GET", "/api/tabs/caller-tab/work"),
            ("POST", "/api/tabs/caller-tab/work"),
            ("GET", "/api/tabs/caller-tab/work/work-id"),
            ("PATCH", "/api/tabs/caller-tab/work/work-id"),
            ("POST", "/api/tabs/caller-tab/work/work-id/report"),
        ):
            assert client.request(method, path).status_code == 404
        response = client.get(f"/api/workspaces/{workspace.id}/task-capabilities")
        assert response.status_code == 200
        assert response.json()["supported_execution_controls"] == ["workspace", "initiator"]
        assert "legacy_chat_work_create" not in response.json()
    assert not manager.tasks and not manager.sessions


def ordinary_schedule(manager, workspace):
    task = ScheduledTask.model_validate(main_schedule("tab_message", workspace.id))
    manager.scheduled_tasks[task.id] = task
    manager._save_scheduled_tasks()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("control", ["disable", "delete"])
async def test_fire_waiter_rechecks_current_ordinary_schedule(
    manager, workspace, monkeypatch, manual, control
):
    stale = ordinary_schedule(manager, workspace)
    sent = AsyncMock(side_effect=forbidden)
    monkeypatch.setattr(manager, "_send_tab_message", sent)
    lock = manager._sched_fire_locks.setdefault(stale.id, asyncio.Lock())
    await lock.acquire()
    pending = asyncio.create_task(
        manager._fire_scheduled_task(
            stale,
            datetime.fromisoformat(STAMP) + timedelta(seconds=1),
            manual=manual,
        )
    )
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        if control == "delete":
            assert manager.delete_scheduled_task(stale.id)
        else:
            live = manager.update_scheduled_task(stale.id, ScheduledTaskUpdate(enabled=False))
            assert live is not stale and not live.enabled
    finally:
        lock.release()
    if manual:
        with pytest.raises(KeyError if control == "delete" else ValueError):
            await asyncio.wait_for(pending, 5)
    else:
        assert await asyncio.wait_for(pending, 5) is None
    sent.assert_not_awaited()
    assert stale.run_count == 0
    if control == "delete":
        assert stale.id not in manager.scheduled_tasks
    else:
        assert not manager.scheduled_tasks[stale.id].enabled
        assert manager.scheduled_tasks[stale.id].run_count == 0


@pytest.mark.asyncio
async def test_fire_waiter_uses_edited_ordinary_message(manager, workspace, monkeypatch):
    stale = ordinary_schedule(manager, workspace)
    sent = AsyncMock()
    monkeypatch.setattr(manager, "_send_tab_message", sent)
    lock = manager._sched_fire_locks.setdefault(stale.id, asyncio.Lock())
    await lock.acquire()
    pending = asyncio.create_task(
        manager._fire_scheduled_task(
            stale,
            datetime.fromisoformat(STAMP) + timedelta(seconds=1),
        )
    )
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        live = manager.update_scheduled_task(
            stale.id, ScheduledTaskUpdate(message="edited while waiting")
        )
    finally:
        lock.release()
    await asyncio.wait_for(pending, 5)
    sent.assert_awaited_once_with("caller-tab", "edited while waiting")
    assert live is manager.scheduled_tasks[stale.id]
    assert live.run_count == 1 and stale.run_count == 0
    assert live.last_status == "ok" and not live.enabled


@pytest.mark.asyncio
async def test_ordinary_schedule_save_failure_precedes_action(manager, workspace, monkeypatch):
    task = ordinary_schedule(manager, workspace)
    path = _wm._scheduling.SCHEDULED_TASKS_FILE
    before = path.read_bytes()
    sent = AsyncMock(side_effect=forbidden)
    monkeypatch.setattr(manager, "_send_tab_message", sent)
    monkeypatch.setattr(manager, "_save_scheduled_tasks", Mock(side_effect=OSError("save failed")))
    with pytest.raises(OSError, match="save failed"):
        await manager._fire_scheduled_task(task, datetime.fromisoformat(STAMP), manual=True)
    sent.assert_not_awaited()
    assert path.read_bytes() == before


def test_ordinary_report_fingerprint_keeps_main_contract(manager, workspace):
    payload = AgentReportCreate(
        state="completed",
        message="ordinary report",
        call_id="first-call",
        task_id="main-task",
        changed_files=["a.py"],
        validation="unit checked",
    )
    # Frozen ordinary content keys from main; identity and call_id are excluded.
    keys = """
        state message message_en message_zh task_id changed_files validation risks
        acceptance_check goal_packet evaluation_report review_profiles profile_results
        artifact_refs confidence requires_human_judgment review_decision review_reason
        risk_level acked_call_ids
    """.split()
    data = payload.model_dump(mode="json")
    expected = hashlib.sha256(
        json.dumps(
            {key: data.get(key) for key in keys},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert manager._compute_report_fingerprint(payload) == expected
    persisted = AgentReport(
        id="report-id",
        workspace_id=workspace.id,
        session_id="session-id",
        created_at=datetime.fromisoformat(STAMP),
        **payload.model_dump(),
    )
    assert manager._compute_report_fingerprint(persisted) == expected
    assert (
        manager._compute_report_fingerprint(payload.model_copy(update={"call_id": "another-call"}))
        == expected
    )
    assert (
        manager._compute_report_fingerprint(
            payload.model_copy(update={"message": "different content"})
        )
        != expected
    )
