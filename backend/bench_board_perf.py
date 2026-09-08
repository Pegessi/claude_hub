"""Benchmark: board read latency (AC6).

Measures warm single-board GET and 8-concurrent board GET against a realistic
workspace, to verify the pure-snapshot read path stays under the AC6 bounds.

Run:  uv run python bench_board_perf.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Optional

from httpx import ASGITransport, AsyncClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    User,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager

# Realistic workspace sizing (matches the scale of the prior diagnosis).
N_OPEN_TASKS = 30  # non-Done: always returned, never paginated
N_DONE_TASKS = 50  # Done: paginated (DEFAULT_BOARD_TASKS_LIMIT=15)
REPORTS_PER_TASK = 3
N_ITERATIONS = 20
N_CONCURRENT = 8


def _build_workspace() -> str:
    """Populate the in-memory manager with a realistic workspace."""
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    workspace_id = "bench-ws"
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager.workspaces[workspace_id] = Workspace(
        id=workspace_id,
        name="bench",
        path="/repo",
        default_branch="main",
        session_prefix="bench",
        created_at=now,
        updated_at=now,
    )

    def add_task(task_id: str, status: WorkspaceTaskStatus) -> None:
        workspace_manager.tasks[task_id] = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=f"Task {task_id}",
            prompt=f"do {task_id}",
            agent_type=AgentType.CLAUDE,
            status=status,
            created_at=now,
            updated_at=now,
        )
        for i in range(REPORTS_PER_TASK):
            workspace_manager.reports[f"{task_id}:{i}"] = AgentReport(
                id=f"{task_id}:{i}",
                workspace_id=workspace_id,
                task_id=task_id,
                session_id="s1",
                state=AgentReportState.WORKING,
                message=f"report {i} for {task_id}",
                created_at=now,
            )

    for i in range(N_OPEN_TASKS):
        add_task(f"open-{i}", WorkspaceTaskStatus.WORKING)
    for i in range(N_DONE_TASKS):
        add_task(f"done-{i}", WorkspaceTaskStatus.DONE)

    workspace_manager.sessions["s1"] = ManagedSession(
        id="s1",
        workspace_id=workspace_id,
        tab_id="tab1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="Orchestrator",
        workspace_path="/repo",
        tmux_session="claude-hub-bench",
        runtime_status=AgentRuntimeStatus.IDLE,
        created_at=now,
        updated_at=now,
    )

    # A reviewed task with a realistic report history (a few review cycles)
    # so the direct-task endpoint measurement reflects a real worst case.
    detail_task_id = "detail-task"
    workspace_manager.tasks[detail_task_id] = WorkspaceTask(
        id=detail_task_id,
        workspace_id=workspace_id,
        title="Reviewed task",
        prompt="do reviewed task",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.REVIEW,
        created_at=now,
        updated_at=now,
    )
    for i in range(15):
        workspace_manager.reports[f"{detail_task_id}:{i}"] = AgentReport(
            id=f"{detail_task_id}:{i}",
            workspace_id=workspace_id,
            task_id=detail_task_id,
            session_id="s1",
            state=AgentReportState.WORKING,
            message=f"report {i} " + "x" * 200,
            created_at=now,
        )
    return workspace_id


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


async def main() -> None:
    workspace_id = _build_workspace()

    async def fake_current_user() -> User:
        return User(open_id="bench", name="Bench", email="bench@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        url = f"/api/workspaces/{workspace_id}/board"

        # Warm up.
        warm = await client.get(url)
        assert warm.status_code == 200, warm.text

        # Single warm reads.
        single: list[float] = []
        for _ in range(N_ITERATIONS):
            started = time.monotonic()
            response = await client.get(url)
            assert response.status_code == 200
            single.append((time.monotonic() - started) * 1000)

        # 8-concurrent reads (measure the wall-clock of the whole batch).
        concurrent: list[float] = []
        for _ in range(N_ITERATIONS):
            started = time.monotonic()
            results = await asyncio.gather(*[client.get(url) for _ in range(N_CONCURRENT)])
            assert all(r.status_code == 200 for r in results)
            concurrent.append((time.monotonic() - started) * 1000)

        # Direct single-task endpoint (AC4): latency + payload size.
        detail_url = f"/api/workspaces/{workspace_id}/tasks/detail-task"
        detail = await client.get(detail_url)
        assert detail.status_code == 200, detail.text
        detail_latencies: list[float] = []
        for _ in range(N_ITERATIONS):
            started = time.monotonic()
            response = await client.get(detail_url)
            assert response.status_code == 200
            detail_latencies.append((time.monotonic() - started) * 1000)
        detail_payload_kb = len(detail.content) / 1024

    app.dependency_overrides.pop(get_current_user, None)

    print(
        f"Workspace: {N_OPEN_TASKS} open + {N_DONE_TASKS} done tasks, "
        f"{REPORTS_PER_TASK} reports/task"
    )
    print(
        f"Single board GET   median={statistics.median(single):.1f}ms  "
        f"p95={_p95(single):.1f}ms  (AC6 target <100ms; was 0.65-1.0s)"
    )
    print(
        f"{N_CONCURRENT}-concurrent     median={statistics.median(concurrent):.1f}ms  "
        f"p95={_p95(concurrent):.1f}ms  (AC6 target <500ms; was 4.23s)"
    )
    print(
        f"Direct task GET    median={statistics.median(detail_latencies):.1f}ms  "
        f"payload={detail_payload_kb:.1f}KB  (AC4 target <200ms / <50KB; was 1.43-1.56s / 2.48MB)"
    )


if __name__ == "__main__":
    asyncio.run(main())
