"""HTTP contract tests for native Chat scheduled tasks."""

from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from claude_hub.api import scheduled_tasks as scheduled_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskRunStatus,
    User,
)


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch):
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local", email="local@example.test"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_chat_schedule_create_run_and_list_runs(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now()
    task = ScheduledTask(
        id="schedule-1",
        name="Daily summary",
        kind="chat_turn",
        interval_seconds=3600,
        tab_id="chat-1",
        agent_type=AgentType.TRAEX,
        message="Summarize the latest work",
        created_at=now,
        updated_at=now,
    )
    run = ScheduledTaskRun(
        id="run-1",
        scheduled_task_id=task.id,
        tab_id="chat-1",
        client_turn_id="scheduled-run-1",
        message=task.message or "",
        status=ScheduledTaskRunStatus.WAITING,
        scheduled_for=now,
        queued_at=now,
        waiting_reason="waiting for the current Chat response",
    )
    created_bodies: list[object] = []

    def create(body: object) -> ScheduledTask:
        created_bodies.append(body)
        return task

    async def run_now(task_id: str) -> ScheduledTask:
        assert task_id == task.id
        task.last_run_id = run.id
        task.last_run_at = now
        task.last_status = run.status.value
        task.last_error = run.waiting_reason
        return task

    monkeypatch.setattr(scheduled_api.workspace_manager, "create_scheduled_task", create)
    monkeypatch.setattr(scheduled_api.workspace_manager, "run_scheduled_task", run_now)
    monkeypatch.setattr(
        scheduled_api.workspace_manager,
        "list_scheduled_task_runs",
        lambda task_id: [run] if task_id == task.id else (_ for _ in ()).throw(KeyError(task_id)),
    )

    created = await client.post(
        "/api/scheduled-tasks",
        json={
            "name": task.name,
            "kind": "chat_turn",
            "interval_seconds": 3600,
            "tab_id": "chat-1",
            "message": task.message,
        },
    )
    assert created.status_code == 201
    assert created.json()["kind"] == "chat_turn"
    assert created_bodies[0].tab_id == "chat-1"

    fired = await client.post(f"/api/scheduled-tasks/{task.id}/run")
    assert fired.status_code == 200
    assert fired.json()["last_run_id"] == run.id
    assert fired.json()["last_status"] == "waiting"

    runs = await client.get(f"/api/scheduled-tasks/{task.id}/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["waiting_reason"] == run.waiting_reason

    missing = await client.get("/api/scheduled-tasks/missing/runs")
    assert missing.status_code == 404
