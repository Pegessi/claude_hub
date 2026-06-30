import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Generator, Optional

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    RemoteProfile,
    TerminalAgentStatus,
    TerminalTab,
    User,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager

workspace_module = import_module("claude_hub.services.workspace_manager")
ORIGINAL_WRITE_TASK_RECORD = workspace_manager._write_task_record
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg=="
)


def write_iteration_task_record_fixture(
    state_root: Path,
    workspace_id: str,
    task_id: str,
    *,
    review_failed_count: int = 2,
    needs_input_count: int = 0,
) -> None:
    """Write a minimal task_records fixture so the lesson validator sees a Signal A task."""
    records_dir = state_root / workspace_id / "task_records"
    records_dir.mkdir(parents=True, exist_ok=True)
    states = (
        ["started", "working"]
        + ["review_failed"] * review_failed_count
        + ["needs_input"] * needs_input_count
        + ["completed"]
    )
    payload = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "task": {"id": task_id, "title": "fixture", "status": "done"},
        "session": {},
        "reports": [{"state": state} for state in states],
        "timeline": [],
        "artifacts": {"changed_files": [], "validation": [], "risks": []},
        "final_summary": "fixture",
    }
    (records_dir / f"2026-05-15T00-00-00-{task_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def pass_task_review(client: TestClient, task_id: str, message: str = "Review passed"):
    reviewer_id = workspace_manager.tasks[task_id].review_session_id
    assert reviewer_id is not None
    return client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task_id,
            "state": "review_passed",
            "message": message,
        },
    )


def stub_workspace_terminal(
    monkeypatch: MonkeyPatch,
    repo: Path,
    *,
    tab_id: str,
    port: int,
    sent_messages: list[tuple[str, str]] | None = None,
) -> None:
    created_count = 0

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        nonlocal created_count
        created_count += 1
        return TerminalTab(
            id=f"{tab_id}-{created_count}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=port,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        if sent_messages is not None:
            sent_messages.append((tmux_session, message))

    async def fake_update_tab(
        tab_id_to_update: str, name: Optional[str] = None, **_: object
    ) -> TerminalTab:
        return TerminalTab(
            id=tab_id_to_update,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=port,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )


@pytest.fixture(autouse=True)
def isolated_workspace_manager(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_write_task_record", lambda _task: None)

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()


def test_workspace_task_flow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    response = client.post(
        "/api/workspaces",
        json={
            "name": "Test Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "test",
        },
    )

    assert response.status_code == 201
    workspace = response.json()
    assert workspace["name"] == "Test Repo"

    response = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Implement thing",
            "prompt": "Make a focused change",
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "todo"
    assert task["agent_type"] == "codex"
    assert task["execution_complexity"] == "auto"

    response = client.get(f"/api/workspaces/{workspace['id']}/board")

    assert response.status_code == 200
    board = response.json()
    assert board["workspace"]["id"] == workspace["id"]
    assert [item["id"] for item in board["tasks"]] == [task["id"]]
    assert board["sessions"] == []


def test_board_etag_returns_304_when_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "ETag Repo", "path": str(repo), "session_prefix": "etag"},
    ).json()

    first = client.get(f"/api/workspaces/{workspace['id']}/board")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag

    # A matching If-None-Match short-circuits to a bodyless 304.
    cached = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        headers={"If-None-Match": etag},
    )
    assert cached.status_code == 304
    assert cached.headers.get("etag") == etag
    assert cached.content == b""

    # A mismatching tag still returns the full board with a fresh ETag.
    stale = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        headers={"If-None-Match": '"deadbeef"'},
    )
    assert stale.status_code == 200
    assert stale.headers.get("etag") == etag

    # Content change (new task) rotates the ETag so the client re-fetches.
    client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "New task", "prompt": "Do a thing"},
    )
    changed = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        headers={"If-None-Match": etag},
    )
    assert changed.status_code == 200
    assert changed.headers.get("etag") != etag


def test_task_goal_packet_create_update_and_legacy_normalization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Goal Repo", "path": str(repo), "session_prefix": "goal"},
    ).json()
    goal_packet = {
        "objective": "Implement goal packet persistence.",
        "acceptance_criteria": ["Task stores packet"],
        "validation_plan": ["pytest tests/test_workspaces.py"],
        "assumptions": ["Use optional task metadata"],
        "out_of_scope": ["Goal editor"],
        "handoff_requirements": ["Summarize changed files"],
    }

    create_response = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Goal task",
            "prompt": "Add Goal Packet support",
            "goal_packet": goal_packet,
        },
    )
    assert create_response.status_code == 201
    task = create_response.json()
    assert task["goal_packet"]["objective"] == goal_packet["objective"]
    assert task["goal_packet"]["status"] == "draft"

    update_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={
            "goal_packet": {
                **goal_packet,
                "objective": "Updated objective.",
                "acceptance_criteria": ["Updated criterion"],
            }
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["goal_packet"]["objective"] == "Updated objective."
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.TODO

    normalized = workspace_manager._normalize_task_item(
        {
            **workspace_manager.tasks[task["id"]].model_dump(mode="json"),
            "goal_packet": {"objective": "", "acceptance_criteria": "bad"},
        }
    )
    assert normalized["goal_packet"] is None


def test_autonomous_task_create_defaults_and_legacy_normalization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Auto Repo", "path": str(repo), "session_prefix": "auto"},
    ).json()

    response = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Auto task",
            "prompt": "Iterate until evaluation passes",
            "task_mode": "autonomous",
            "autonomy_policy": {"max_iterations": 2, "require_artifact_review": True},
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["task_mode"] == "autonomous"
    assert task["autonomy_policy"]["max_iterations"] == 2
    assert task["autonomous_run"]["phase"] == "intake"
    assert task["autonomous_run"]["max_iterations"] == 2

    normalized = workspace_manager._normalize_task_item(
        {
            "id": "legacy-task",
            "workspace_id": workspace["id"],
            "title": "Legacy",
            "prompt": "Old state",
            "agent_type": "codex",
            "status": "todo",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
    )
    assert normalized["task_mode"] == "reviewed"
    assert normalized["execution_complexity"] == "auto"
    assert normalized["autonomy_policy"] is None
    assert normalized["autonomous_run"] is None


def test_task_origin_defaults_human_and_resident_roundtrip(tmp_path: Path) -> None:
    """origin defaults to human for frontend-created tasks, accepts an explicit
    resident value, and legacy tasks without the field normalize to human."""
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Origin Repo", "path": str(repo), "session_prefix": "orig"},
    ).json()

    # Default (frontend / human) create — no origin in payload.
    human = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Human task", "prompt": "Do a thing"},
    )
    assert human.status_code == 201
    assert human.json()["origin"] == "human"

    # Explicit resident-created task (what the resident agent posts).
    resident = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Resident task", "prompt": "Proposed by resident", "origin": "resident"},
    )
    assert resident.status_code == 201
    assert resident.json()["origin"] == "resident"

    # Legacy persisted task without the field normalizes to human.
    normalized = workspace_manager._normalize_task_item(
        {
            "id": "legacy-origin-task",
            "workspace_id": workspace["id"],
            "title": "Legacy",
            "prompt": "Old state",
            "agent_type": "codex",
            "status": "todo",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
    )
    assert normalized["origin"] == "human"


def test_task_execution_complexity_prompts_and_legacy_normalization(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="complexity-tab",
        port=12542,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Complexity Repo", "path": str(repo), "session_prefix": "cx"},
    ).json()

    complex_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Complex task",
            "prompt": "Implement a broad feature with tests",
            "execution_complexity": "complex",
        },
    ).json()
    assert complex_task["execution_complexity"] == "complex"

    started = client.post(f"/api/workspaces/tasks/{complex_task['id']}/start", json={}).json()
    assignment_prompt = sent_messages[-1][1]
    assert "Task execution complexity: complex" in assignment_prompt
    assert "Selected complexity: complex" in assignment_prompt
    assert "Act as the task orchestrator" in assignment_prompt
    sent_messages.clear()

    report_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": complex_task["id"],
            "state": "completed",
            "message": "Done",
            "goal_packet": {
                "objective": "Implement a broad feature.",
                "acceptance_criteria": ["Feature works"],
                "validation_plan": ["pytest"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": [],
            },
            "acceptance_check": [
                {
                    "criterion": "Feature works",
                    "status": "passed",
                    "evidence": "Focused checks passed",
                }
            ],
            "changed_files": ["backend/claude_hub/models/schemas.py"],
            "review_decision": "request",
            "review_reason": "Verify complex orchestration prompt",
        },
    )
    assert report_response.status_code == 201
    reviewer_prompt = sent_messages[-1][1]
    assert "Task execution complexity: complex" in reviewer_prompt
    assert "Execution complexity review context" in reviewer_prompt
    assert "lack of decomposition" in reviewer_prompt

    auto_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Auto task", "prompt": "Judge the size"},
    ).json()
    assert auto_task["execution_complexity"] == "auto"

    normalized = workspace_manager._normalize_task_item(
        {
            **workspace_manager.tasks[auto_task["id"]].model_dump(mode="json"),
            "execution_complexity": "overspecified",
        }
    )
    assert normalized["execution_complexity"] == "auto"


def test_autonomous_task_passes_after_evaluator_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="auto-pass-tab",
        port=12531,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Auto Pass", "path": str(repo), "session_prefix": "autop"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Auto pass",
            "prompt": "Complete and evaluate",
            "task_mode": "autonomous",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    assert "Autonomous Mode V1 is enabled" in sent_messages[-1][1]

    goal_packet = {
        "objective": "Complete autonomous task.",
        "acceptance_criteria": ["Evaluator passes"],
        "validation_plan": ["Report inspection"],
        "assumptions": [],
        "out_of_scope": [],
        "handoff_requirements": [],
    }
    report_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "goal_packet": goal_packet,
            "acceptance_check": [
                {
                    "criterion": "Evaluator passes",
                    "status": "passed",
                    "evidence": "Ready for evaluator",
                }
            ],
            "review_decision": "skip",
        },
    )

    assert report_response.status_code == 201
    evaluating_task = workspace_manager.tasks[task["id"]]
    assert evaluating_task.review_session_id is not None
    assert evaluating_task.autonomous_run is not None
    assert evaluating_task.autonomous_run.phase.value == "evaluating"
    assert "Autonomous evaluation context" in sent_messages[-1][1]

    response = client.post(
        f"/api/workspaces/sessions/{evaluating_task.review_session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Evaluation passed",
            "evaluation_report": {
                "id": "eval-pass",
                "decision": "pass",
                "overall_score": 0.95,
                "iteration": 1,
            },
        },
    )

    assert response.status_code == 201
    passed_task = workspace_manager.tasks[task["id"]]
    assert passed_task.status == WorkspaceTaskStatus.REVIEW
    assert passed_task.human_acceptance_requested_at is not None
    assert passed_task.autonomous_run is not None
    assert passed_task.autonomous_run.phase.value == "passed"
    assert passed_task.autonomous_run.current_score == 0.95
    assert passed_task.autonomous_run.evaluation_reports[-1].decision.value == "pass"


def test_autonomous_passed_ignores_stale_worker_reports(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: once an autonomous run reaches PASSED for a review cycle,
    late/stale worker ``completed``/``working`` reports for that same cycle
    must NOT reopen review or clear ``human_acceptance_requested_at``.

    Repro of the stuck-in-review/no-Done bug (task 33f033b3): an autonomous
    worker kept re-emitting ``completed`` after the evaluator's
    ``review_passed``. Each stale report flipped ``autonomous_run.phase``
    PASSED → EVALUATING and eventually reopened a fresh review round,
    clearing ``human_acceptance_requested_at`` — so the card was stranded in
    the REVIEW column with no usable Done button.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="auto-stale-tab",
        port=12533,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Auto Stale", "path": str(repo), "session_prefix": "autos"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Auto stale",
            "prompt": "Complete and evaluate",
            "task_mode": "autonomous",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    goal_packet = {
        "objective": "Complete autonomous task.",
        "acceptance_criteria": ["Evaluator passes"],
        "validation_plan": ["Report inspection"],
        "assumptions": [],
        "out_of_scope": [],
        "handoff_requirements": [],
    }
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "goal_packet": goal_packet,
            "acceptance_check": [
                {
                    "criterion": "Evaluator passes",
                    "status": "passed",
                    "evidence": "Ready for evaluator",
                }
            ],
            "review_decision": "skip",
        },
    )
    evaluating_task = workspace_manager.tasks[task["id"]]
    assert evaluating_task.autonomous_run is not None
    assert evaluating_task.autonomous_run.phase.value == "evaluating"

    # Evaluator passes → run reaches PASSED, human acceptance requested.
    client.post(
        f"/api/workspaces/sessions/{evaluating_task.review_session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Evaluation passed",
            "evaluation_report": {
                "id": "eval-pass",
                "decision": "pass",
                "overall_score": 0.95,
                "iteration": 1,
            },
        },
    )
    passed_task = workspace_manager.tasks[task["id"]]
    assert passed_task.status == WorkspaceTaskStatus.REVIEW
    assert passed_task.human_acceptance_requested_at is not None
    assert passed_task.autonomous_run is not None
    assert passed_task.autonomous_run.phase.value == "passed"
    accepted_at = passed_task.human_acceptance_requested_at
    passed_cycle = passed_task.review_cycle
    passed_reviewed_cycle = passed_task.reviewed_cycle

    # The worker re-emits stale reports for the SAME cycle, several times.
    for note in ("still working", "completed again", "more progress"):
        state = "working" if note == "more progress" else "completed"
        resp = client.post(
            f"/api/workspaces/sessions/{started['session_id']}/reports",
            json={
                "task_id": task["id"],
                "state": state,
                "message": note,
                "acceptance_check": [
                    {
                        "criterion": "Evaluator passes",
                        "status": "passed",
                        "evidence": "echo",
                    }
                ],
                "review_decision": "skip",
            },
        )
        assert resp.status_code == 201

    after = workspace_manager.tasks[task["id"]]
    # Stale post-PASS reports leave the task acceptance-able and unchanged.
    assert after.status == WorkspaceTaskStatus.REVIEW
    assert after.human_acceptance_requested_at == accepted_at
    assert after.autonomous_run is not None
    assert after.autonomous_run.phase.value == "passed"
    # No spurious new review round was opened.
    assert after.review_cycle == passed_cycle
    assert after.reviewed_cycle == passed_reviewed_cycle


def test_autonomous_task_exhausts_iteration_budget(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="auto-exhaust-tab",
        port=12532,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Auto Exhaust", "path": str(repo), "session_prefix": "autox"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Auto exhaust",
            "prompt": "Use one iteration",
            "task_mode": "autonomous",
            "autonomy_policy": {"max_iterations": 1},
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Ready for evaluation",
        },
    )
    review_session_id = workspace_manager.tasks[task["id"]].review_session_id
    assert review_session_id is not None
    sent_before_failure = len(sent_messages)

    response = client.post(
        f"/api/workspaces/sessions/{review_session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Blocking issue remains",
        },
    )

    assert response.status_code == 201
    exhausted_task = workspace_manager.tasks[task["id"]]
    assert exhausted_task.status == WorkspaceTaskStatus.REVIEW
    assert exhausted_task.autonomous_run is not None
    assert exhausted_task.autonomous_run.phase.value == "exhausted"
    assert exhausted_task.autonomous_run.exhausted_at is not None
    assert exhausted_task.autonomous_run.evaluation_reports[-1].decision.value == "revise"
    assert len(sent_messages) == sent_before_failure


def test_direct_task_completed_does_not_auto_request_ai_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="direct-complete-tab",
        port=12533,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Direct Complete", "path": str(repo), "session_prefix": "directc"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Direct complete",
            "prompt": "Complete without AI review",
            "task_mode": "direct",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "changed_files": ["direct.txt"],
        },
    )

    assert response.status_code == 201
    direct_task = workspace_manager.tasks[task["id"]]
    assert direct_task.status == WorkspaceTaskStatus.REVIEW
    assert direct_task.review_session_id is None
    assert direct_task.review_requested_at is None
    assert direct_task.review_skipped_at is not None
    assert direct_task.human_acceptance_requested_at is not None
    assert sent_messages == []


@pytest.mark.parametrize("report_state", ["blocked", "needs_input"])
def test_direct_task_waiting_reports_do_not_become_accept_ready(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    report_state: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id=f"direct-{report_state}-tab",
        port=12535,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Direct Waiting", "path": str(repo), "session_prefix": "directw"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": f"Direct {report_state}",
            "prompt": "Report unfinished work without AI review",
            "task_mode": "direct",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()
    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": report_state,
            "message": "Waiting on input",
        },
    )

    assert response.status_code == 201
    direct_task = workspace_manager.tasks[task["id"]]
    direct_session = workspace_manager.sessions[started["session_id"]]
    assert direct_task.status == WorkspaceTaskStatus.WORKING
    assert direct_task.review_session_id is None
    assert direct_task.review_requested_at is None
    assert direct_task.review_skipped_at is None
    assert direct_task.human_acceptance_requested_at is None
    assert direct_session.status == ManagedSessionStatus.NEEDS_INPUT
    assert direct_session.runtime_status == AgentRuntimeStatus.ATTENTION
    assert sent_messages == []


def test_direct_task_explicit_review_request_still_creates_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="direct-review-tab",
        port=12534,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Direct Review", "path": str(repo), "session_prefix": "directr"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Direct review",
            "prompt": "Request review explicitly",
            "task_mode": "direct",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()
    write_iteration_task_record_fixture(state_root, workspace["id"], task["id"])
    lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={
            "id": "explicit-review-handoff",
            "summary": "Explicit review requests need handoff evidence.",
            "applies_when": ["explicit review", "review request"],
            "do": "Check changed files, validation, risks, and acceptance evidence.",
            "avoid": "Do not pass review based only on the completion message.",
            "tags": ["review", "handoff"],
            "scope": "workspace",
            "evidence_task_ids": [task["id"]],
            "confidence": 0.8,
        },
    )
    assert lesson_response.status_code == 201

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done, please review",
            "review_decision": "request",
            "review_reason": "Explicit Direct-mode review request",
        },
    )

    assert response.status_code == 201
    direct_task = workspace_manager.tasks[task["id"]]
    assert direct_task.status == WorkspaceTaskStatus.REVIEW
    assert direct_task.review_session_id is not None
    assert direct_task.review_requested_at is not None
    assert direct_task.feedback_lesson_ids == []
    assert "Review workspace task." in sent_messages[-1][1]
    assert "explicit-review-handoff" in sent_messages[-1][1]
    assert "Workspace lessons index" in sent_messages[-1][1]
    assert (
        "Check changed files, validation, risks, and acceptance evidence."
        not in sent_messages[-1][1]
    )
    task_reports = [
        report
        for report in workspace_manager.reports_for_workspace(workspace["id"])
        if report.task_id == task["id"] and report.risk_level == "system_audit"
    ]
    assert task_reports == []


def test_agent_report_stores_goal_packet_and_acceptance_check(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "REVIEW.md").write_text(
        "Always verify task-specific review profiles.", encoding="utf-8"
    )
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="goal-report-tab",
        port=12530,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Goal Report", "path": str(repo), "session_prefix": "goalr"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Goal report", "prompt": "Report a Goal Packet"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    assert "Goal Packet report example" in sent_messages[-1][1]
    assert '"goal_packet"' in sent_messages[-1][1]
    sent_messages.clear()

    goal_packet = {
        "objective": "Report structured task intent.",
        "acceptance_criteria": ["Packet is stored"],
        "validation_plan": ["Inspect task response"],
        "assumptions": [],
        "out_of_scope": [],
        "handoff_requirements": ["Include acceptance evidence"],
    }
    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "goal_packet": goal_packet,
            "acceptance_check": [
                {
                    "criterion": "Packet is stored",
                    "status": "passed",
                    "evidence": "Task model has goal_packet",
                }
            ],
            "changed_files": ["backend/claude_hub/models/schemas.py"],
            "validation": "pytest tests/test_workspaces.py",
            "risks": "none",
            "review_decision": "request",
            "review_reason": "Feature change needs review",
        },
    )

    assert response.status_code == 201
    report = response.json()
    assert report["acceptance_check"][0]["status"] == "passed"
    stored_goal_packet = workspace_manager.tasks[task["id"]].goal_packet
    assert stored_goal_packet is not None
    assert stored_goal_packet.objective == goal_packet["objective"]

    reviewer_prompt = sent_messages[-1][1]
    assert "Stored Goal Packet JSON" in reviewer_prompt
    assert "Report structured task intent." in reviewer_prompt
    assert "acceptance_check" in reviewer_prompt
    assert "Goal fidelity" in reviewer_prompt
    assert "Enabled review profiles JSON" in reviewer_prompt
    assert '"code"' in reviewer_prompt
    assert "Repository review guidance" in reviewer_prompt
    assert "Always verify task-specific review profiles." in reviewer_prompt

    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    review_response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Profile-aware review passed",
            "review_profiles": ["general", "code"],
            "profile_results": [
                {
                    "profile": "general",
                    "status": "passed",
                    "evidence": "Goal Packet and reports reviewed.",
                },
                {
                    "profile": "code",
                    "status": "passed",
                    "evidence": "Changed schema path reviewed.",
                },
            ],
            "artifact_refs": ["backend/claude_hub/models/schemas.py"],
            "confidence": 0.9,
        },
    )
    assert review_response.status_code == 201
    review_report = review_response.json()
    assert review_report["profile_results"][0]["profile"] == "general"
    assert review_report["artifact_refs"] == ["backend/claude_hub/models/schemas.py"]
    assert review_report["confidence"] == 0.9


def test_working_goal_packet_triggers_approval_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="goal-gate-tab",
        port=12531,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Goal Gate", "path": str(repo), "session_prefix": "ggate"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Plan first", "prompt": "Implement after plan approval"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Goal Packet created; waiting for approval.",
            "goal_packet": {
                "objective": "Implement after plan approval.",
                "acceptance_criteria": ["Plan is approved before coding"],
                "validation_plan": ["Reviewer checks the packet"],
                "assumptions": ["Reviewed mode requires a packet gate"],
                "out_of_scope": ["Implementation before approval"],
                "handoff_requirements": ["Report approval status"],
            },
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.goal_packet is not None
    assert updated.goal_packet.status.value == "pending_review"
    assert updated.review_session_id is not None
    assert updated.human_acceptance_requested_at is None
    reviewer_prompt = sent_messages[-1][1]
    assert "Goal Packet approval review" in reviewer_prompt
    assert "Do not judge implementation completeness" in reviewer_prompt
    assert "review_passed means the implementation agent may begin development" in reviewer_prompt


def test_goal_packet_review_pass_resumes_original_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="goal-pass-tab",
        port=12532,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Goal Pass", "path": str(repo), "session_prefix": "gpass"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Approve packet", "prompt": "Implement after approval"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Goal Packet created; waiting for approval.",
            "goal_packet": {
                "objective": "Implement after approval.",
                "acceptance_criteria": ["Approval happens first"],
                "validation_plan": ["Reviewer approval"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": [],
            },
        },
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Goal Packet preserves the task and has checkable criteria.",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert updated.goal_packet is not None
    assert updated.goal_packet.status.value == "approved"
    assert updated.human_acceptance_requested_at is None
    assert workspace_manager.sessions[started["session_id"]].status == ManagedSessionStatus.WORKING
    assert "Goal Packet approved" in sent_messages[-1][1]
    assert "Begin implementation" in sent_messages[-1][1]

    sent_messages.clear()
    final_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Implementation complete.",
            "acceptance_check": [
                {
                    "criterion": "Approval happens first",
                    "status": "passed",
                    "evidence": "Goal Packet review passed before this report.",
                }
            ],
            "changed_files": ["backend/claude_hub/services/workspace_manager.py"],
            "validation": "pytest backend/tests/test_workspaces.py",
            "risks": "none",
            "review_decision": "request",
            "review_reason": "Implementation change needs normal review.",
        },
    )

    assert final_response.status_code == 201
    final_task = workspace_manager.tasks[task["id"]]
    assert final_task.status == WorkspaceTaskStatus.REVIEW
    assert final_task.goal_packet is not None
    assert final_task.goal_packet.status.value == "approved"
    assert "Review workspace task." in sent_messages[-1][1]
    assert "Goal Packet approval review" not in sent_messages[-1][1]


def test_goal_packet_review_failed_returns_revision_to_original_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="goal-fail-tab",
        port=12533,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Goal Fail", "path": str(repo), "session_prefix": "gfail"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Revise packet", "prompt": "Do the full task, not just docs"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Goal Packet created; waiting for approval.",
            "goal_packet": {
                "objective": "Only write docs.",
                "acceptance_criteria": ["Docs exist"],
                "validation_plan": ["Read docs"],
                "assumptions": [],
                "out_of_scope": ["Code changes"],
                "handoff_requirements": [],
            },
        },
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Required fixes: preserve the code-change scope.",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert updated.goal_packet is not None
    assert updated.goal_packet.status.value == "rejected"
    assert "Goal Packet reviewer requested changes" in sent_messages[-1][1]
    assert "Do not start implementation" in sent_messages[-1][1]


def test_implementation_review_not_misrouted_as_goal_packet_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression test: implementation-phase review must not be misrouted
    to the goal-packet review handler.

    Bug: after goal packet approval, the packet stays in APPROVED status
    and review_completed_at gets rewritten by the implementation review's
    own fast-path. The old `is_goal_packet_review` condition matched both
    (APPROVED status + review_session_id + review_completed_at >= report),
    so implementation review_passed was incorrectly handled as a goal
    packet approval, triggering continue_task and looping the task back
    into implementation + review cycles.

    Fix: also require goal_packet.updated_at >= report.created_at, which
    is only true when the *packet itself* was touched by this review
    cycle (goal packet reviews only).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="impl-not-goal-tab",
        port=12700,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "ImplNotGoal", "path": str(repo), "session_prefix": "implnotgoal"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Impl review stays review", "prompt": "Build it and review it"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    # Step 1: agent submits goal packet → goal packet approval review
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Goal Packet created; waiting for approval.",
            "goal_packet": {
                "objective": "Build the feature.",
                "acceptance_criteria": ["Feature works"],
                "validation_plan": ["Run tests"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": [],
            },
        },
    )
    goal_reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert goal_reviewer_id is not None
    sent_messages.clear()

    # Step 2: goal packet reviewer approves → task resumes in WORKING
    client.post(
        f"/api/workspaces/sessions/{goal_reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Goal Packet looks good.",
        },
    )
    after_goal_pass = workspace_manager.tasks[task["id"]]
    assert after_goal_pass.status == WorkspaceTaskStatus.WORKING
    assert after_goal_pass.goal_packet is not None
    assert after_goal_pass.goal_packet.status.value == "approved"
    assert after_goal_pass.session_id == started["session_id"]
    sent_messages.clear()

    # Step 3: agent completes implementation → implementation review dispatched
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Implementation complete.",
            "acceptance_check": [
                {
                    "criterion": "Feature works",
                    "status": "passed",
                    "evidence": "Tests pass.",
                }
            ],
            "changed_files": ["src/feature.py"],
            "validation": "pytest",
            "risks": "none",
            "review_decision": "request",
            "review_reason": "Implementation needs review.",
        },
    )
    impl_reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert impl_reviewer_id is not None
    after_impl_request = workspace_manager.tasks[task["id"]]
    assert after_impl_request.status == WorkspaceTaskStatus.REVIEW
    assert after_impl_request.goal_packet is not None
    assert after_impl_request.goal_packet.status.value == "approved"
    # The goal packet's updated_at should be from the goal review, not
    # the implementation review request.
    goal_updated_at_before = after_impl_request.goal_packet.updated_at
    sent_messages.clear()

    # Step 4: implementation reviewer passes.  This is the bug point:
    # the old code would misroute this as a goal-packet review (because
    # packet is APPROVED, review_session_id matches, review_completed_at
    # is set by the fast-path and >= report.created_at), call
    # continue_task, and push the task back to WORKING.
    impl_review_response = client.post(
        f"/api/workspaces/sessions/{impl_reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Implementation looks good.",
        },
    )
    assert impl_review_response.status_code == 201

    final_task = workspace_manager.tasks[task["id"]]
    # Key assertions for the bug fix:
    assert final_task.status == WorkspaceTaskStatus.REVIEW
    assert final_task.human_acceptance_requested_at is not None
    assert final_task.goal_packet is not None
    assert final_task.goal_packet.status.value == "approved"
    # Goal packet updated_at must NOT have changed — implementation
    # reviews do not touch the goal packet.
    assert final_task.goal_packet.updated_at == goal_updated_at_before
    # The last sent message should NOT be "Goal Packet approved" feedback
    # (which is what the bug would have produced via continue_task).
    if sent_messages:
        last_msg = sent_messages[-1][1]
        assert "Goal Packet approved" not in last_msg
        assert "Begin implementation" not in last_msg


def test_duplicate_goal_packet_review_does_not_strand_task_in_working(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression test for the reported incident: a reviewed task that posted
    ``completed`` stayed stuck in the Working column and never entered Review.

    Reproduces the exact production sequence:

      1. Agent posts a Goal Packet → goal-packet review dispatched.
      2. Goal-packet reviewer ``review_passed`` → packet APPROVED,
         ``continue_task`` reopens the task to WORKING and clears
         ``review_requested_at`` / ``review_completed_at``.
      3. A SECOND (duplicate / stale) goal-packet ``review_passed`` arrives
         from the same reviewer while the agent is still implementing. With no
         review in flight this must be IGNORED. Previously it was misrouted as
         an implementation-phase verdict, writing a phantom
         ``review_completed_at`` / ``reviewed_at`` that the monitor reopen
         heuristic and late-report suppression turned into a permanently-stuck
         WORKING task.
      4. Agent posts ``completed`` → a genuine implementation review must be
         dispatched and the task must end in REVIEW (not stuck in WORKING).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="dup-goal-review-tab",
        port=12701,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "DupGoalReview", "path": str(repo), "session_prefix": "dupgoal"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Stale dup review", "prompt": "Build it and review it"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    # Step 1: agent submits goal packet → goal-packet approval review.
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Goal Packet created; waiting for approval.",
            "goal_packet": {
                "objective": "Build the feature.",
                "acceptance_criteria": ["Feature works"],
                "validation_plan": ["Run tests"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": [],
            },
        },
    )
    goal_reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert goal_reviewer_id is not None

    # Step 2: goal-packet reviewer approves → task resumes in WORKING and the
    # goal-review verdict fields are cleared by continue_task.
    client.post(
        f"/api/workspaces/sessions/{goal_reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Goal Packet looks good.",
        },
    )
    after_goal_pass = workspace_manager.tasks[task["id"]]
    assert after_goal_pass.status == WorkspaceTaskStatus.WORKING
    assert after_goal_pass.goal_packet is not None
    assert after_goal_pass.goal_packet.status.value == "approved"
    assert after_goal_pass.review_requested_at is None
    assert after_goal_pass.review_completed_at is None
    sent_messages.clear()

    # Step 3: a DUPLICATE / stale goal-packet review_passed arrives from the
    # same reviewer while the agent is still implementing. No review is in
    # flight, so it must be ignored — it must NOT seed a phantom verdict.
    dup_response = client.post(
        f"/api/workspaces/sessions/{goal_reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Goal Packet looks good (duplicate).",
        },
    )
    assert dup_response.status_code == 201
    after_dup = workspace_manager.tasks[task["id"]]
    assert after_dup.status == WorkspaceTaskStatus.WORKING
    assert after_dup.review_completed_at is None
    assert after_dup.reviewed_at is None
    assert after_dup.human_acceptance_requested_at is None

    # Step 4: agent completes implementation → a genuine implementation review
    # must be dispatched and the task must reach REVIEW (not stay WORKING).
    completed_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Implementation complete.",
            "acceptance_check": [
                {
                    "criterion": "Feature works",
                    "status": "passed",
                    "evidence": "Tests pass.",
                }
            ],
            "changed_files": ["src/feature.py"],
            "validation": "pytest",
            "risks": "none",
            "review_decision": "request",
            "review_reason": "Implementation needs review.",
        },
    )
    assert completed_response.status_code == 201

    final_task = workspace_manager.tasks[task["id"]]
    assert final_task.status == WorkspaceTaskStatus.REVIEW
    assert final_task.review_session_id is not None
    assert final_task.review_requested_at is not None
    assert final_task.review_completed_at is None


def test_update_workspace_changes_path_and_remote_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    new_repo = tmp_path / "new-repo"
    new_repo.mkdir()

    client = TestClient(app)
    create_response = client.post(
        "/api/workspaces",
        json={
            "name": "Editable",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "edit",
        },
    )
    assert create_response.status_code == 201
    workspace_id = create_response.json()["id"]

    update_response = client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"path": str(new_repo), "remote_cwd": "~/projects/foo"},
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["path"] == str(new_repo.resolve())
    assert updated["remote_cwd"] == "~/projects/foo"

    missing_response = client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"path": str(tmp_path / "does-not-exist")},
    )
    assert missing_response.status_code == 400


def test_create_task_persists_pasted_image_attachment(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Image Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "image",
        },
    )

    response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Use screenshot",
            "prompt": "Inspect the pasted screenshot",
            "attachments": [
                {
                    "filename": "screen shot.png",
                    "mime_type": "image/png",
                    "data_url": PNG_DATA_URL,
                }
            ],
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["attachments"][0]["filename"] == "screen-shot.png"
    attachment_path = Path(task["attachments"][0]["path"])
    assert attachment_path.exists()
    assert attachment_path.read_bytes().startswith(b"\x89PNG")


async def test_preview_report_markdown_artifact_is_scoped_to_report(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "report.md").write_text("# Research\n\nFindings", encoding="utf-8")
    (repo / "changed.md").write_text("# Changed\n\nNotes", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Secret", encoding="utf-8")
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")

    stub_workspace_terminal(monkeypatch, repo, tab_id="report-tab", port=18190)
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Report Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "report",
        },
    ).json()
    session = await workspace_manager.ensure_workspace_agent(
        workspace["id"],
        workspace_module.EnsureWorkspaceAgentRequest(agent_type="codex", reuse_existing=False),
    )
    report = client.post(
        f"/api/workspaces/sessions/{session.id}/reports",
        json={
            "state": "completed",
            "message": "Research report ready",
            "changed_files": [str(repo / "changed.md")],
            "artifact_refs": ["report.md", str(tmp_path / "outside.md")],
        },
    ).json()

    workspace_manager.snapshot_path(workspace["id"]).parent.mkdir(parents=True, exist_ok=True)
    workspace_manager.snapshot_path(workspace["id"]).write_text(
        "# Claude Hub Workspace State\n",
        encoding="utf-8",
    )

    board_response = client.get(f"/api/workspaces/{workspace['id']}/board")
    assert board_response.status_code == 200
    markdown_documents = board_response.json()["markdown_documents"]
    assert any(
        document["source"] == "artifact" and document["path"] == "report.md"
        for document in markdown_documents
    )
    assert any(
        document["source"] == "changed_file" and document["path"] == str(repo / "changed.md")
        for document in markdown_documents
    )
    assert any(document["source"] == "snapshot" for document in markdown_documents)

    response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": "report.md", "report_id": report["id"]},
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["filename"] == "report.md"
    assert preview["content"] == "# Research\n\nFindings"
    assert preview["truncated"] is False

    changed_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": str(repo / "changed.md"), "report_id": report["id"]},
    )
    assert changed_response.status_code == 200
    assert changed_response.json()["content"] == "# Changed\n\nNotes"

    snapshot_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": str(workspace_manager.snapshot_path(workspace["id"]))},
    )
    assert snapshot_response.status_code == 200
    assert "# Claude Hub Workspace State" in snapshot_response.json()["content"]

    outside_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": str(tmp_path / "outside.md"), "report_id": report["id"]},
    )
    assert outside_response.status_code == 404

    unknown_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": "missing.md", "report_id": report["id"]},
    )
    assert unknown_response.status_code == 404

    unsupported_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": "notes.txt", "report_id": report["id"]},
    )
    assert unsupported_response.status_code == 404

    def unreadable_file(_path: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable_file)
    unreadable_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": "report.md", "report_id": report["id"]},
    )
    assert unreadable_response.status_code == 400
    assert unreadable_response.json()["detail"] == "Artifact could not be read"


async def test_preview_markdown_artifact_resolves_from_git_worktree(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A markdown artifact that exists only in a git worktree must still preview.

    Agents work inside isolated git worktrees, so a report's markdown artifact
    frequently lives only in a sibling worktree rather than under the main
    workspace path. The preview endpoint must resolve those files via the
    workspace's git worktree roots instead of returning 404.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required to exercise worktree resolution")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")

    # The artifact lives ONLY in the worktree, not under the workspace path.
    worktree = tmp_path / "repo-worktree"
    git("worktree", "add", "-b", "feat/work", str(worktree))
    (worktree / "notes").mkdir()
    (worktree / "notes" / "worktree-only.md").write_text(
        "# Worktree Output\n\nProduced in the worktree.",
        encoding="utf-8",
    )
    # A markdown file outside every allowed root must stay unreachable.
    (tmp_path / "outside.md").write_text("# Secret", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")
    stub_workspace_terminal(monkeypatch, repo, tab_id="worktree-tab", port=18191)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Worktree Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "worktree",
        },
    ).json()
    session = await workspace_manager.ensure_workspace_agent(
        workspace["id"],
        workspace_module.EnsureWorkspaceAgentRequest(agent_type="claude", reuse_existing=False),
    )
    # The session records the main workspace path, NOT the worktree — proving the
    # fix relies on git worktree enumeration rather than session workspace_path.
    assert workspace_manager.sessions[session.id].workspace_path == str(repo)

    report = client.post(
        f"/api/workspaces/sessions/{session.id}/reports",
        json={
            "state": "completed",
            "message": "Worktree report ready",
            "changed_files": ["notes/worktree-only.md"],
            "artifact_refs": [str(tmp_path / "outside.md")],
        },
    ).json()

    # The worktree-only artifact resolves for preview (previously 404).
    response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": "notes/worktree-only.md", "report_id": report["id"]},
    )
    assert response.status_code == 200
    preview = response.json()
    assert preview["filename"] == "worktree-only.md"
    assert preview["content"] == "# Worktree Output\n\nProduced in the worktree."

    # It also surfaces in the workspace board's markdown documents.
    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert any(
        document["source"] == "changed_file" and document["path"] == "notes/worktree-only.md"
        for document in board["markdown_documents"]
    )

    # Path-escape safety: a markdown path outside ALL roots still 404s.
    outside_response = client.get(
        f"/api/workspaces/{workspace['id']}/artifacts/preview",
        params={"path": str(tmp_path / "outside.md"), "report_id": report["id"]},
    )
    assert outside_response.status_code == 404


def test_spawn_worker_is_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Spawn Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "spawn",
        },
    )
    task_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Worker task",
            "prompt": "Do worker work",
            "agent_type": "codex",
        },
    )

    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/spawn",
        json={},
    )

    assert response.status_code == 400
    assert "Worker spawning is disabled" in response.json()["detail"]
    assert workspace_manager.sessions == {}


def test_completed_report_creates_temporary_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []
    sent_messages: list[tuple[str, str]] = []
    renamed_tabs: list[tuple[str, str | None]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"review-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12400 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    deleted_tabs: list[str] = []

    async def fake_delete_tab(tab_id: str) -> bool:
        deleted_tabs.append(tab_id)
        return True

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: object) -> TerminalTab:
        renamed_tabs.append((tab_id, name))
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12499,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Review Loop", "path": str(repo), "session_prefix": "rl"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Review me", "prompt": "Implement then review"},
    ).json()
    started = client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    ).json()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "changed_files": ["frontend/src/App.vue"],
            "validation": "pnpm build",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_attempts == 1
    assert updated.review_session_id is not None
    reviewer = workspace_manager.sessions[updated.review_session_id]
    assert reviewer.role == WorkspaceSessionRole.REVIEWER
    assert reviewer.ephemeral is True
    assert reviewer.current_task_id == task["id"]
    assert reviewer.title == "Review me"
    assert renamed_tabs[-1] == (reviewer.tab_id, "Review me")
    assert "independent reviewer agent" in sent_messages[-2][1]
    assert "Review workspace task" in sent_messages[-1][1]

    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    assert updated.review_session_id in workspace_manager.sessions
    assert workspace_manager.sessions[reviewer.id].current_task_id == task["id"]

    done_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={"status": "done"})
    assert done_response.status_code == 200
    assert reviewer.id not in workspace_manager.sessions
    assert reviewer.tab_id in deleted_tabs


def test_reviewer_clears_context_between_unrelated_tasks(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-clear-tab",
        port=12810,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reviewer Clear", "path": str(repo), "session_prefix": "rc"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    persistent_reviewer = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"role": "reviewer", "reuse_existing": False},
    ).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First", "prompt": "Implement first"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    sent_messages.clear()
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": first_task["id"], "state": "completed", "message": "Done"},
    )

    first_reviewer_id = workspace_manager.tasks[first_task["id"]].review_session_id
    assert first_reviewer_id is not None
    assert first_reviewer_id == persistent_reviewer["id"]
    first_review_messages = [m for _sess, m in sent_messages]
    assert (
        "/clear" not in first_review_messages
    ), "Fresh reviewer with no prior history should not receive /clear"

    pass_resp = pass_task_review(client, first_task["id"])
    assert pass_resp.status_code == 201
    done_resp = client.patch(
        f"/api/workspaces/tasks/{first_task['id']}",
        json={"status": "done"},
    )
    assert done_resp.status_code == 200

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second", "prompt": "Implement second"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    sent_messages.clear()
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": second_task["id"], "state": "completed", "message": "Done"},
    )

    second_reviewer_id = workspace_manager.tasks[second_task["id"]].review_session_id
    assert (
        second_reviewer_id == first_reviewer_id
    ), "Reviewer session should be reused across unrelated tasks"
    second_messages = [m for _sess, m in sent_messages]
    clear_index = next(
        (i for i, m in enumerate(second_messages) if m == "/clear"),
        None,
    )
    prompt_index = next(
        (i for i, m in enumerate(second_messages) if "Review workspace task" in m),
        None,
    )
    assert (
        clear_index is not None
    ), "Reviewer with prior task history should receive /clear before unrelated review"
    assert prompt_index is not None
    assert clear_index < prompt_index


def test_reviewer_bound_to_working_task_is_not_reused_for_other_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-bound-tab",
        port=12812,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reviewer Bound", "path": str(repo), "session_prefix": "rb"},
    ).json()
    first_worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    second_worker = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"reuse_existing": False},
    ).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First", "prompt": "Needs revision"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": first_worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{first_worker['id']}/reports",
        json={"task_id": first_task["id"], "state": "completed", "message": "v1"},
    )
    first_reviewer_id = workspace_manager.tasks[first_task["id"]].review_session_id
    assert first_reviewer_id is not None

    fail_resp = client.post(
        f"/api/workspaces/sessions/{first_reviewer_id}/reports",
        json={
            "task_id": first_task["id"],
            "state": "review_failed",
            "message": "Needs changes.",
        },
    )
    assert fail_resp.status_code == 201
    assert workspace_manager.tasks[first_task["id"]].status == WorkspaceTaskStatus.WORKING
    assert workspace_manager.tasks[first_task["id"]].review_session_id == first_reviewer_id
    assert workspace_manager.sessions[first_reviewer_id].current_task_id == first_task["id"]

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second", "prompt": "Independent review"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": second_worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{second_worker['id']}/reports",
        json={"task_id": second_task["id"], "state": "completed", "message": "done"},
    )

    second_reviewer_id = workspace_manager.tasks[second_task["id"]].review_session_id
    assert second_reviewer_id is not None
    assert second_reviewer_id != first_reviewer_id


def test_shared_review_session_id_does_not_steal_busy_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When two tasks both carry the same review_session_id (from a prior round)
    and both request review, the second task must NOT steal the busy reviewer
    from the first task. Instead it should fall through to the next available
    reviewer or create a new one.

    This is the root cause of the "only one task gets reviewed while others
    wait" bug: every task was reviewed by reviewer-1 historically, so every
    task has review_session_id=reviewer-1, and the _select_or_create_reviewer
    fast path would unconditionally return reviewer-1, letting the last
    request "win" and strand the others.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="shared-rev-tab",
        port=12813,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Shared Reviewer", "path": str(repo), "session_prefix": "sr"},
    ).json()
    first_worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    second_worker = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"reuse_existing": False},
    ).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First", "prompt": "First task"},
    ).json()
    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second", "prompt": "Second task"},
    ).json()

    # First task: start and complete to get a reviewer assigned.
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": first_worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{first_worker['id']}/reports",
        json={"task_id": first_task["id"], "state": "completed", "message": "v1"},
    )
    first_reviewer_id = workspace_manager.tasks[first_task["id"]].review_session_id
    assert first_reviewer_id is not None

    # Simulate the historical scenario: both tasks share the same
    # review_session_id because they were both reviewed by the same
    # reviewer in a prior round (before more reviewers were added).
    second_task_obj = workspace_manager.tasks[second_task["id"]]
    workspace_manager.tasks[second_task["id"]] = second_task_obj.model_copy(
        update={"review_session_id": first_reviewer_id}
    )

    # Now have both workers request review for their tasks.
    # First worker requests review → should get first_reviewer_id.
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": first_worker["id"]},
    )
    first_ready = client.post(
        f"/api/workspaces/sessions/{first_worker['id']}/reports",
        json={"task_id": first_task["id"], "state": "ready_for_review", "message": "Done"},
    )
    assert first_ready.status_code == 201
    assert workspace_manager.tasks[first_task["id"]].review_session_id == first_reviewer_id
    assert workspace_manager.sessions[first_reviewer_id].task_id == first_task["id"]

    # Second worker requests review → should NOT steal first_reviewer_id
    # from the first task. It should fall through and get a different reviewer.
    client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": second_worker["id"]},
    )
    second_ready = client.post(
        f"/api/workspaces/sessions/{second_worker['id']}/reports",
        json={"task_id": second_task["id"], "state": "ready_for_review", "message": "Done"},
    )
    assert second_ready.status_code == 201

    second_reviewer_id = workspace_manager.tasks[second_task["id"]].review_session_id
    assert second_reviewer_id is not None
    assert second_reviewer_id != first_reviewer_id, (
        "Second task must NOT steal the busy reviewer from the first task; "
        "it should get its own reviewer instead"
    )

    # First task's reviewer binding must remain intact.
    assert workspace_manager.tasks[first_task["id"]].review_session_id == first_reviewer_id
    assert workspace_manager.sessions[first_reviewer_id].task_id == first_task["id"]

    # Second task must have its own reviewer that is actually assigned to it.
    assert workspace_manager.sessions[second_reviewer_id].task_id == second_task["id"]


def test_failed_review_continues_reviewed_task_after_repeated_attempts(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-repeat-tab",
        port=12815,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Repeated Review", "path": str(repo), "session_prefix": "rr"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Keep iterating", "prompt": "Fix until review passes"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "v1"},
    )

    prepared_task = workspace_manager.tasks[task["id"]]
    reviewer_id = prepared_task.review_session_id
    assert reviewer_id is not None
    workspace_manager.tasks[task["id"]] = prepared_task.model_copy(
        update={"review_attempts": workspace_module.MAX_AUTOMATED_REVIEW_FAILURES + 1}
    )
    sent_messages.clear()

    fail_resp = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Please address another blocking issue.",
        },
    )

    assert fail_resp.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    worker_session = workspace_manager.sessions[worker["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert worker_session.status == ManagedSessionStatus.WORKING
    assert worker_session.runtime_status == AgentRuntimeStatus.WORKING
    assert any("Reviewer requested changes" in message for _sess, message in sent_messages)


def test_reviewer_keeps_context_when_re_reviewing_same_task(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-same-tab",
        port=12820,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reviewer Same", "path": str(repo), "session_prefix": "rs"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Same", "prompt": "Implement and revise"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "v1"},
    )

    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    fail_resp = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Please address X.",
        },
    )
    assert fail_resp.status_code == 201

    sent_messages.clear()
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "v2"},
    )

    assert workspace_manager.tasks[task["id"]].review_session_id == reviewer_id
    re_review_messages = [m for _sess, m in sent_messages]
    assert (
        "/clear" not in re_review_messages
    ), "Re-reviewing the same task on the same reviewer must keep prior context"


def test_reviewer_clears_context_even_when_prior_task_review_session_id_nulled(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: the cross-task /clear must still fire when the previously
    reviewed task's ``review_session_id`` has been nulled (as abort, skip, and
    stale-reviewer-release paths do). The decision keys off the reviewer
    session's ``last_review_task_id``, not a scan of other tasks' fields."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-nulled-tab",
        port=12830,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reviewer Nulled", "path": str(repo), "session_prefix": "rn"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    persistent_reviewer = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"role": "reviewer", "reuse_existing": False},
    ).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First", "prompt": "Implement first"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": first_task["id"], "state": "completed", "message": "Done"},
    )

    first_reviewer_id = workspace_manager.tasks[first_task["id"]].review_session_id
    assert first_reviewer_id == persistent_reviewer["id"]
    # The reviewer session now remembers it reviewed the first task.
    assert workspace_manager.sessions[first_reviewer_id].last_review_task_id == first_task["id"]

    # Simulate a terminal path (skip / abort / stale-release) nulling the first
    # task's review_session_id back-reference. The old task-scan heuristic would
    # now see no prior history and skip /clear.
    first = workspace_manager.tasks[first_task["id"]]
    workspace_manager.tasks[first_task["id"]] = first.model_copy(update={"review_session_id": None})

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second", "prompt": "Implement second"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    sent_messages.clear()
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": second_task["id"], "state": "completed", "message": "Done"},
    )

    second_reviewer_id = workspace_manager.tasks[second_task["id"]].review_session_id
    assert second_reviewer_id == first_reviewer_id
    second_messages = [m for _sess, m in sent_messages]
    clear_index = next(
        (i for i, m in enumerate(second_messages) if m == "/clear"),
        None,
    )
    prompt_index = next(
        (i for i, m in enumerate(second_messages) if "Review workspace task" in m),
        None,
    )
    assert (
        clear_index is not None
    ), "Reviewer must /clear before an unrelated review even if the prior task's review_session_id was nulled"
    assert prompt_index is not None
    assert clear_index < prompt_index


@pytest.mark.parametrize("report_state", ["ready_for_review", "blocked", "needs_input"])
def test_agent_review_gate_states_trigger_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    report_state: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"manual-review-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12500 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Manual Review", "path": str(repo), "session_prefix": "manual"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Manual gate", "prompt": "Stop at human review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": report_state,
            "message": "Needs reviewer gate",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_session_id is not None
    assert (
        workspace_manager.sessions[updated.review_session_id].role == WorkspaceSessionRole.REVIEWER
    )


def test_completed_skip_review_marks_review_without_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="skip-review-tab",
        port=12550,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Skip Review", "path": str(repo), "session_prefix": "skip"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Analysis task",
            "prompt": "Analyze without editing",
            "goal_packet": {
                "objective": "Analyze without editing.",
                "acceptance_criteria": ["Analysis is complete"],
                "validation_plan": ["Review notes"],
                "assumptions": [],
                "out_of_scope": ["Code changes"],
                "handoff_requirements": ["Summarize findings"],
            },
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Read-only analysis complete",
            "acceptance_check": [
                {
                    "criterion": "Analysis is complete",
                    "status": "passed",
                    "evidence": "Report summarizes the findings",
                }
            ],
            "review_decision": "skip",
            "review_reason": "No files changed; analysis only.",
            "risk_level": "low",
        },
    )

    assert response.status_code == 201
    assert response.json()["review_decision"] == "skip"
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_session_id is None
    assert updated.review_attempts == 0
    assert updated.review_skipped_at is not None
    assert updated.human_acceptance_requested_at is not None
    assert updated.human_accepted_at is None
    assert updated.review_skip_reason == "No files changed; analysis only."
    assert sent_messages == []

    done_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"status": "done"},
    )
    assert done_response.status_code == 200
    assert done_response.json()["human_accepted_at"] is not None


def test_manual_feedback_reaper_promotes_lesson(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="feedback-reaper-tab",
        port=12531,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Feedback Repo", "path": str(repo), "session_prefix": "feed"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "CLI symbols", "prompt": "Figure out the --symbols CLI format"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    report_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "--symbols expects comma-separated values.",
            "validation": "Ran CLI probe",
            "risks": "none",
            "artifact_refs": ["artifacts/probe.log"],
        },
    )
    assert report_response.status_code == 201

    reap_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/feedback/reap",
        json={
            "source": "human",
            "summary": "Human confirmed the reusable CLI symbols lesson.",
            "tags": ["cli", "symbols"],
            "lesson_drafts": [
                {
                    "summary": "--symbols expects comma-separated values.",
                    "applies_when": ["CLI symbol lists", "market data probes"],
                    "do": "Use --symbols AAPL,MSFT.",
                    "avoid": "Do not pass --symbols AAPL MSFT.",
                    "tags": ["cli", "symbols"],
                    "scope": "workspace",
                    "confidence": 0.9,
                    "promote_to_active": True,
                }
            ],
        },
    )

    assert reap_response.status_code == 200
    run = reap_response.json()
    assert run["record"]["source"] == "human"
    assert run["record"]["artifact_refs"] == ["artifacts/probe.log"]
    assert run["lesson_drafts"][0]["summary"] == "--symbols expects comma-separated values."
    assert run["promoted_lessons"][0]["status"] == "active"
    assert "You are an internal Feedback Reaper" in run["reaper_prompt"]

    lessons_response = client.get(
        f"/api/workspaces/{workspace['id']}/lessons",
        params={"query": "symbols market data"},
    )
    assert lessons_response.status_code == 200
    lessons = lessons_response.json()
    assert len(lessons) == 1
    assert lessons[0]["title"] == "--symbols expects comma-separated values"
    assert lessons[0]["summary"] == "--symbols expects comma-separated values."

    write_iteration_task_record_fixture(state_root, workspace["id"], task["id"])
    manual_lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={
            "title": "Use comma-separated symbols",
            "summary": "CLI symbol probes should pass one comma-separated --symbols value.",
            "applies_when": ["CLI --symbols flag", "market data probes"],
            "do": "Pass symbols comma-separated: --symbols AAPL,MSFT.",
            "avoid": "Do not pass --symbols with whitespace separators.",
            "tags": ["cli", "symbols"],
            "scope": "workspace",
            "evidence_task_ids": [task["id"]],
            "source_record_ids": ["record-1"],
            "confidence": 0.6,
        },
    )
    assert manual_lesson_response.status_code == 201
    manual_lesson = manual_lesson_response.json()
    assert manual_lesson["title"] == "Use comma-separated symbols"
    duplicate_lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={
            "title": "Use comma-separated symbols",
            "summary": "CLI symbol probes should pass one comma-separated --symbols value.",
            "applies_when": ["CLI --symbols flag", "market data probes"],
            "do": "Pass symbols comma-separated: --symbols AAPL,MSFT.",
            "avoid": "Do not pass --symbols with whitespace separators.",
            "tags": ["symbols", "cli"],
            "scope": "workspace",
            "evidence_task_ids": [task["id"], "task-two"],
            "source_record_ids": ["record-2"],
            "confidence": 0.9,
        },
    )
    assert duplicate_lesson_response.status_code == 201
    duplicate_lesson = duplicate_lesson_response.json()
    assert duplicate_lesson["id"] == manual_lesson["id"]
    assert duplicate_lesson["evidence_task_ids"] == [task["id"], "task-two"]
    assert duplicate_lesson["source_record_ids"] == ["record-1", "record-2"]
    assert duplicate_lesson["confidence"] == 0.85
    assert duplicate_lesson["fingerprint"] == manual_lesson["fingerprint"]
    deduped_lessons_response = client.get(f"/api/workspaces/{workspace['id']}/lessons")
    assert deduped_lessons_response.status_code == 200
    assert len(deduped_lessons_response.json()) == 2

    delete_lesson_response = client.delete(
        f"/api/workspaces/{workspace['id']}/lessons/{manual_lesson['id']}"
    )
    assert delete_lesson_response.status_code == 200
    assert delete_lesson_response.json()["status"] == "archived"
    active_lessons_response = client.get(f"/api/workspaces/{workspace['id']}/lessons")
    assert active_lessons_response.status_code == 200
    assert manual_lesson["id"] not in {lesson["id"] for lesson in active_lessons_response.json()}

    feedback_dir = state_root / workspace["id"] / "feedback"
    assert list((feedback_dir / "records").glob("*.json"))
    assert list((feedback_dir / "lesson-drafts").glob("*.json"))
    assert (feedback_dir / "lesson-index.json").exists()

    bad_lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={"summary": "   "},
    )
    assert bad_lesson_response.status_code == 400

    bad_draft_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/feedback/reap",
        json={"lesson_drafts": [{"summary": "   "}]},
    )
    assert bad_draft_response.status_code == 400


def test_task_assignment_injects_lessons_index_with_api_and_take_tracking(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="lesson-context-tab",
        port=12532,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Lesson Repo", "path": str(repo), "session_prefix": "less"},
    ).json()
    write_iteration_task_record_fixture(state_root, workspace["id"], "evidence-task-cli")
    lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={
            "id": "cli-symbols-comma-separated",
            "summary": "--symbols expects comma-separated values.",
            "applies_when": ["CLI symbol lists"],
            "do": "Use --symbols AAPL,MSFT.",
            "avoid": "Do not pass --symbols AAPL MSFT.",
            "tags": ["cli", "symbols"],
            "scope": "workspace",
            "evidence_task_ids": ["evidence-task-cli"],
            "confidence": 0.9,
        },
    )
    assert lesson_response.status_code == 201

    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Symbols CLI probe", "prompt": "Probe market data symbols via CLI"},
    ).json()
    start_response = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={})

    assert start_response.status_code == 201
    started_task = start_response.json()
    assert started_task["feedback_lesson_ids"] == []
    assignment_prompt = sent_messages[-1][1]
    assert "Workspace lessons index" in assignment_prompt
    assert "cli-symbols-comma-separated" in assignment_prompt
    assert "docs/working-logs/lessons-catalog.md" in assignment_prompt
    assert "/api/workspaces/" in assignment_prompt
    assert "/lessons/<lesson_id>" in assignment_prompt
    assert "This workspace ID:" in assignment_prompt
    assert workspace["id"] in assignment_prompt
    assert "Read lessons only when you judge they may apply" in assignment_prompt
    assert "Use --symbols AAPL,MSFT" not in assignment_prompt
    task_reports = [
        report
        for report in workspace_manager.reports_for_workspace(workspace["id"])
        if report.task_id == task["id"]
    ]
    assert task_reports == []

    fetch_response = client.get(
        f"/api/workspaces/{workspace['id']}/lessons/cli-symbols-comma-separated"
    )
    assert fetch_response.status_code == 200
    fetched = fetch_response.json()
    assert fetched["id"] == "cli-symbols-comma-separated"
    assert fetched["summary"] == "--symbols expects comma-separated values."
    assert fetched["do"] == "Use --symbols AAPL,MSFT."
    assert fetched["avoid"] == "Do not pass --symbols AAPL MSFT."
    assert fetched["hit_count"] == 1

    fetch_response2 = client.get(
        f"/api/workspaces/{workspace['id']}/lessons/cli-symbols-comma-separated"
    )
    assert fetch_response2.json()["hit_count"] == 2

    notfound_response = client.get(f"/api/workspaces/{workspace['id']}/lessons/nonexistent-lesson")
    assert notfound_response.status_code == 404


def test_workspace_feedback_summary_uses_hidden_internal_reaper_task(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="internal-reaper-tab",
        port=12536,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Internal Reaper Repo", "path": str(repo), "session_prefix": "reap"},
    ).json()
    record_dir = tmp_path / "state" / workspace["id"] / "task_records"
    record_dir.mkdir(parents=True)
    record_dir.joinpath("2026-06-07T12-00-00-task-one.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": workspace["id"],
                "task": {
                    "id": "task-one",
                    "title": "Probe symbols",
                    "status": "done",
                    "completed_at": "2026-06-07T12:00:00",
                },
                "reports": [
                    {"state": "started"},
                    {"state": "working"},
                    {"state": "review_failed"},
                    {"state": "working"},
                    {"state": "review_failed"},
                    {"state": "completed", "message": "Use comma symbols."},
                ],
                "artifacts": {
                    "changed_files": [],
                    "validation": ["CLI probe passed."],
                    "risks": [],
                },
                "final_summary": "Use comma-separated symbols for CLI probes.",
            }
        ),
        encoding="utf-8",
    )
    lesson_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons",
        json={
            "id": "cli-symbols-comma-separated",
            "title": "Use comma-separated symbols",
            "summary": "--symbols expects comma-separated values.",
            "applies_when": ["CLI symbol lists"],
            "do": "Use --symbols AAPL,MSFT.",
            "avoid": "Do not pass --symbols AAPL MSFT.",
            "tags": ["cli", "symbols"],
            "scope": "workspace",
            "evidence_task_ids": ["task-one"],
        },
    )
    assert lesson_response.status_code == 201

    response = client.post(f"/api/workspaces/{workspace['id']}/lessons/summarize")

    assert response.status_code == 201
    summary_run = response.json()
    assert summary_run["cache_hit"] is False
    assert summary_run["input_record_ids"] == ["task-one"]
    internal_task = workspace_manager.tasks[summary_run["task_id"]]
    assert internal_task.system_internal is True
    assert internal_task.internal_kind == "feedback_reaper"
    assert internal_task.status == WorkspaceTaskStatus.WORKING
    assert sent_messages
    reaper_prompt = sent_messages[-1][1]
    assert "internal Feedback Reaper" in reaper_prompt
    assert "system-internal task" in reaper_prompt
    assert "input_task_digests" in reaper_prompt
    assert "task-one" in reaper_prompt
    assert "POST /api/workspaces/" in reaper_prompt

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert internal_task.id not in {task["id"] for task in board["tasks"]}
    assert workspace_manager.tasks[internal_task.id].system_internal is True
    audit_reports = [
        report
        for report in workspace_manager.reports_for_workspace(workspace["id"])
        if report.task_id == internal_task.id and report.risk_level == "system_audit"
    ]
    assert audit_reports

    workspace_manager._write_snapshot(workspace["id"])
    snapshot = workspace_manager.snapshot_path(workspace["id"]).read_text(encoding="utf-8")
    assert internal_task.id not in snapshot
    assert "Feedback Reaper: summarize workspace lessons" not in snapshot
    feedback_index = json.loads(
        (tmp_path / "state" / workspace["id"] / "feedback" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    assert feedback_index["processed_task_records"][0]["task_id"] == "task-one"

    completion_response = client.post(
        f"/api/workspaces/sessions/{internal_task.session_id}/reports",
        json={
            "task_id": internal_task.id,
            "state": "completed",
            "message": "Internal reaper finished.",
            "changed_files": [],
            "validation": "skipped_reason=no_new_lessons",
            "review_decision": "skip",
            "review_reason": "System-internal Feedback Reaper audit.",
            "risk_level": "system_audit",
        },
    )

    assert completion_response.status_code == 201
    completed = workspace_manager.tasks[internal_task.id]
    assert completed.status == WorkspaceTaskStatus.DONE
    assert completed.review_session_id is None
    assert completed.review_skipped_at is not None
    assert completed.review_skip_reason == "System-internal task completed without human review."
    summary_run_files = list(
        (tmp_path / "state" / workspace["id"] / "feedback" / "summary-runs").glob("*.json")
    )
    completed_run = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in summary_run_files
        if json.loads(path.read_text(encoding="utf-8"))["id"] == summary_run["id"]
    ][0]
    assert completed_run["skipped_reason"] == "no_new_lessons"

    sent_messages.clear()
    cache_response = client.post(f"/api/workspaces/{workspace['id']}/lessons/summarize")
    assert cache_response.status_code == 201
    cached_run = cache_response.json()
    assert cached_run["cache_hit"] is True
    assert cached_run["task_id"] is None
    assert cached_run["skipped_reason"] == "no_new_task_records"
    assert sent_messages == []

    force_response = client.post(
        f"/api/workspaces/{workspace['id']}/lessons/summarize",
        json={"force": True, "limit": 1},
    )
    assert force_response.status_code == 201
    force_run = force_response.json()
    assert force_run["cache_hit"] is False
    assert force_run["input_record_ids"] == ["task-one"]
    assert force_run["task_id"] in workspace_manager.tasks
    assert sent_messages


def test_lessons_index_includes_all_active_lessons_without_full_body_leak(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="cjk-lesson-tab",
        port=12535,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    cjk_workspace = client.post(
        "/api/workspaces",
        json={"name": "CJK Lessons", "path": str(repo), "session_prefix": "cjkl"},
    ).json()
    write_iteration_task_record_fixture(state_root, cjk_workspace["id"], "evidence-cjk-image")
    write_iteration_task_record_fixture(state_root, cjk_workspace["id"], "evidence-cjk-market")
    client.post(
        f"/api/workspaces/{cjk_workspace['id']}/lessons",
        json={
            "id": "image-workflow-docs-first",
            "title": "图片生成先读文档",
            "summary": "图片生成任务要先阅读工作流文档。",
            "applies_when": ["图片生成", "写真任务", "工作流"],
            "do": "先检查仓库工作流文档和已有运行记录。",
            "avoid": "不要只看原始提示词就开始生成。",
            "tags": ["图片", "工作流"],
            "scope": "workspace",
            "evidence_task_ids": ["evidence-cjk-image"],
            "confidence": 0.8,
        },
    )
    client.post(
        f"/api/workspaces/{cjk_workspace['id']}/lessons",
        json={
            "id": "market-data-symbols",
            "title": "Market data CLI uses comma-separated symbols",
            "summary": "Market data CLI symbols must be comma-separated.",
            "applies_when": ["market data"],
            "do": "Use --symbols AAPL,MSFT.",
            "avoid": "Do not pass symbols as separate arguments.",
            "tags": ["market", "cli"],
            "scope": "workspace",
            "evidence_task_ids": ["evidence-cjk-market"],
            "confidence": 0.8,
        },
    )

    cjk_task = client.post(
        f"/api/workspaces/{cjk_workspace['id']}/tasks",
        json={
            "title": "写真图片生成",
            "prompt": "生成希希芙写真图片，先看仓库工作流。",
        },
    ).json()
    cjk_start_response = client.post(
        f"/api/workspaces/tasks/{cjk_task['id']}/start",
        json={},
    )

    assert cjk_start_response.status_code == 201
    cjk_started_task = cjk_start_response.json()
    assert cjk_started_task["feedback_lesson_ids"] == []
    cjk_prompt = sent_messages[-1][1]
    assert "Workspace lessons index" in cjk_prompt
    assert "image-workflow-docs-first" in cjk_prompt
    assert "market-data-symbols" in cjk_prompt
    assert "图片生成先读文档" in cjk_prompt
    assert "Market data CLI uses comma-separated symbols" in cjk_prompt
    assert "先检查仓库工作流文档和已有运行记录。" not in cjk_prompt
    assert "Use --symbols AAPL,MSFT." not in cjk_prompt
    assert "Do not pass symbols as separate arguments." not in cjk_prompt
    assert "不要只看原始提示词就开始生成。" not in cjk_prompt
    assert "docs/working-logs/lessons-catalog.md" in cjk_prompt
    assert "/api/workspaces/" in cjk_prompt

    emoji_workspace = client.post(
        "/api/workspaces",
        json={"name": "Emoji Lessons", "path": str(repo), "session_prefix": "emol"},
    ).json()
    write_iteration_task_record_fixture(state_root, emoji_workspace["id"], "evidence-emoji")
    client.post(
        f"/api/workspaces/{emoji_workspace['id']}/lessons",
        json={
            "id": "emoji-only-workspace-lesson",
            "title": "Emoji-only workspace still gets index",
            "summary": "All active lessons appear in the index regardless of query overlap.",
            "applies_when": ["any task"],
            "do": "Agent decides autonomously which lessons apply.",
            "avoid": "Do not force-fit lessons.",
            "tags": ["emoji"],
            "scope": "workspace",
            "evidence_task_ids": ["evidence-emoji"],
            "confidence": 0.8,
        },
    )
    emoji_task = client.post(
        f"/api/workspaces/{emoji_workspace['id']}/tasks",
        json={"title": "😀😀", "prompt": "🔥🔥"},
    ).json()
    emoji_start_response = client.post(
        f"/api/workspaces/tasks/{emoji_task['id']}/start",
        json={},
    )

    assert emoji_start_response.status_code == 201
    emoji_started_task = emoji_start_response.json()
    assert emoji_started_task["feedback_lesson_ids"] == []
    emoji_prompt = sent_messages[-1][1]
    assert "Workspace lessons index" in emoji_prompt
    assert "emoji-only-workspace-lesson" in emoji_prompt
    assert "Agent decides autonomously which lessons apply." not in emoji_prompt
    task_reports = [
        report
        for report in workspace_manager.reports_for_workspace(emoji_workspace["id"])
        if report.task_id == emoji_task["id"]
    ]
    assert task_reports == []


@pytest.mark.parametrize(
    ("task_goal_packet", "acceptance_check", "expected_gap"),
    [
        (
            None,
            [
                {
                    "criterion": "Analysis is complete",
                    "status": "passed",
                    "evidence": "Report summarizes the findings",
                }
            ],
            "stored Goal Packet",
        ),
        (
            {
                "objective": "Analyze without editing.",
                "acceptance_criteria": ["Analysis is complete"],
                "validation_plan": ["Review notes"],
                "assumptions": [],
                "out_of_scope": ["Code changes"],
                "handoff_requirements": ["Summarize findings"],
            },
            [],
            "acceptance_check evidence",
        ),
    ],
)
def test_completed_skip_review_requires_goal_packet_audit_evidence(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    task_goal_packet: dict[str, object] | None,
    acceptance_check: list[dict[str, str]],
    expected_gap: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="skip-goal-required-tab",
        port=12556,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Skip Goal Required", "path": str(repo), "session_prefix": "skipg"},
    ).json()
    task_payload: dict[str, object] = {
        "title": "Analysis task",
        "prompt": "Analyze without editing",
    }
    if task_goal_packet is not None:
        task_payload["goal_packet"] = task_goal_packet
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json=task_payload,
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Read-only analysis complete",
            "acceptance_check": acceptance_check,
            "review_decision": "skip",
            "review_reason": "No files changed; analysis only.",
            "risk_level": "low",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    session = workspace_manager.sessions[started["session_id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert updated.review_session_id is None
    assert updated.review_skipped_at is None
    assert session.status == ManagedSessionStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.WORKING
    assert len(sent_messages) == 1
    assert "completion-style workspace report is missing" in sent_messages[0][1]
    assert expected_gap in sent_messages[0][1]
    assert "acceptance_check" in sent_messages[0][1]
    assert "goal_packet" in sent_messages[0][1]


def test_completed_skip_review_allows_explicit_trivial_changed_files(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="skip-trivial-tab",
        port=12557,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Skip Trivial", "path": str(repo), "session_prefix": "skip-trivial"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Bind host",
            "prompt": "Switch dev server host to 0.0.0.0",
            "goal_packet": {
                "objective": "Switch dev server host to 0.0.0.0.",
                "acceptance_criteria": ["Host binding is updated"],
                "validation_plan": ["Inspect config diff"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": ["List changed files"],
            },
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Changed only the dev host bind address",
            "changed_files": ["frontend/vite.config.ts"],
            "acceptance_check": [
                {
                    "criterion": "Host binding is updated",
                    "status": "passed",
                    "evidence": "Config diff only changes the bind host",
                }
            ],
            "review_decision": "skip",
            "review_reason": "Trivial host bind change; no AI reviewer needed.",
            "risk_level": "trivial",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_session_id is None
    assert updated.review_attempts == 0
    assert updated.review_skipped_at is not None
    assert updated.human_acceptance_requested_at is not None
    assert updated.review_skip_reason == "Trivial host bind change; no AI reviewer needed."
    assert sent_messages == []


def test_completed_skip_review_is_denied_for_changed_files(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="skip-denied-tab",
        port=12551,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Skip Denied", "path": str(repo), "session_prefix": "skip-denied"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Code task",
            "prompt": "Change code",
            "goal_packet": {
                "objective": "Change code.",
                "acceptance_criteria": ["Code change is complete"],
                "validation_plan": ["Run tests"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": ["List changed files"],
            },
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Code changed but requesting skip",
            "changed_files": ["backend/claude_hub/services/workspace_manager.py"],
            "acceptance_check": [
                {
                    "criterion": "Code change is complete",
                    "status": "passed",
                    "evidence": "Implementation files changed",
                }
            ],
            "review_decision": "skip",
            "review_reason": "Agent thinks this is safe.",
            "risk_level": "low",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_session_id is not None
    assert updated.review_attempts == 1
    assert updated.review_skipped_at is None
    assert "Review workspace task" in sent_messages[-1][1]


def test_completed_skip_review_is_denied_for_tracked_diff(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="skip-diff-tab",
        port=12552,
        sent_messages=sent_messages,
    )

    async def fake_has_tracked_changes(_workspace_id: str) -> bool:
        return True

    monkeypatch.setattr(
        workspace_manager,
        "_workspace_has_tracked_changes",
        fake_has_tracked_changes,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Skip Tracked Diff", "path": str(repo), "session_prefix": "skip-diff"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Dirty task",
            "prompt": "Change tracked files",
            "goal_packet": {
                "objective": "Change tracked files.",
                "acceptance_criteria": ["Tracked diff is complete"],
                "validation_plan": ["Run tests"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": ["List changed files"],
            },
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "No changed_files reported but repo is dirty",
            "acceptance_check": [
                {
                    "criterion": "Tracked diff is complete",
                    "status": "passed",
                    "evidence": "Repository has tracked changes",
                }
            ],
            "review_decision": "skip",
            "review_reason": "No changed_files in report.",
            "risk_level": "low",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.review_session_id is not None
    assert updated.review_attempts == 1
    assert "Review workspace task" in sent_messages[-1][1]


def test_manual_request_review_after_skipped_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="manual-review-tab",
        port=12553,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Manual Review", "path": str(repo), "session_prefix": "manual-review"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Manual request",
            "prompt": "Maybe review later",
            "goal_packet": {
                "objective": "Maybe review later.",
                "acceptance_criteria": ["Analysis is complete"],
                "validation_plan": ["Read report"],
                "assumptions": [],
                "out_of_scope": [],
                "handoff_requirements": ["Explain review routing"],
            },
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Skipping review for now",
            "acceptance_check": [
                {
                    "criterion": "Analysis is complete",
                    "status": "passed",
                    "evidence": "Report completed",
                }
            ],
            "review_decision": "skip",
            "review_reason": "No changes.",
            "risk_level": "low",
        },
    )
    sent_messages.clear()

    response = client.post(
        f"/api/workspaces/tasks/{task['id']}/request-review",
        json={"message": "Please check the no-change skip evidence."},
    )

    assert response.status_code == 200
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.REVIEW
    assert updated.review_session_id is not None
    assert updated.review_attempts == 1
    assert updated.review_skipped_at is None
    assert updated.human_acceptance_requested_at is None
    assert (
        "Please check the no-change skip evidence."
        in list(workspace_manager.reports.values())[-1].message
    )
    assert "Review workspace task" in sent_messages[-1][1]
    assert "Please check the no-change skip evidence." in sent_messages[-1][1]


def test_review_passed_keeps_task_in_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"pass-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12600 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Pass Review", "path": str(repo), "session_prefix": "pass"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Pass task", "prompt": "Complete cleanly"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "Done"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    stale_reviewer_id = "pass-reviewer-stale"
    workspace_manager.sessions[stale_reviewer_id] = workspace_manager.sessions[
        reviewer_id
    ].model_copy(
        update={
            "id": stale_reviewer_id,
            "task_id": task["id"],
            "current_task_id": task["id"],
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Looks good",
            "validation": "Reviewed reported checks",
        },
    )

    assert response.status_code == 201
    reviewed_task = workspace_manager.tasks[task["id"]]
    assert reviewed_task.status == WorkspaceTaskStatus.REVIEW
    assert reviewed_task.completed_at is None
    assert reviewed_task.review_completed_at is not None
    assert reviewed_task.human_acceptance_requested_at is not None
    assert reviewed_task.human_accepted_at is None
    assert workspace_manager.sessions[started["session_id"]].current_task_id == task["id"]
    assert workspace_manager.sessions[reviewer_id].current_task_id == task["id"]
    assert workspace_manager.sessions[stale_reviewer_id].current_task_id is None
    assert workspace_manager.sessions[stale_reviewer_id].status == ManagedSessionStatus.IDLE

    done_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"status": "done"},
    )
    assert done_response.status_code == 200
    assert done_response.json()["status"] == "done"
    assert done_response.json()["human_accepted_at"] is not None


def test_review_failed_returns_feedback_to_original_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"fail-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12700 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Fail Review", "path": str(repo), "session_prefix": "fail"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Fail task", "prompt": "Needs a fix"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "Done"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Required fixes: add the missing assertion.",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert workspace_manager.sessions[started["session_id"]].current_task_id == task["id"]
    assert "Reviewer requested changes" in sent_messages[-1][1]
    assert "add the missing assertion" in sent_messages[-1][1]


def test_request_changes_succeeds_while_agent_holds_review_task(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="busy-agent-tab",
        port=12750,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Busy Agent", "path": str(repo), "session_prefix": "busy"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First task", "prompt": "Implement first"},
    ).json()
    first_started = client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": worker["id"]},
    ).json()
    client.post(
        f"/api/workspaces/sessions/{first_started['session_id']}/reports",
        json={
            "task_id": first_task["id"],
            "state": "completed",
            "message": "First task done",
        },
    )
    pass_response = pass_task_review(client, first_task["id"])
    assert pass_response.status_code == 201

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second task", "prompt": "Implement second"},
    ).json()
    second_started = client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    assert second_started.status_code == 201
    # The original agent stays locked to the first task while it remains in
    # REVIEW. The second task may queue behind it but cannot preempt it.
    assert workspace_manager.tasks[second_task["id"]].status == WorkspaceTaskStatus.QUEUED
    assert (
        workspace_manager.sessions[first_started["session_id"]].current_task_id == first_task["id"]
    )

    # Because the agent still owns the first task's context, requesting changes
    # works and re-engages the same session.
    response = client.post(
        f"/api/workspaces/tasks/{first_task['id']}/continue",
        json={"message": "Please adjust before human acceptance."},
    )

    assert response.status_code == 200
    assert workspace_manager.tasks[first_task["id"]].status == WorkspaceTaskStatus.WORKING
    assert (
        workspace_manager.sessions[first_started["session_id"]].current_task_id == first_task["id"]
    )
    assert workspace_manager.tasks[second_task["id"]].status == WorkspaceTaskStatus.QUEUED


def test_start_task_dispatches_to_resident_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []
    tab_metadata_updates: list[tuple[str, str | None, str | None, WorkspaceSessionRole | None]] = []
    renamed_tabs: list[tuple[str, str | None]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: object) -> TerminalTab:
        renamed_tabs.append((tab_id, name))
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_response.json()["id"],
            workspace_name="Resident Repo",
            workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
        )

    def fake_set_tab_workspace_metadata(
        tab_id: str,
        workspace_id: str | None,
        workspace_name: str | None,
        workspace_role: WorkspaceSessionRole | None,
    ) -> bool:
        tab_metadata_updates.append((tab_id, workspace_id, workspace_name, workspace_role))
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "set_tab_workspace_metadata",
        fake_set_tab_workspace_metadata,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Resident Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "resident",
        },
    )
    task_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Resident task",
            "prompt": "Run this in the resident terminal",
            "agent_type": "codex",
        },
    )

    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/start",
        json={},
    )

    assert response.status_code == 201
    started_task = response.json()
    assert started_task["status"] == "working"
    assert started_task["session_id"] == "resident-agent-1"
    session = workspace_manager.sessions[started_task["session_id"]]
    assert session.role == WorkspaceSessionRole.ORCHESTRATOR
    assert session.title == "Resident task"
    assert session.workspace_path == str(repo)
    assert renamed_tabs == [("tab-agent", "Resident task")]
    assert len(sent_messages) == 2
    assert "resident workspace agent" in sent_messages[0][1]
    assert "Run this in the resident terminal" in sent_messages[1][1]
    assert renamed_tabs == [("tab-agent", "Resident task")]

    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    board = board_response.json()
    assert board["workspace"]["dispatcher_session_id"] is None
    assert board["tasks"][0]["status"] == "working"
    assert board["tasks"][0]["session_id"] == "resident-agent-1"
    assert tab_metadata_updates[-1] == (
        "tab-agent",
        workspace_response.json()["id"],
        "Resident Repo",
        WorkspaceSessionRole.ORCHESTRATOR,
    )

    report_response = client.post(
        "/api/workspaces/sessions/resident-agent-1/reports",
        json={
            "task_id": task_response.json()["id"],
            "state": "started",
            "message": "Started resident task",
        },
    )

    assert report_response.status_code == 201
    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    assert board_response.json()["tasks"][0]["status"] == "working"


def test_review_task_keeps_agent_locked_until_done(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    status_samples: list[TerminalAgentStatus] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="queue-review-tab",
        port=12347,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Queue Review Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "queue-review",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "First task",
            "prompt": "Complete this before the next queued task",
        },
    ).json()
    first_start = client.post(f"/api/workspaces/tasks/{first_task['id']}/start", json={}).json()
    session_id = first_start["session_id"]
    session = workspace_manager.sessions[session_id]

    ready_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": first_task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert ready_response.status_code == 201
    assert pass_task_review(client, first_task["id"]).status_code == 201
    assert workspace_manager.tasks[first_task["id"]].status == WorkspaceTaskStatus.REVIEW
    assert workspace_manager.sessions[session_id].current_task_id == first_task["id"]

    reviewed_at = workspace_manager.tasks[first_task["id"]].reviewed_at
    assert reviewed_at is not None
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id=session.tab_id,
            tab_name="Queue Review Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail=None,
            tmux_session=session.tmux_session,
            last_changed_at=reviewed_at,
            sampled_at=reviewed_at + timedelta(seconds=1),
        )
    ]

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Second task",
            "prompt": "Run after the reviewed task is human-accepted",
        },
    ).json()
    second_start = client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={},
    )

    # REVIEW status (even after review_passed) holds the agent so the
    # implementation context is preserved for revisions or human rejection.
    # The new task gets queued onto the same agent without preempting it.
    assert second_start.status_code == 201
    started_second = second_start.json()
    assert started_second["status"] == "queued"
    assert started_second["session_id"] == session_id
    assert workspace_manager.tasks[first_task["id"]].status == WorkspaceTaskStatus.REVIEW
    assert workspace_manager.sessions[session_id].current_task_id == first_task["id"]

    # Once the human accepts the first task it moves to DONE, releasing the
    # agent and letting the queued second task start.
    accept_response = client.patch(
        f"/api/workspaces/tasks/{first_task['id']}",
        json={"status": "done"},
    )
    assert accept_response.status_code == 200
    assert workspace_manager.tasks[first_task["id"]].status == WorkspaceTaskStatus.DONE
    assert workspace_manager.tasks[second_task["id"]].status == WorkspaceTaskStatus.WORKING
    assert workspace_manager.sessions[session_id].current_task_id == second_task["id"]
    assert "Run after the reviewed task is human-accepted" in sent_messages[-1][1]


def test_reassigns_auto_queued_task_when_another_agent_becomes_free(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Auto-queued work should not stay stuck behind a held review if another agent frees up."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="queue-rebalance-tab",
        port=12348,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Queue Rebalance Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "queue-rebalance",
        },
    ).json()
    first_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    second_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First review task", "prompt": "Hold first agent"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{first_agent['id']}/reports",
        json={
            "task_id": first_task["id"],
            "state": "ready_for_review",
            "message": "Ready",
        },
    )
    assert pass_task_review(client, first_task["id"]).status_code == 201

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second review task", "prompt": "Hold second agent"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={"target_session_id": second_agent["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{second_agent['id']}/reports",
        json={
            "task_id": second_task["id"],
            "state": "ready_for_review",
            "message": "Ready",
        },
    )
    assert pass_task_review(client, second_task["id"]).status_code == 201

    queued_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Queued task", "prompt": "Run on the next available agent"},
    ).json()
    queued_start = client.post(
        f"/api/workspaces/tasks/{queued_task['id']}/start",
        json={},
    )

    assert queued_start.status_code == 201
    queued = queued_start.json()
    assert queued["status"] == "queued"
    assert queued["session_id"] == first_agent["id"]
    assert queued["dispatch_reason"] == "Queued behind existing workspace agent"

    accept_second = client.patch(
        f"/api/workspaces/tasks/{second_task['id']}",
        json={"status": "done"},
    )

    assert accept_second.status_code == 200
    rebalanced = workspace_manager.tasks[queued_task["id"]]
    assert rebalanced.status == WorkspaceTaskStatus.WORKING
    assert rebalanced.session_id == second_agent["id"]
    assert rebalanced.dispatch_reason == "Reassigned to newly available workspace agent"
    assert workspace_manager.sessions[first_agent["id"]].current_task_id == first_task["id"]
    assert workspace_manager.sessions[second_agent["id"]].current_task_id == queued_task["id"]
    assert "Run on the next available agent" in sent_messages[-1][1]


def test_reassigns_auto_queued_task_off_busy_working_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A task auto-queued behind a genuinely WORKING agent should migrate to a
    freshly available agent instead of starving until that one specific agent
    goes idle."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="queue-working-tab",
        port=12349,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Queue Working Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "queue-working",
        },
    ).json()
    first_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    # First agent picks up a task and stays WORKING (no live status sample flips
    # it back to idle), so the next task has nowhere free to land.
    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Long running task", "prompt": "Keep first agent busy"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    assert (
        workspace_manager.sessions[first_agent["id"]].runtime_status == AgentRuntimeStatus.WORKING
    )

    queued_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Queued task", "prompt": "Run on the next available agent"},
    ).json()
    queued_start = client.post(
        f"/api/workspaces/tasks/{queued_task['id']}/start",
        json={},
    )

    assert queued_start.status_code == 201
    queued = queued_start.json()
    assert queued["status"] == "queued"
    assert queued["session_id"] == first_agent["id"]
    assert queued["dispatch_reason"] == "Queued behind existing workspace agent"

    # A second agent comes online; a dispatch pass should migrate the queued
    # task off the still-WORKING first agent onto the now-idle second agent.
    second_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    dispatch_response = client.post(f"/api/workspaces/{workspace['id']}/dispatch")
    assert dispatch_response.status_code == 204

    rebalanced = workspace_manager.tasks[queued_task["id"]]
    assert rebalanced.status == WorkspaceTaskStatus.WORKING
    assert rebalanced.session_id == second_agent["id"]
    assert rebalanced.dispatch_reason == "Reassigned to newly available workspace agent"
    assert workspace_manager.sessions[first_agent["id"]].current_task_id == first_task["id"]
    assert workspace_manager.sessions[second_agent["id"]].current_task_id == queued_task["id"]
    assert "Run on the next available agent" in sent_messages[-1][1]


def test_reassigns_related_pinned_task_off_review_parked_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A related-task-pinned queued task must not starve forever behind an agent
    that is runtime-idle but still bound to a non-DONE review-parked task. When a
    different agent is free, the pinned task migrates to it instead of waiting
    indefinitely for a human to accept the parked review.

    This reproduces the live deadlock: the related-pinned task carried
    dispatch_reason="Related to task ..." (not "Queued behind existing workspace
    agent"), so the original rebalancer skipped it entirely; the assigned agent
    was idle on paper yet locked by a review-parked task, while other agents sat
    idle."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="related-pinned-tab",
        port=12352,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Related Pinned Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "related-pinned",
        },
    ).json()
    first_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    # First agent runs a task and gets it parked in REVIEW (review passed but not
    # human-accepted), so it stays runtime-idle yet bound to a non-DONE task.
    # _can_dispatch_to is False for it indefinitely, but _can_assign_or_queue_to
    # is True (it is holding an unresolved review task), so related-task dispatch
    # still pins new work to it.
    parked_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Review parked task", "prompt": "Hold first agent in review"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{parked_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{first_agent['id']}/reports",
        json={
            "task_id": parked_task["id"],
            "state": "ready_for_review",
            "message": "Ready",
        },
    )
    assert pass_task_review(client, parked_task["id"]).status_code == 201
    parked = workspace_manager.tasks[parked_task["id"]]
    assert parked.status == WorkspaceTaskStatus.REVIEW
    assert workspace_manager.sessions[first_agent["id"]].current_task_id == parked_task["id"]
    assert not workspace_manager._can_dispatch_to(workspace_manager.sessions[first_agent["id"]])

    # A follow-up task related to the parked one gets pinned to the same agent
    # for context continuity (dispatch_reason "Related to task ...").
    related_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Follow-up fix", "prompt": "Continue from the related task"},
    ).json()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={"related_task_id": parked_task["id"]},
    )

    assert related_start.status_code == 201
    queued = related_start.json()
    assert queued["status"] == "queued"
    assert queued["session_id"] == first_agent["id"]
    assert queued["dispatch_reason"] == f"Related to task {parked_task['id']}"

    # A second idle agent comes online; a dispatch pass should migrate the
    # related-pinned task off the review-parked first agent onto the free one.
    second_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    dispatch_response = client.post(f"/api/workspaces/{workspace['id']}/dispatch")
    assert dispatch_response.status_code == 204

    rebalanced = workspace_manager.tasks[related_task["id"]]
    assert rebalanced.status == WorkspaceTaskStatus.WORKING
    assert rebalanced.session_id == second_agent["id"]
    assert rebalanced.dispatch_reason == "Reassigned to newly available workspace agent"
    assert rebalanced.clear_context is True
    # The parked agent keeps holding its review task untouched.
    assert workspace_manager.sessions[first_agent["id"]].current_task_id == parked_task["id"]
    assert workspace_manager.sessions[second_agent["id"]].current_task_id == related_task["id"]
    assert "Continue from the related task" in sent_messages[-1][1]


def test_user_pinned_task_never_migrates_off_review_parked_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit operator target pin ("User selected target agent") must wait
    for exactly that agent and never migrate to a free one, even when the pinned
    agent is stuck holding a review-parked task. This guards the one
    dispatch_reason that stays non-migratable."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="user-pinned-tab",
        port=12353,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "User Pinned Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "user-pinned",
        },
    ).json()
    first_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    parked_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Review parked task", "prompt": "Hold first agent in review"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{parked_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{first_agent['id']}/reports",
        json={
            "task_id": parked_task["id"],
            "state": "ready_for_review",
            "message": "Ready",
        },
    )
    assert pass_task_review(client, parked_task["id"]).status_code == 201

    # Operator explicitly targets the busy first agent.
    pinned_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Operator pinned fix", "prompt": "Must run on first agent only"},
    ).json()
    pinned_start = client.post(
        f"/api/workspaces/tasks/{pinned_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    assert pinned_start.status_code == 201
    queued = pinned_start.json()
    assert queued["status"] == "queued"
    assert queued["session_id"] == first_agent["id"]
    assert queued["dispatch_reason"] == "User selected target agent"

    # A second idle agent comes online and a dispatch pass runs; the explicit
    # user pin must NOT migrate.
    second_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    dispatch_response = client.post(f"/api/workspaces/{workspace['id']}/dispatch")
    assert dispatch_response.status_code == 204

    held = workspace_manager.tasks[pinned_task["id"]]
    assert held.status == WorkspaceTaskStatus.QUEUED
    assert held.session_id == first_agent["id"]
    assert held.dispatch_reason == "User selected target agent"
    assert workspace_manager.sessions[second_agent["id"]].current_task_id is None


def test_dispatch_holds_agent_during_in_flight_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent stays bound to its task while reviewer is still running."""

    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="hold-review-tab",
        port=12351,
        sent_messages=sent_messages,
    )

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Hold Review Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "hold-review",
        },
    ).json()
    busy_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    free_agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()

    first_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "First task", "prompt": "Review held"},
    ).json()
    first_start = client.post(
        f"/api/workspaces/tasks/{first_task['id']}/start",
        json={"target_session_id": busy_agent["id"]},
    ).json()
    assert first_start["session_id"] == busy_agent["id"]

    # Agent reports ready_for_review; reviewer is now working but has not
    # produced a review_passed/failed verdict yet.
    ready_response = client.post(
        f"/api/workspaces/sessions/{busy_agent['id']}/reports",
        json={
            "task_id": first_task["id"],
            "state": "ready_for_review",
            "message": "Ready",
        },
    )
    assert ready_response.status_code == 201
    held_task = workspace_manager.tasks[first_task["id"]]
    assert held_task.review_requested_at is not None
    assert held_task.review_completed_at is None
    assert workspace_manager.sessions[busy_agent["id"]].current_task_id == first_task["id"]

    second_task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second task", "prompt": "Pick the free agent"},
    ).json()
    second_start = client.post(
        f"/api/workspaces/tasks/{second_task['id']}/start",
        json={},
    )

    assert second_start.status_code == 201
    started_second = second_start.json()
    assert started_second["status"] == "working"
    assert started_second["session_id"] == free_agent["id"]
    assert workspace_manager.sessions[busy_agent["id"]].current_task_id == first_task["id"]


def test_report_with_task_renames_non_orchestrator_session(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    renamed_tabs: list[tuple[str, str | None]] = []

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: object) -> TerminalTab:
        renamed_tabs.append((tab_id, name))
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12600,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Review Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "review",
        },
    )
    task_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Review assigned task",
            "prompt": "Review this change",
            "agent_type": "codex",
        },
    )
    now = datetime.now()
    workspace_manager.sessions["review-reviewer-1"] = ManagedSession(
        id="review-reviewer-1",
        workspace_id=workspace_response.json()["id"],
        task_id=None,
        tab_id="tab-reviewer",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Reviewer 1",
        branch=None,
        workspace_path=str(repo),
        tmux_session="claude-hub-tab-revi",
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        solo_mode=True,
        remote_forward_port=None,
        created_at=now,
        updated_at=now,
    )

    report_response = client.post(
        "/api/workspaces/sessions/review-reviewer-1/reports",
        json={
            "task_id": task_response.json()["id"],
            "state": "started",
            "message": "Started review",
        },
    )

    assert report_response.status_code == 201
    session = workspace_manager.sessions["review-reviewer-1"]
    assert session.role == WorkspaceSessionRole.WORKER
    assert session.title == "Review assigned task"
    assert session.current_task_id == task_response.json()["id"]
    assert renamed_tabs == [("tab-reviewer", "Review assigned task")]


def test_start_task_prefers_related_task_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"related{len(created_tabs) + 1}-tab"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12360 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Related Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "related",
        },
    )
    workspace_id = workspace_response.json()["id"]
    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    second_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()

    original_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Original task", "prompt": "Use the second agent"},
    ).json()
    original_start = client.post(
        f"/api/workspaces/tasks/{original_task['id']}/start",
        json={"target_session_id": second_agent["id"]},
    )
    assert original_start.status_code == 201
    assert original_start.json()["session_id"] == second_agent["id"]

    done_response = client.patch(
        f"/api/workspaces/tasks/{original_task['id']}",
        json={"status": "done"},
    )
    assert done_response.status_code == 200

    related_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Follow-up task",
            "prompt": "Continue with the related agent",
            "related_task_id": original_task["id"],
        },
    ).json()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={},
    )

    assert related_start.status_code == 201
    started_related = related_start.json()
    assert first_agent["id"] != second_agent["id"]
    assert started_related["status"] == "working"
    assert started_related["session_id"] == second_agent["id"]
    assert started_related["dispatch_reason"] == f"Related to task {original_task['id']}"
    assert sent_messages[-1][0] == "claude-hub-related2"
    assert "Continue with the related agent" in sent_messages[-1][1]


def test_related_task_clear_context_checkbox_sends_clear(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A task created with the 'Clear context' checkbox must send /clear even
    when it is dispatched via the related-task continuity path (which used to
    hardcode clear_context=False and silently drop the user's choice)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"clrctx{len(created_tabs) + 1}-tab"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12480 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_id = client.post(
        "/api/workspaces",
        json={
            "name": "Clear Context Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "clrctx",
        },
    ).json()["id"]
    agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()

    original_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Original task", "prompt": "Seed the agent"},
    ).json()
    original_start = client.post(
        f"/api/workspaces/tasks/{original_task['id']}/start",
        json={"target_session_id": agent["id"]},
    )
    assert original_start.status_code == 201
    client.patch(
        f"/api/workspaces/tasks/{original_task['id']}",
        json={"status": "done"},
    )

    # Related follow-up created with the "Clear context" checkbox ticked.
    related_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Follow-up task",
            "prompt": "Fresh context please",
            "related_task_id": original_task["id"],
            "clear_context": True,
        },
    ).json()
    sent_messages.clear()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={},
    )

    assert related_start.status_code == 201
    started = related_start.json()
    assert started["status"] == "working"
    assert started["session_id"] == agent["id"]
    assert started["dispatch_reason"] == f"Related to task {original_task['id']}"
    assert started["clear_context"] is True
    # /clear must be sent before the task prompt on this continuity path.
    assert sent_messages[0][1] == "/clear"
    assert "Fresh context please" in sent_messages[1][1]


def test_start_task_skips_offline_related_task_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"offline-related{len(created_tabs) + 1}"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12400 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Offline Related Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "offrel",
        },
    ).json()
    workspace_id = workspace["id"]
    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    second_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()

    original_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Original offline context", "prompt": "Use first agent"},
    ).json()
    original_start = client.post(
        f"/api/workspaces/tasks/{original_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    assert original_start.status_code == 201
    assert (
        client.patch(
            f"/api/workspaces/tasks/{original_task['id']}",
            json={"status": "done"},
        ).status_code
        == 200
    )

    workspace_manager.sessions[first_agent["id"]] = workspace_manager.sessions[
        first_agent["id"]
    ].model_copy(
        update={
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    related_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Related task",
            "prompt": "Continue without the offline agent",
            "related_task_id": original_task["id"],
        },
    ).json()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={},
    )

    assert related_start.status_code == 201
    started_related = related_start.json()
    assert started_related["status"] == "working"
    assert started_related["session_id"] == second_agent["id"]
    assert started_related["dispatch_reason"] == "Only one workspace agent is available"


def test_tmux_pending_input_detection_matches_codex_paste_prompt() -> None:
    message = "New workspace task assigned.\n\nTask description"

    assert workspace_manager._message_still_in_input(
        "\n› N[Pasted Content 1360 chars]\n  gpt-5.5 medium · ~/repo\n",
        message,
    )
    assert workspace_manager._message_still_in_input(
        "\n› New workspace task assigned.\n\n  Task description\n",
        message,
    )
    assert not workspace_manager._message_still_in_input(
        "\n› N[Pasted Content 1360 chars]\n\n• Working\n",
        message,
    )
    assert not workspace_manager._message_still_in_input(
        "\n❯ /clear\n  ⎿ \xa0(no content)\n\n❯\xa0\n",
        "/clear",
    )
    assert workspace_manager._message_still_in_input(
        "\n› N[Pasted Content 1360 chars]\n\n  ⎿ \xa0(no content)\n",
        message,
    )
    assert workspace_manager._message_still_in_input(
        "\n› Ne[Pasted Content 10513 chars]\n\n  gpt-5.5 high · ~/repo\n" + ("\n" * 30),
        message,
    )


def test_send_tmux_message_pastes_with_bracketed_paste_flags(
    monkeypatch: MonkeyPatch,
) -> None:
    """A multi-line prompt must be pasted as one bracketed paste with newlines
    preserved.

    Regression for the codex "新建 agent 反复喂初始输入" bug: ``tmux paste-buffer``
    with no flags replaces every LF with CR and emits no bracketed-paste control
    codes, so a codex TUI (bracketed-paste mode on) reads each CR as Enter and
    submits each bootstrap line separately, piling up "Queued follow-up inputs".
    Pasting with ``-p -r`` wraps the buffer in ESC[200~ … ESC[201~ and keeps the
    newlines as newlines, so it lands as a single composer entry and the
    existing single Enter submits it.
    """
    tmux_calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        tmux_calls.append(args)

    # The pasted prompt would otherwise look "still pending"; short-circuit the
    # submit verification so the test focuses on the paste invocation.
    async def fake_submit(_tmux_session: str, _message: str) -> None:
        return None

    monkeypatch.setattr(workspace_manager, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr(workspace_manager, "_submit_tmux_message", fake_submit)

    asyncio.run(
        workspace_manager._send_tmux_message(
            "claude-hub-deadbeef",
            "Workspace: H2O\nSession: cb-agent-9\nRuntime target: local",
        )
    )

    paste_calls = [call for call in tmux_calls if call and call[0] == "paste-buffer"]
    assert len(paste_calls) == 1
    paste_call = paste_calls[0]
    # Both flags are required: -p alone still converts LF->CR; -r alone omits the
    # bracketed-paste markers. Assert both are present and the pane is targeted.
    assert "-p" in paste_call
    assert "-r" in paste_call
    assert "-t" in paste_call
    assert "claude-hub-deadbeef" in paste_call
    # The buffer must be loaded before it is pasted.
    assert any(call and call[0] == "load-buffer" for call in tmux_calls)
    load_index = next(i for i, call in enumerate(tmux_calls) if call and call[0] == "load-buffer")
    paste_index = next(i for i, call in enumerate(tmux_calls) if call and call[0] == "paste-buffer")
    assert load_index < paste_index


def test_tmux_pending_input_detection_matches_cursor_paste_prompt() -> None:
    message = "New workspace task assigned.\n\nTask description"

    cursor_paste_screen = (
        "Cursor Agent\n"
        "v2026.05.20-2b5dd59\n"
        "Use /auto-run to skip all approvals.\n"
        "\n"
        "→ [Pasted text #1 +39 lines]\n"
        "\n"
        "Opus 4.7 1M High Thinking · MAX\n"
        "~/Projects/codex_workspace · main\n"
    )
    assert workspace_manager._message_still_in_input(cursor_paste_screen, message)

    cursor_message_prefix = (
        "→ New workspace task assigned.\n"
        "\n"
        "  Task description\n"
        "\n"
        "Opus 4.7 1M High Thinking · MAX\n"
    )
    assert workspace_manager._message_still_in_input(cursor_message_prefix, message)

    cursor_after_submit = (
        "→ [Pasted text #1 +39 lines]\n" "\n" "● Working\n" "Opus 4.7 1M High Thinking · MAX\n"
    )
    assert not workspace_manager._message_still_in_input(cursor_after_submit, message)


def test_agent_input_ready_recognizes_cursor_banner() -> None:
    cursor_banner = (
        "Cursor Agent\n" "v2026.05.20-2b5dd59\n" "Use /auto-run to skip all approvals.\n"
    )
    assert workspace_manager._agent_input_ready(cursor_banner)
    assert not workspace_manager._agent_input_ready("loading...\nplease wait\n")


def test_tmux_pending_input_detection_matches_claude_pasted_text_placeholder() -> None:
    """Claude Code compresses multi-line paste into `[Pasted text +N lines]`.

    The Codex-era placeholder check only matched `[Pasted Content`, so a
    Claude paste that was still sitting in the input would be reported as
    submitted and the C-m retry loop would not fire. Lock in coverage that
    the broadened `[Pasted text` check now catches Claude's format too.
    """
    message = "New workspace task assigned.\n\nTask description"

    claude_paste_pending = (
        '  Try "create a util logging.py that..."\n'
        "\n"
        "> [Pasted text +57 lines]\n"
        "\n"
        "  ? for shortcuts\n"
    )
    assert workspace_manager._message_still_in_input(claude_paste_pending, message)

    claude_after_submit = (
        "> [Pasted text +57 lines]\n" "\n" "⏺ Working on it...\n" "  ? for shortcuts\n"
    )
    assert not workspace_manager._message_still_in_input(claude_after_submit, message)


def test_dispatch_workspace_serializes_concurrent_dispatches(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-race",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12390,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_update_tab(
        tab_id: str,
        name: Optional[str] = None,
        **_: object,
    ) -> TerminalTab:
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12391,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    async def fake_send_tmux_message(_tmux_session: str, message: str) -> None:
        sent_messages.append(message)
        await asyncio.sleep(0.02)

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Dispatch Race", "path": str(repo), "session_prefix": "race"},
    ).json()
    agent = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    sent_messages.clear()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Race task", "prompt": "Do the work"},
    ).json()

    queued_at = datetime.now()
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.QUEUED,
            "session_id": agent["id"],
            "clear_context": True,
            "queued_at": queued_at,
            "updated_at": queued_at,
        }
    )

    async def run_concurrent_dispatch() -> None:
        await asyncio.gather(
            workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False),
            workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False),
        )

    asyncio.run(run_concurrent_dispatch())

    assert sent_messages[0] == "/clear"
    assert len(sent_messages) == 2
    assert sent_messages[1].count("New workspace task assigned.") == 1
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.WORKING


def test_auto_continue_ignores_stale_interruption_before_latest_continue() -> None:
    output = "\n".join(
        [
            "API Error: 400 unknown error",
            "",
            "› please continue",
            "",
            "⏺ 没有新指令，等待用户输入。",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) is None


def test_auto_continue_detects_current_interruption_segment() -> None:
    output = "\n".join(
        [
            "› New workspace task assigned",
            "",
            "⏺ Bash(command)",
            "  ⎿ API Error: 400 unknown error",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) == "api error"


def test_auto_continue_detects_missing_final_report_segment() -> None:
    output = "\n".join(
        [
            "› New workspace task assigned",
            "",
            "Implemented the fix.",
            "Validation: backend tests passed.",
            "Risks: no known follow-up risk.",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) is None
    assert workspace_manager._auto_continue_completion_reason(output) == "validation:"


def test_background_monitor_auto_continues_interrupted_idle_working_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-api-error",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12358,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "› New workspace task assigned",
                "",
                '⏺ Bash(ssh merlin_dev "grep -n _rr_counter file")',
                "  ⎿ API Error: 400 unknown error",
                "",
                "❯ ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "API Retry Repo", "path": str(repo), "session_prefix": "retry"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Retry task", "prompt": "Handle flaky API"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-api-error",
            tab_name="API Retry Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-api-",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"
    assert board["sessions"][0]["auto_continue_task_id"] == started["id"]
    assert board["sessions"][0]["auto_continue_attempts"] == 0

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "claude-hub-tab-api-"
    assert "continue from the last actionable step" in sent_messages[0][1]
    assert "ready_for_review or completed report" in sent_messages[0][1]
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.WORKING
    assert session.auto_continue_task_id == started["id"]
    assert session.auto_continue_attempts == 1


def test_non_interrupted_idle_working_agent_is_not_auto_continued(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-idle-no-error",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12362,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "3 tasks (2 done, 1 in progress, 0 open)\n› "

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Idle No Error Repo", "path": str(repo), "session_prefix": "idle"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Idle task", "prompt": "Handle a normal stopped task"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-idle-no-error",
            tab_name="Idle No Error Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-idle",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"
    assert board["sessions"][0]["auto_continue_task_id"] == started["id"]
    assert board["sessions"][0]["auto_continue_attempts"] == 0

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert sent_messages == []
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.IDLE
    assert session.auto_continue_task_id == started["id"]
    assert session.auto_continue_attempts == 0


def test_completed_idle_working_agent_is_prompted_to_report(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-report-missing",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12365,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "Implemented status transition fix.",
                "Validation: tests passed.",
                "Risks: no known risk.",
                "",
                "› ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Report Missing Repo", "path": str(repo), "session_prefix": "report"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Report task", "prompt": "Finish but forget report"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-report-missing",
            tab_name="Report Missing Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-report",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "claude-hub-tab-repo"
    assert "no workspace report was recorded" in sent_messages[0][1]
    assert "changed_files, validation, risks" in sent_messages[0][1]
    assert "acceptance_check evidence" in sent_messages[0][1]
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.WORKING
    assert session.auto_continue_attempts == 1


def test_idle_working_agent_with_review_in_flight_is_not_prompted(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-review-in-flight",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12366,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "Implemented status transition fix.",
                "Validation: tests passed.",
                "Risks: no known risk.",
                "",
                "› ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Review In Flight Repo", "path": str(repo), "session_prefix": "rvw"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Review task", "prompt": "Finish and request review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    review_requested_at = datetime.now() - timedelta(seconds=5)
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={
            "review_requested_at": review_requested_at,
            "review_completed_at": None,
            "updated_at": review_requested_at,
        }
    )

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-review-in-flight",
            tab_name="Review In Flight Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-rvw-",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert sent_messages == []
    assert session.auto_continue_attempts == 0


def test_bound_reviewer_on_reopened_task_is_not_auto_prompted(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reviewer bound to a reopened WORKING task must not be auto-prompted.

    Regression for "后面那个reviewer的上报始终收不到完成信号": after a
    ``review_failed`` reopen the reviewer session intentionally keeps
    ``current_task_id`` on the task (so the same reviewer handles the next
    cycle), but the task is now WORKING and owned by the *worker*. The monitor
    used to treat the idle reviewer as a worker owing a report and endlessly
    auto-prompted it (action=report_missing); the reviewer re-posted its verdict,
    which was dropped as a stale duplicate, stranding the task until the fallback
    reaper fired ~5 min later. The monitor must only auto-continue the worker
    (``task.session_id``), never the bound reviewer.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        suffix = "reviewer" if workspace_role == WorkspaceSessionRole.REVIEWER else "worker"
        return TerminalTab(
            id=f"tab-bound-{suffix}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12367,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        # Completion-shaped output proven to trigger the auto report-missing
        # prompt (same shape as test_completed_idle_working_agent_is_prompted_to_report).
        # If the reviewer guard were absent, the bound reviewer would be prompted.
        return "\n".join(
            [
                "Implemented status transition fix.",
                "Validation: tests passed.",
                "Risks: no known risk.",
                "",
                "› ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Bound Reviewer Repo", "path": str(repo), "session_prefix": "br"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Reopened task", "prompt": "Address the review feedback"},
    ).json()
    started = client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    ).json()

    # Simulate the post-``review_failed`` reopen: task is WORKING and owned by
    # the worker, while a separate reviewer session stays bound to it.
    now = datetime.now()
    reviewer = ManagedSession(
        id="br-reviewer-1",
        workspace_id=workspace["id"],
        task_id=task["id"],
        tab_id="tab-bound-reviewer",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=task["id"],
        queued_count=0,
        title="Bound Reviewer 1",
        branch=None,
        workspace_path=str(repo),
        tmux_session="claude-hub-tab-bound-reviewer",
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        solo_mode=True,
        remote_forward_port=None,
        created_at=now,
        updated_at=now,
    )
    workspace_manager.sessions[reviewer.id] = reviewer
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "review_session_id": reviewer.id,
            "review_requested_at": now - timedelta(seconds=30),
            "review_completed_at": now - timedelta(seconds=20),
            "updated_at": now,
        }
    )
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-bound-reviewer",
            tab_name="Bound Reviewer 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="reviewer prompt visible",
            tmux_session="claude-hub-tab-bound-reviewer",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    # The bound reviewer must not be prompted to report.
    reviewer_after = workspace_manager.sessions[reviewer.id]
    assert sent_messages == []
    assert reviewer_after.auto_continue_attempts == 0


def test_monitor_surfaces_worker_prompt_stuck_in_input(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "› New workspace task assigned.",
                "",
                "Workspace: Stuck Worker Repo",
                "Task ID: task-still-in-input",
            ]
        )

    submitted_keys: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        submitted_keys.append(args)

    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="tab-worker-stuck",
        port=12370,
        sent_messages=sent_messages,
    )
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)
    monkeypatch.setattr(workspace_manager, "_run_tmux", fake_run_tmux)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Stuck Worker Repo", "path": str(repo), "session_prefix": "stuckw"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Worker stuck", "prompt": "Start this task"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    stale_started_at = datetime.now() - timedelta(seconds=30)
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={"started_at": stale_started_at, "updated_at": stale_started_at}
    )
    session = workspace_manager.sessions[started["session_id"]]
    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id=session.tab_id,
            tab_name="Stuck Worker Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="prompt pending",
            tmux_session=session.tmux_session,
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    asyncio.run(workspace_manager._refresh_session_statuses(workspace["id"]))

    updated_task = workspace_manager.tasks[started["id"]]
    updated_session = workspace_manager.sessions[started["session_id"]]
    assert updated_task.status == WorkspaceTaskStatus.WORKING
    assert updated_session.runtime_status == AgentRuntimeStatus.WORKING
    assert updated_session.prompt_retry_task_id == started["id"]
    assert submitted_keys == [("send-keys", "-t", session.tmux_session, "C-m")]
    assert sent_messages == []

    retry_at = datetime.now() - timedelta(seconds=30)
    workspace_manager.sessions[started["session_id"]] = updated_session.model_copy(
        update={"prompt_retry_attempted_at": retry_at, "updated_at": retry_at}
    )
    asyncio.run(workspace_manager._refresh_session_statuses(workspace["id"]))

    updated_task = workspace_manager.tasks[started["id"]]
    updated_session = workspace_manager.sessions[started["session_id"]]
    report = list(workspace_manager.reports.values())[-1]
    assert updated_task.status == WorkspaceTaskStatus.REVIEW
    assert updated_session.runtime_status == AgentRuntimeStatus.ATTENTION
    assert report.state.value == "needs_input"
    assert report.risk_level == "prompt_dispatch_stalled"
    assert "terminal input box" in report.message
    assert len(submitted_keys) == 1


def test_monitor_surfaces_reviewer_prompt_stuck_in_input(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "› Review workspace task.",
                "",
                "Workspace: Stuck Reviewer Repo",
                "Task ID: task-review-still-in-input",
            ]
        )

    submitted_keys: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        submitted_keys.append(args)

    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="tab-reviewer-stuck",
        port=12371,
        sent_messages=sent_messages,
    )
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)
    monkeypatch.setattr(workspace_manager, "_run_tmux", fake_run_tmux)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Stuck Reviewer Repo", "path": str(repo), "session_prefix": "stuckr"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Reviewer stuck", "prompt": "Complete and review this task"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": started["id"],
            "state": "completed",
            "message": "Done; request review",
            "review_decision": "request",
            "review_reason": "Exercise reviewer dispatch",
        },
    )
    sent_messages.clear()

    review_session_id = workspace_manager.tasks[started["id"]].review_session_id
    assert review_session_id is not None
    stale_review_requested_at = datetime.now() - timedelta(seconds=30)
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={
            "review_requested_at": stale_review_requested_at,
            "updated_at": stale_review_requested_at,
        }
    )
    reviewer = workspace_manager.sessions[review_session_id]
    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id=reviewer.tab_id,
            tab_name="Stuck Reviewer Repo Reviewer 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="review prompt pending",
            tmux_session=reviewer.tmux_session,
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    asyncio.run(workspace_manager._refresh_session_statuses(workspace["id"]))

    updated_task = workspace_manager.tasks[started["id"]]
    updated_reviewer = workspace_manager.sessions[review_session_id]
    assert updated_task.status == WorkspaceTaskStatus.REVIEW
    assert updated_reviewer.runtime_status == AgentRuntimeStatus.WORKING
    assert updated_reviewer.prompt_retry_task_id == started["id"]
    assert submitted_keys == [("send-keys", "-t", reviewer.tmux_session, "C-m")]
    assert sent_messages == []

    retry_at = datetime.now() - timedelta(seconds=30)
    workspace_manager.sessions[review_session_id] = updated_reviewer.model_copy(
        update={"prompt_retry_attempted_at": retry_at, "updated_at": retry_at}
    )
    asyncio.run(workspace_manager._refresh_session_statuses(workspace["id"]))

    updated_task = workspace_manager.tasks[started["id"]]
    updated_reviewer = workspace_manager.sessions[review_session_id]
    report = list(workspace_manager.reports.values())[-1]
    assert updated_task.status == WorkspaceTaskStatus.REVIEW
    assert updated_reviewer.runtime_status == AgentRuntimeStatus.ATTENTION
    assert report.state.value == "review_needs_input"
    assert report.risk_level == "prompt_dispatch_stalled"
    assert "Review" in report.message or "review" in report.message
    assert len(submitted_keys) == 1


def test_fallback_reaper_grace_skips_recently_dispatched_idle_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reviewer just got the prompt but hasn't started typing yet — terminal
    classifier briefly reports IDLE. The fallback reaper must not treat this
    as a stuck dispatch and re-fire ``ready_for_review`` repeatedly. Once the
    grace window elapses without activity, redispatch is allowed again."""
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="tab-reaper-grace",
        port=12372,
        sent_messages=sent_messages,
    )
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reaper Grace Repo", "path": str(repo), "session_prefix": "reapg"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Slow reviewer", "prompt": "Finish then request review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": started["id"],
            "state": "completed",
            "message": "Done; please review",
            "review_decision": "request",
            "review_reason": "exercise reaper grace",
        },
    )

    review_session_id = workspace_manager.tasks[started["id"]].review_session_id
    assert review_session_id is not None
    initial_attempts = workspace_manager.tasks[started["id"]].review_attempts
    sent_messages.clear()

    # Simulate "slow first token": reviewer was just dispatched (within grace)
    # but the terminal classifier reports IDLE because no output has streamed
    # yet. _reviewer_is_active returns False, so without the grace the
    # fallback reaper would re-dispatch.
    just_now = datetime.now()
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={
            "review_requested_at": just_now,
            "updated_at": just_now,
        }
    )
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "runtime_status": AgentRuntimeStatus.IDLE,
            "last_activity_at": just_now,
            "updated_at": just_now,
        }
    )

    asyncio.run(workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False))

    after_grace_task = workspace_manager.tasks[started["id"]]
    assert (
        after_grace_task.review_attempts == initial_attempts
    ), "fallback reaper should not redispatch within grace window"
    assert all(
        "fallback reaper" not in msg.lower() for _, msg in sent_messages
    ), "reviewer should not see a re-dispatched prompt during grace"

    # Push the dispatch + last activity past the grace window — the reaper
    # should now treat the reviewer as genuinely stuck and redispatch.
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 5
    )
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={"review_requested_at": stale, "updated_at": stale}
    )
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "runtime_status": AgentRuntimeStatus.IDLE,
            "status": ManagedSessionStatus.IDLE,
            "task_id": None,
            "current_task_id": None,
            "last_activity_at": stale,
            "updated_at": stale,
        }
    )

    asyncio.run(workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False))

    redispatched_task = workspace_manager.tasks[started["id"]]
    assert (
        redispatched_task.review_attempts == initial_attempts + 1
    ), "fallback reaper should redispatch once dispatch grace has elapsed"


def test_fallback_reaper_does_not_redispatch_already_judged_round(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: the fallback reaper must not re-dispatch a review whose
    current round already has a verdict (``reviewed_cycle >= review_cycle``).

    This reproduces the infinite re-dispatch loop ("pass bug"): once a round is
    judged, ``_request_task_review`` clears ``review_completed_at`` /
    ``human_acceptance_requested_at`` without bumping ``review_cycle``, so the
    reviewer's re-emitted ``review_passed`` is stamped at the already-judged
    cycle and dropped as a closed-round echo by ``_reviewer_verdict_actionable``.
    ``review_completed_at`` is never rewritten, ``review_in_flight`` stays true,
    and absent the ``current_round_has_verdict`` guard the reaper re-fires every
    loop forever (observed in the field: ``review_passed`` applied 14×, zero
    diff). The guard must skip this task while still recovering a genuinely
    stuck *unjudged* round (asserted at the end)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []

    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="tab-reaper-judged",
        port=12379,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reaper Judged Repo", "path": str(repo), "session_prefix": "reapj"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Already judged", "prompt": "Finish then request review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": started["id"],
            "state": "ready_for_review",
            "message": "Done; please review",
        },
    )

    pass_resp = pass_task_review(client, started["id"])
    assert pass_resp.status_code == 201

    judged_task = workspace_manager.tasks[started["id"]]
    review_session_id = judged_task.review_session_id
    assert review_session_id is not None
    # Pass advanced reviewed_cycle to the current round: the round is sealed.
    assert judged_task.reviewed_cycle >= judged_task.review_cycle, (
        f"precondition: round must be judged; review_cycle={judged_task.review_cycle} "
        f"reviewed_cycle={judged_task.reviewed_cycle}"
    )
    initial_attempts = judged_task.review_attempts

    # Drive the task into the self-sustaining limbo state the reaper itself
    # produces: status=REVIEW, a stale review_requested_at, verdict timestamps
    # cleared, but reviewed_cycle still == review_cycle (round already judged).
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30
    )
    workspace_manager.tasks[started["id"]] = judged_task.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            "review_requested_at": stale,
            "review_completed_at": None,
            "human_acceptance_requested_at": None,
            "updated_at": stale,
        }
    )
    # Reviewer is idle/unbound so _reviewer_is_active() returns False — without
    # the guard, condition 1 (review_in_flight + inactive reviewer) would fire.
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": None,
            "current_task_id": None,
            "last_activity_at": stale,
            "updated_at": stale,
        }
    )

    review_reports_before = [
        r
        for r in workspace_manager.reports.values()
        if r.task_id == started["id"] and r.review_decision is not None
    ]
    sent_messages.clear()

    asyncio.run(workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False))

    looped_task = workspace_manager.tasks[started["id"]]
    assert looped_task.review_attempts == initial_attempts, (
        "fallback reaper must NOT re-dispatch a round that already has a verdict "
        f"(loop bug); attempts went {initial_attempts} -> {looped_task.review_attempts}"
    )
    assert all(
        "fallback reaper" not in msg.lower() for _, msg in sent_messages
    ), "reviewer must not be re-prompted for an already-judged round"
    review_reports_after = [
        r
        for r in workspace_manager.reports.values()
        if r.task_id == started["id"] and r.review_decision is not None
    ]
    assert len(review_reports_after) == len(review_reports_before), (
        "fallback reaper must not spawn a new review trigger report for a sealed "
        f"round; before={len(review_reports_before)} after={len(review_reports_after)}"
    )

    # Precision check: a genuinely stuck *unjudged* round (reviewed_cycle behind
    # review_cycle) must STILL be recovered, so the guard is not over-broad.
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={
            "reviewed_cycle": looped_task.review_cycle - 1,
            "review_requested_at": stale,
            "review_completed_at": None,
            "human_acceptance_requested_at": None,
            "updated_at": stale,
        }
    )
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": None,
            "current_task_id": None,
            "last_activity_at": stale,
            "updated_at": stale,
        }
    )

    asyncio.run(workspace_manager.dispatch_workspace(workspace["id"], refresh_sessions=False))

    recovered_task = workspace_manager.tasks[started["id"]]
    assert recovered_task.review_attempts == initial_attempts + 1, (
        "fallback reaper must still recover a genuinely stuck UNjudged round "
        f"(reviewed_cycle < review_cycle); attempts={recovered_task.review_attempts}"
    )


def test_reviewer_dispatch_stuck_predicate() -> None:
    """Unit coverage for the reaper-only ``_reviewer_dispatch_stuck`` predicate:
    a reviewer bound to THIS task and not stopped is NEVER considered stuck
    regardless of IDLE; missing/stopped/unbound/cross-bound IS stuck."""
    now = datetime.now()

    def make_task(review_session_id: str | None) -> WorkspaceTask:
        return WorkspaceTask(
            id="task-x",
            workspace_id="ws-x",
            title="t",
            prompt="p",
            agent_type=AgentType.CLAUDE,
            status=WorkspaceTaskStatus.REVIEW,
            created_at=now,
            updated_at=now,
            review_session_id=review_session_id,
        )

    def make_reviewer(
        *,
        status: ManagedSessionStatus = ManagedSessionStatus.IDLE,
        runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE,
        task_id: str | None = "task-x",
    ) -> ManagedSession:
        return ManagedSession(
            id="rev-x",
            workspace_id="ws-x",
            tab_id="tab-x",
            title="Reviewer X",
            workspace_path="/tmp/ws-x",
            tmux_session="claude-hub-revx",
            role=WorkspaceSessionRole.REVIEWER,
            agent_type=AgentType.CLAUDE,
            status=status,
            runtime_status=runtime_status,
            task_id=task_id,
            current_task_id=task_id,
            created_at=now,
            updated_at=now,
        )

    wm = workspace_manager

    # No review_session_id -> stuck.
    assert wm._reviewer_dispatch_stuck(make_task(None)) is True

    # Bound + IDLE reviewer -> NOT stuck (the regression we are fixing).
    wm.sessions["rev-x"] = make_reviewer()
    try:
        assert wm._reviewer_dispatch_stuck(make_task("rev-x")) is False
        # Missing session -> stuck.
        del wm.sessions["rev-x"]
        assert wm._reviewer_dispatch_stuck(make_task("rev-x")) is True
        # Stopped -> stuck.
        wm.sessions["rev-x"] = make_reviewer(status=ManagedSessionStatus.STOPPED)
        assert wm._reviewer_dispatch_stuck(make_task("rev-x")) is True
        # Cross-bound to a different task -> stuck.
        wm.sessions["rev-x"] = make_reviewer(task_id="other-task")
        assert wm._reviewer_dispatch_stuck(make_task("rev-x")) is True
    finally:
        wm.sessions.pop("rev-x", None)


def _setup_review_in_flight_task(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    *,
    tab_id: str,
    port: int,
    name: str,
    prefix: str,
    sent_messages: list[tuple[str, str]],
) -> tuple[TestClient, str, str, str]:
    """Drive a task to review-in-flight with a bound reviewer, then push the
    dispatch past the reaper grace window. Returns (client, workspace_id,
    task_id, review_session_id) ready for reaper assertions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_workspace_terminal(
        monkeypatch, repo, tab_id=tab_id, port=port, sent_messages=sent_messages
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": name, "path": str(repo), "session_prefix": prefix},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": name, "prompt": "Finish then request review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": started["id"],
            "state": "ready_for_review",
            "message": "Done; please review",
            "review_decision": "request",
            "review_reason": "exercise reaper",
        },
    )

    review_session_id = workspace_manager.tasks[started["id"]].review_session_id
    assert review_session_id is not None

    # Push review_requested_at + reviewer last activity past the dispatch grace
    # so only the stuck-detection predicate decides whether to re-dispatch.
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30
    )
    workspace_manager.tasks[started["id"]] = workspace_manager.tasks[started["id"]].model_copy(
        update={"review_requested_at": stale, "updated_at": stale}
    )
    return client, workspace["id"], started["id"], review_session_id


def test_fallback_reaper_keeps_bound_idle_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for the duplicate "fallback reaper" card: a reviewer that is
    bound to THIS task and merely IDLE (reading/thinking over a large review
    prompt past the grace window) must NOT be re-dispatched. The terminal
    classifier reports IDLE between bursts of model output, but the reviewer is
    genuinely working — re-dispatching produced a confusing second
    ``ready_for_review`` report."""
    sent_messages: list[tuple[str, str]] = []
    client, workspace_id, task_id, review_session_id = _setup_review_in_flight_task(
        monkeypatch,
        tmp_path,
        tab_id="tab-reaper-bound-idle",
        port=12381,
        name="Bound idle reviewer",
        prefix="reapbi",
        sent_messages=sent_messages,
    )

    initial_attempts = workspace_manager.tasks[task_id].review_attempts
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30
    )
    # Reviewer is bound to the task but IDLE with stale activity (silent think).
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": task_id,
            "current_task_id": task_id,
            "last_activity_at": stale,
            "updated_at": stale,
        }
    )

    # Input box is empty — the reviewer already submitted the prompt and is
    # working. The backstop must therefore NOT fire.
    async def fake_capture_output(_tmux_session: str) -> str:
        return "⏺ Reviewing the change...\n  ? for shortcuts\n"

    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)
    sent_messages.clear()

    asyncio.run(workspace_manager.dispatch_workspace(workspace_id, refresh_sessions=False))

    after = workspace_manager.tasks[task_id]
    assert after.review_attempts == initial_attempts, (
        "fallback reaper must NOT re-dispatch a reviewer bound to this task and "
        f"merely IDLE; attempts {initial_attempts} -> {after.review_attempts}"
    )
    assert all(
        "fallback reaper" not in msg.lower() for _, msg in sent_messages
    ), "a genuinely-working reviewer must not be re-prompted"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"__remove__": True}, id="missing"),
        pytest.param({"status": ManagedSessionStatus.STOPPED}, id="stopped"),
    ],
)
def test_fallback_reaper_redispatches_unavailable_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    mutation: dict,
) -> None:
    """The reaper must still recover a genuinely failed dispatch: reviewer
    session missing or STOPPED. (The cross-bound case is covered by the
    ``_review_dispatch_failed`` predicate unit test — at the dispatch-loop
    level ``_cleanup_stale_reviewer_assignments`` rewrites a cross-bound
    reviewer and refreshes its activity, so redispatch happens on the next
    loop rather than immediately.)"""
    sent_messages: list[tuple[str, str]] = []
    client, workspace_id, task_id, review_session_id = _setup_review_in_flight_task(
        monkeypatch,
        tmp_path,
        tab_id="tab-reaper-unavail",
        port=12382,
        name="Unavailable reviewer",
        prefix="reapun",
        sent_messages=sent_messages,
    )

    initial_attempts = workspace_manager.tasks[task_id].review_attempts
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30
    )
    if mutation.get("__remove__"):
        del workspace_manager.sessions[review_session_id]
    else:
        reviewer = workspace_manager.sessions[review_session_id]
        workspace_manager.sessions[review_session_id] = reviewer.model_copy(
            update={
                "runtime_status": AgentRuntimeStatus.IDLE,
                "last_activity_at": stale,
                "updated_at": stale,
                **mutation,
            }
        )
    sent_messages.clear()

    asyncio.run(workspace_manager.dispatch_workspace(workspace_id, refresh_sessions=False))

    after = workspace_manager.tasks[task_id]
    assert after.review_attempts == initial_attempts + 1, (
        "fallback reaper must re-dispatch when the reviewer is unavailable; "
        f"attempts {initial_attempts} -> {after.review_attempts}"
    )


def test_fallback_reaper_redispatches_when_prompt_still_pending(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Backstop: a reviewer bound to the task but whose review prompt never
    left the tmux input box (send raised after binding) is still IDLE forever
    under the strict predicate. The input-box backstop must recover it."""
    sent_messages: list[tuple[str, str]] = []
    client, workspace_id, task_id, review_session_id = _setup_review_in_flight_task(
        monkeypatch,
        tmp_path,
        tab_id="tab-reaper-pending",
        port=12383,
        name="Pending prompt reviewer",
        prefix="reappp",
        sent_messages=sent_messages,
    )

    initial_attempts = workspace_manager.tasks[task_id].review_attempts
    stale = datetime.now() - timedelta(
        seconds=workspace_module.REVIEW_REAPER_DISPATCH_GRACE_SECONDS + 30
    )
    reviewer = workspace_manager.sessions[review_session_id]
    workspace_manager.sessions[review_session_id] = reviewer.model_copy(
        update={
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": task_id,
            "current_task_id": task_id,
            "last_activity_at": stale,
            "updated_at": stale,
        }
    )

    # The review prompt ("Review workspace task.") is still sitting unsent in
    # the input box — _message_still_in_input must report it as pending.
    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n› Review workspace task.\n\n  ? for shortcuts\n"

    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)
    sent_messages.clear()

    asyncio.run(workspace_manager.dispatch_workspace(workspace_id, refresh_sessions=False))

    after = workspace_manager.tasks[task_id]
    assert after.review_attempts == initial_attempts + 1, (
        "fallback reaper backstop must re-dispatch when the review prompt is "
        f"still pending in the input box; attempts {initial_attempts} -> {after.review_attempts}"
    )


def test_interrupted_idle_working_agent_auto_continue_stops_after_limit(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-api-limit",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12359,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "API Error: connection reset by peer\n\n› "

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "API Limit Repo", "path": str(repo), "session_prefix": "limit"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Limit task", "prompt": "Handle repeated API failures"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session = workspace_manager.sessions[started["session_id"]]
    workspace_manager.sessions[session.id] = session.model_copy(
        update={
            "auto_continue_task_id": started["id"],
            "auto_continue_attempts": 10,
        }
    )
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-api-limit",
            tab_name="API Limit Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-api-",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    task_after_limit = workspace_manager.tasks[started["id"]]
    assert len(sent_messages) == 2
    assert "independent reviewer agent" in sent_messages[0][1]
    assert "Review workspace task" in sent_messages[1][1]
    assert task_after_limit.status == WorkspaceTaskStatus.REVIEW
    assert task_after_limit.session_id == started["session_id"]
    assert task_after_limit.review_session_id is not None
    assert session.runtime_status == AgentRuntimeStatus.ATTENTION
    assert session.auto_continue_attempts == 10


def test_review_passed_task_stays_in_review_when_agent_runtime_is_working(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-review-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12357,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Review Repo",
            "path": str(repo),
            "session_prefix": "review",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Review task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    # Capture the parked verdict fields. Under the decoupled review-cycle model
    # a passed task is parked in REVIEW; runtime activity (terminal chat) must
    # never move it or touch these fields. We deliberately do NOT flip the task
    # to WORKING here — that runtime-reopen path was removed.
    parked = workspace_manager.tasks[task["id"]]
    parked_review_cycle = parked.review_cycle
    parked_reviewed_cycle = parked.reviewed_cycle
    parked_review_completed_at = parked.review_completed_at
    parked_human_acceptance_requested_at = parked.human_acceptance_requested_at

    # Simulate the agent runtime going WORKING (free-form terminal chat after
    # the verdict). This drives _refresh_session_statuses on the next board poll.
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-review-agent",
            tab_name="Review Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing",
            tmux_session="claude-hub-tab-revi",
            last_changed_at=reviewed_at,
            sampled_at=reviewed_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    # The parked task stays in REVIEW and its verdict fields are untouched even
    # though the agent runtime reports WORKING.
    assert board["tasks"][0]["status"] == "review"
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]
    after = workspace_manager.tasks[task["id"]]
    assert after.review_cycle == parked_review_cycle
    assert after.reviewed_cycle == parked_reviewed_cycle
    assert after.review_completed_at == parked_review_completed_at
    assert after.human_acceptance_requested_at == parked_human_acceptance_requested_at
    assert after.human_accepted_at is None


def test_review_passed_task_does_not_reopen_when_agent_has_new_activity(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-review-continued",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12361,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Review Continued Repo",
            "path": str(repo),
            "session_prefix": "continued",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Review continued task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )

    continued_at = reviewed_at + timedelta(seconds=30)
    assert continued_at > reviewed_at
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-review-continued",
            tab_name="Review Continued Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing follow-up",
            tmux_session="claude-hub-tab-cont",
            last_changed_at=continued_at,
            sampled_at=continued_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "review"
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]


def test_review_passed_reconciles_stale_working_task(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-stale-working",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12364,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Stale Working Repo",
            "path": str(repo),
            "session_prefix": "stale",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Stale working task",
            "prompt": "Do work then report review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    review_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201

    # A passed task is parked in REVIEW. Under the decoupled review-cycle model
    # the runtime layer no longer drifts it to WORKING, so the reconcile-back-to
    # -REVIEW repair is gone; instead the verdict is stable from the first save.
    # A board poll must keep it parked with completed_at cleared and reviewed_at
    # preserved (the user-visible "passed, awaiting acceptance" state).
    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "review"
    assert board["tasks"][0]["completed_at"] is None
    assert board["tasks"][0]["reviewed_at"] == pass_response.json()["created_at"]
    parked = workspace_manager.tasks[task["id"]]
    assert parked.review_completed_at is not None
    assert parked.human_acceptance_requested_at is not None
    assert parked.reviewed_cycle == parked.review_cycle


def test_review_cycle_stale_echo_suppressed_when_parked(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replayed reviewer verdict on a parked task mutates no state (cycle echo)."""

    repo = tmp_path / "repo"
    repo.mkdir()
    stub_workspace_terminal(monkeypatch, repo, tab_id="echo-tab", port=12821)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Echo Repo", "path": str(repo), "session_prefix": "echo"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Echo task", "prompt": "Work then review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "ready_for_review", "message": "Ready"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    assert pass_task_review(client, task["id"]).status_code == 201

    parked = workspace_manager.tasks[task["id"]]
    assert parked.status == WorkspaceTaskStatus.REVIEW
    parked_reviewed_cycle = parked.reviewed_cycle
    parked_review_completed_at = parked.review_completed_at
    parked_human_acceptance_requested_at = parked.human_acceptance_requested_at

    # Replay the SAME reviewer verdict (a stale echo on the closed round).
    echo = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={"task_id": task["id"], "state": "review_passed", "message": "echo"},
    )
    assert echo.status_code == 201

    after = workspace_manager.tasks[task["id"]]
    assert after.status == WorkspaceTaskStatus.REVIEW
    assert after.reviewed_cycle == parked_reviewed_cycle
    assert after.review_cycle == parked.review_cycle  # not reopened
    assert after.review_completed_at == parked_review_completed_at
    assert after.human_acceptance_requested_at == parked_human_acceptance_requested_at
    assert after.human_accepted_at is None


def test_review_cycle_continue_task_reopens_and_redispatches_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """continue_task on a parked task bumps the cycle and a fresh review fires."""

    repo = tmp_path / "repo"
    repo.mkdir()
    stub_workspace_terminal(monkeypatch, repo, tab_id="reopen-tab", port=12822)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reopen Repo", "path": str(repo), "session_prefix": "reopen"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Reopen task", "prompt": "Work then review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "ready_for_review", "message": "Ready"},
    )
    assert pass_task_review(client, task["id"]).status_code == 201
    parked = workspace_manager.tasks[task["id"]]
    cycle_before = parked.review_cycle
    assert parked.reviewed_cycle == cycle_before  # round sealed

    # Human requests changes from the task board.
    reopened = client.post(
        f"/api/workspaces/tasks/{task['id']}/continue",
        json={"message": "Please add more tests"},
    )
    assert reopened.status_code == 200
    reopened_task = workspace_manager.tasks[task["id"]]
    assert reopened_task.status == WorkspaceTaskStatus.WORKING
    assert reopened_task.review_cycle == cycle_before + 1
    assert reopened_task.reviewed_cycle == cycle_before  # prior verdict, now stale
    assert reopened_task.review_completed_at is None
    assert reopened_task.human_acceptance_requested_at is None

    # Worker resubmits the new round; a fresh reviewer must be dispatched.
    resubmit = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "ready_for_review", "message": "Round 2"},
    )
    assert resubmit.status_code == 201
    after = workspace_manager.tasks[task["id"]]
    assert after.review_requested_at is not None
    assert after.review_session_id is not None
    # New round is in flight, not yet judged.
    assert after.reviewed_cycle < after.review_cycle


def test_review_cycle_review_failed_reopens_for_fresh_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed verdict reopens the round; the worker resubmit gets a fresh review."""

    repo = tmp_path / "repo"
    repo.mkdir()
    stub_workspace_terminal(monkeypatch, repo, tab_id="failreopen-tab", port=12823)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Fail Repo", "path": str(repo), "session_prefix": "fail"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Fail task", "prompt": "Work then review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "ready_for_review", "message": "Ready"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    assert reviewer_id is not None
    cycle_before = workspace_manager.tasks[task["id"]].review_cycle

    # Reviewer rejects → continue_task feedback path reopens to WORKING.
    fail = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={"task_id": task["id"], "state": "review_failed", "message": "Fix it"},
    )
    assert fail.status_code == 201
    reopened = workspace_manager.tasks[task["id"]]
    assert reopened.status == WorkspaceTaskStatus.WORKING
    assert reopened.review_cycle == cycle_before + 1
    assert reopened.reviewed_cycle == cycle_before  # failed verdict applied to round 1
    assert reopened.review_requested_at is None

    # Worker resubmits the fix; a fresh review must be dispatched for round 2.
    resubmit = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "ready_for_review", "message": "Fixed"},
    )
    assert resubmit.status_code == 201
    after = workspace_manager.tasks[task["id"]]
    assert after.review_requested_at is not None
    assert after.reviewed_cycle < after.review_cycle


def _stub_workspace_terminal_for_late_reports(monkeypatch: MonkeyPatch, repo: Path) -> None:
    """Monkeypatch ttyd + tmux calls out of the workspace manager for late-report tests."""

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id=f"tab-{name}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12700,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )


def test_late_orchestrator_working_report_after_review_verdict_does_not_flip_status(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC7: WORKING/STARTED/BLOCKED/NEEDS_INPUT reports arriving after a
    reviewer verdict is rendered must not flip REVIEW→WORKING or overwrite
    verdict evidence."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_workspace_terminal_for_late_reports(monkeypatch, repo)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Late Working Guard",
            "path": str(repo),
            "session_prefix": "late",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Late working guard",
            "prompt": "Produce something, pass review, emit late working",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    # Orchestrator reports ready_for_review → reviewer picks it up
    rfr_resp = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert rfr_resp.status_code == 201

    # Reviewer renders a PASS verdict — this sets REVIEW + review_completed_at
    pass_resp = pass_task_review(client, task["id"])
    assert pass_resp.status_code == 201
    pass_resp_data = pass_resp.json()
    reviewed_at = pass_resp_data["created_at"]
    assert reviewed_at is not None

    # Confirm task state before the late report.
    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    pre_task = next(t for t in board["tasks"] if t["id"] == task["id"])
    assert pre_task["status"] == "review"
    assert pre_task["reviewed_at"] == reviewed_at
    review_completed_at_before = workspace_manager.tasks[task["id"]].review_completed_at
    assert review_completed_at_before is not None

    # Now the orchestrator emits a late WORKING report (e.g. a long-running
    # subprocess finally flushed a progress line and an old agent process
    # re-uses the same session).
    late_working = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Late progress update",
        },
    )
    assert late_working.status_code == 201

    # Task MUST still be REVIEW with the original verdict evidence intact.
    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    post_task = next(t for t in board["tasks"] if t["id"] == task["id"])
    assert post_task["status"] == "review", (
        "Late WORKING report must not flip REVIEW → WORKING; " f"got {post_task['status']}"
    )
    assert (
        post_task["reviewed_at"] == reviewed_at
    ), "Late WORKING report must not overwrite reviewed_at"
    task_obj = workspace_manager.tasks[task["id"]]
    assert task_obj.review_completed_at == review_completed_at_before
    assert task_obj.review_session_id is not None


def test_late_orchestrator_completed_report_after_verdict_does_not_redispatch_review(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC8: COMPLETED / READY_FOR_REVIEW reports arriving AFTER a reviewer
    verdict is rendered must not trigger a new review-assignment dispatch
    and must not clear review_completed_at or reviewed_at."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_workspace_terminal_for_late_reports(monkeypatch, repo)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Late Completed Guard",
            "path": str(repo),
            "session_prefix": "latecomp",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Late completed guard",
            "prompt": "Produce something, pass review, emit late completed",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    rfr_resp = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert rfr_resp.status_code == 201

    pass_resp = pass_task_review(client, task["id"])
    assert pass_resp.status_code == 201
    pass_data = pass_resp.json()
    reviewed_at = pass_data["created_at"]
    review_session_before = workspace_manager.tasks[task["id"]].review_session_id
    review_completed_at_before = workspace_manager.tasks[task["id"]].review_completed_at
    assert review_session_before is not None
    assert review_completed_at_before is not None

    # Count review reports BEFORE the late completed to ensure none added.
    review_reports_before = [
        r
        for r in workspace_manager.reports.values()
        if r.task_id == task["id"] and r.state.value.startswith("review_")
    ]

    # Late COMPLETED report from the orchestrator, well after verdict.
    late_resp = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Late completion report from retried agent run",
            "changed_files": [],
            "validation": "Already judged by reviewer.",
            "risks": "None.",
            "acceptance_check": [
                {
                    "criterion": "Placeholder criterion",
                    "status": "passed",
                    "evidence": "Already judged by reviewer verdict.",
                }
            ],
            "goal_packet": {
                "objective": "Late completed guard test",
                "acceptance_criteria": ["Verdict must remain intact after late completed."],
                "validation_plan": ["Assert status and review fields."],
                "assumptions": ["Reviewer verdict is authoritative."],
                "out_of_scope": ["Nothing else."],
                "handoff_requirements": ["No handoff."],
                "status": "approved",
            },
        },
    )
    assert late_resp.status_code == 201

    # Verdict evidence MUST stay intact.
    task_obj = workspace_manager.tasks[task["id"]]
    assert (
        task_obj.status == WorkspaceTaskStatus.REVIEW
    ), f"Task must remain REVIEW after late COMPLETED; got {task_obj.status}"
    assert (
        task_obj.review_session_id == review_session_before
    ), "Late COMPLETED must not reassign the reviewer (no redispatch)"
    assert (
        task_obj.review_completed_at == review_completed_at_before
    ), "Late COMPLETED must not clear review_completed_at"
    assert task_obj.reviewed_at is not None, "Late COMPLETED must not clobber reviewed_at"
    # reviewed_at must fall within +-10s of the pass_response timestamp
    # (both are set close in time; we only care it was not overwritten by
    # the much-later COMPLETED report, so loose equality is enough).
    reviewed_at_ts = task_obj.reviewed_at.timestamp()
    pass_ts = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")).timestamp()
    assert abs(reviewed_at_ts - pass_ts) < 10, (
        f"reviewed_at {task_obj.reviewed_at.isoformat()} drifted too far from "
        f"pass timestamp {reviewed_at}; late COMPLETED overwrote it"
    )

    # No extra REVIEW_STARTED (i.e. reviewer was not re-dispatched).
    review_reports_after = [
        r
        for r in workspace_manager.reports.values()
        if r.task_id == task["id"] and r.state.value.startswith("review_")
    ]
    assert len(review_reports_after) == len(review_reports_before), (
        f"Late COMPLETED must not spawn new review dispatches; "
        f"before={len(review_reports_before)} after={len(review_reports_after)}"
    )


def test_fresh_ready_report_is_not_immediately_reopened_by_runtime_working(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-fresh-ready",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12366,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Fresh Ready Repo",
            "path": str(repo),
            "session_prefix": "fresh",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Fresh ready task",
            "prompt": "Report ready while terminal still updates",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-fresh-ready",
            tab_name="Fresh Ready Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is still finalizing report output",
            tmux_session="claude-hub-tab-fresh",
            last_changed_at=reviewed_at + timedelta(seconds=5),
            sampled_at=reviewed_at + timedelta(seconds=5),
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "review"
    assert board["sessions"][0]["runtime_status"] == "working"


@pytest.mark.parametrize(
    ("activity_delay_seconds", "expected_task_status"),
    [(5, "review"), (30, "review")],
)
def test_completed_review_passed_task_stays_in_review_despite_runtime_activity(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    activity_delay_seconds: int,
    expected_task_status: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-completed-review",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12363,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Completed Review Repo",
            "path": str(repo),
            "session_prefix": "completed",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Completed review task",
            "prompt": "Do work then report completion",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    completed_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Ready for review",
        },
    )
    assert completed_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None

    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-completed-review",
            tab_name="Completed Review Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing follow-up",
            tmux_session="claude-hub-tab-done",
            last_changed_at=reviewed_at + timedelta(seconds=activity_delay_seconds),
            sampled_at=reviewed_at + timedelta(seconds=activity_delay_seconds),
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == expected_task_status
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"


def test_continue_task_marks_working_before_send_verification_failure(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-continue-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12360,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Continue Repo",
            "path": str(repo),
            "session_prefix": "continue",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Continue task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id
    review_response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_needs_input",
            "message": "Need human follow-up",
        },
    )
    assert review_response.status_code == 201
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )

    async def fake_send_session_message(_session_id: str, _message: str) -> None:
        raise RuntimeError("submit verification failed after delivery")

    monkeypatch.setattr(workspace_manager, "send_session_message", fake_send_session_message)

    continue_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/continue",
        json={"message": "Please address review feedback"},
    )
    assert continue_response.status_code == 400

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]
    # The board carries only the latest report per task; the full history lives
    # behind the on-demand per-task endpoint.
    assert [report["state"] for report in board["reports"]] == ["working"]
    history = client.get(f"/api/workspaces/{workspace['id']}/tasks/{task['id']}/reports").json()
    assert [report["state"] for report in history] == [
        "ready_for_review",
        "review_needs_input",
        "working",
    ]


def test_remote_workspace_default_agent_uses_local_tab(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "cwd": cwd,
                "solo_mode": solo_mode,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-default-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12354,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Remote Env",
            "path": str(local_dir),
            "session_prefix": "remote",
            "target": "remote",
            "remote_profile_id": "devbox",
        },
    )
    workspace_id = workspace_response.json()["id"]

    agent_response = client.post(
        f"/api/workspaces/{workspace_id}/agent",
        json={"agent_type": "codex", "role": "orchestrator"},
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["workspace_path"] == str(local_dir)
    assert session["target"] == "local"
    assert session["remote_profile_id"] is None
    assert session["remote_cwd"] is None
    assert session["solo_mode"] is True
    assert session["remote_forward_port"] is None
    assert created_tabs[0]["cwd"] == str(local_dir)
    assert created_tabs[0]["target"] == ExecutionTarget.LOCAL
    assert created_tabs[0]["solo_mode"] is True
    assert created_tabs[0]["remote_profile_id"] is None
    assert created_tabs[0]["remote_forward_port"] is None


def test_remote_workspace_explicit_remote_agent_uses_remote_tab_and_forwarded_reports(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []
    sent_messages: list[str] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "cwd": cwd,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_reconnect": remote_reconnect,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-remote-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12355,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, message: str) -> None:
        sent_messages.append(message)

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Remote Env",
            "path": str(local_dir),
            "session_prefix": "remote",
            "target": "remote",
            "remote_profile_id": "devbox",
            "remote_cwd": "~/repo",
            "remote_reconnect": True,
        },
    )
    assert workspace_response.status_code == 201

    agent_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/agent",
        json={"agent_type": "codex", "role": "orchestrator", "target": "remote"},
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["workspace_path"] == "~/repo"
    assert session["target"] == "remote"
    assert session["remote_profile_id"] == "devbox"
    assert session["remote_cwd"] == "~/repo"
    assert session["solo_mode"] is True
    assert session["remote_forward_port"] == 18173
    assert created_tabs[0]["cwd"] is None
    assert created_tabs[0]["target"] == ExecutionTarget.REMOTE
    assert created_tabs[0]["remote_profile_id"] == "devbox"
    assert created_tabs[0]["remote_cwd"] == "~/repo"
    assert created_tabs[0]["remote_forward_port"] == 18173
    assert "SSH development target: DevBox (devbox)" in sent_messages[0]
    assert "Remote working directory: ~/repo" in sent_messages[0]
    assert "http://127.0.0.1:18173/api/workspaces" in sent_messages[0]


def test_create_agent_can_override_target_and_yolo_mode(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "name": name,
                "cwd": cwd,
                "solo_mode": solo_mode,
                "agent_type": agent_type,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_reconnect": remote_reconnect,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-advanced-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12356,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Mixed Env",
            "path": str(local_dir),
            "session_prefix": "mixed",
        },
    )
    workspace_id = workspace_response.json()["id"]

    agent_response = client.post(
        f"/api/workspaces/{workspace_id}/agent",
        json={
            "agent_type": "claude",
            "title": "Remote careful agent",
            "role": "orchestrator",
            "target": "remote",
            "remote_profile_id": "devbox",
            "remote_cwd": "~/agent-work",
            "remote_reconnect": False,
            "solo_mode": False,
        },
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["target"] == "remote"
    assert session["workspace_path"] == "~/agent-work"
    assert session["remote_profile_id"] == "devbox"
    assert session["remote_cwd"] == "~/agent-work"
    assert session["remote_reconnect"] is False
    assert session["solo_mode"] is False
    assert session["remote_forward_port"] == 18173
    assert created_tabs[0]["name"] == "Remote careful agent"
    assert created_tabs[0]["cwd"] is None
    assert created_tabs[0]["target"] == ExecutionTarget.REMOTE
    assert created_tabs[0]["solo_mode"] is False
    assert created_tabs[0]["remote_profile_id"] == "devbox"
    assert created_tabs[0]["remote_cwd"] == "~/agent-work"
    assert created_tabs[0]["remote_reconnect"] is False


def test_start_task_does_not_dispatch_to_stopped_resident_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"tab-agent-{len(created_tabs) + 1}"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12346 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Restart Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "restart",
        },
    )
    workspace_id = workspace_response.json()["id"]

    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    workspace_manager.sessions[first_agent["id"]] = workspace_manager.sessions[
        first_agent["id"]
    ].model_copy(
        update={
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    task_response = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Offline task",
            "prompt": "Do not dispatch to offline agent",
            "agent_type": "codex",
        },
    )
    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/start",
        json={},
    )

    assert response.status_code == 400
    assert "No idle or working workspace agent is available" in response.json()["detail"]
    assert "restart-agent-2" not in workspace_manager.sessions
    assert len(created_tabs) == 1
    assert len(sent_messages) == 1
    board = client.get(f"/api/workspaces/{workspace_id}/board").json()
    assert board["tasks"][0]["status"] == "todo"
    assert board["tasks"][0]["session_id"] is None


def test_delete_task_removes_reports_and_unlinks_session(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-delete-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12349,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Delete Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "delete",
        },
    )
    workspace_id = workspace_response.json()["id"]
    task_response = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Delete task",
            "prompt": "Delete me",
            "agent_type": "codex",
        },
    )
    task_id = task_response.json()["id"]
    start_response = client.post(f"/api/workspaces/tasks/{task_id}/start", json={})
    session_id = start_response.json()["session_id"]
    client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task_id,
            "state": "started",
            "message": "Started before delete",
        },
    )

    response = client.delete(f"/api/workspaces/tasks/{task_id}")

    assert response.status_code == 204
    board = client.get(f"/api/workspaces/{workspace_id}/board").json()
    assert board["tasks"] == []
    assert board["reports"] == []
    assert board["sessions"][0]["task_id"] is None


def test_done_task_writes_delete_safe_task_record(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "workspace-state"
    monkeypatch.setattr(workspace_manager, "_write_task_record", ORIGINAL_WRITE_TASK_RECORD)

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-record-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12350,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_workspace_dir",
        lambda workspace_id: state_root / workspace_id,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Record Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "record",
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Record task",
            "prompt": "Archive this task",
            "agent_type": "codex",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    base_time = datetime(2026, 1, 2, 3, 0, 0)
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "created_at": base_time,
            "queued_at": base_time + timedelta(minutes=1),
            "started_at": base_time + timedelta(minutes=2),
            "updated_at": base_time + timedelta(minutes=2),
        }
    )
    working_report = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Implementing archive",
            "changed_files": ["backend/claude_hub/services/workspace_manager.py"],
            "validation": "pytest planned",
        },
    ).json()
    completed_report = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Archive complete",
            "changed_files": ["backend/tests/test_workspaces.py"],
            "risks": "None",
        },
    ).json()
    workspace_manager.reports[working_report["id"]] = workspace_manager.reports[
        working_report["id"]
    ].model_copy(update={"created_at": base_time + timedelta(minutes=7)})
    workspace_manager.reports[completed_report["id"]] = workspace_manager.reports[
        completed_report["id"]
    ].model_copy(update={"created_at": base_time + timedelta(minutes=12)})
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "reviewed_at": base_time + timedelta(minutes=12),
            "updated_at": base_time + timedelta(minutes=12),
        }
    )
    monkeypatch.setattr(workspace_module, "_now", lambda: base_time + timedelta(minutes=15))

    done_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"status": "done"},
    )

    assert done_response.status_code == 200
    record_files = list((state_root / workspace["id"] / "task_records").glob("*.json"))
    assert len(record_files) == 1
    record = json.loads(record_files[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["task"]["id"] == task["id"]
    assert record["task"]["status"] == "done"
    assert record["session"]["id"] == started["session_id"]
    assert [report["state"] for report in record["reports"]] == ["working", "completed"]
    assert record["artifacts"]["changed_files"] == [
        "backend/claude_hub/services/workspace_manager.py",
        "backend/tests/test_workspaces.py",
    ]
    assert record["artifacts"]["validation"] == ["pytest planned"]
    assert record["artifacts"]["risks"] == ["None"]
    assert record["artifacts"]["commits"] == []
    assert record["final_summary"] == "Archive complete"
    assert [event["type"] for event in record["timeline"]].count("agent_report") == 2
    assert [
        (event["type"], event["elapsed"], event["duration_since_previous"])
        for event in record["timeline"]
    ] == [
        ("task_created", "0s", "0s"),
        ("task_queued", "1m 0s", "1m 0s"),
        ("task_started", "2m 0s", "1m 0s"),
        ("agent_report", "7m 0s", "5m 0s"),
        ("task_reviewed", "12m 0s", "5m 0s"),
        ("agent_report", "12m 0s", "0s"),
        ("task_completed", "15m 0s", "3m 0s"),
    ]
    assert [
        (event["type"], event["elapsed_seconds"], event["duration_since_previous_seconds"])
        for event in record["timeline"]
    ] == [
        ("task_created", 0, 0),
        ("task_queued", 60, 60),
        ("task_started", 120, 60),
        ("agent_report", 420, 300),
        ("task_reviewed", 720, 300),
        ("agent_report", 720, 0),
        ("task_completed", 900, 180),
    ]

    delete_response = client.delete(f"/api/workspaces/tasks/{task['id']}")

    assert delete_response.status_code == 204
    assert record_files[0].exists()


def test_workspace_routes_validate_missing_resources(tmp_path: Path) -> None:
    client = TestClient(app)

    missing_path_response = client.post(
        "/api/workspaces",
        json={
            "name": "Missing Repo",
            "path": str(tmp_path / "missing"),
        },
    )
    assert missing_path_response.status_code == 400

    task_response = client.post(
        "/api/workspaces/missing-workspace/tasks",
        json={
            "title": "No workspace",
            "prompt": "Should not be created",
            "agent_type": "codex",
        },
    )
    assert task_response.status_code == 404

    update_response = client.patch("/api/workspaces/tasks/missing-task", json={"status": "done"})
    assert update_response.status_code == 404

    delete_response = client.delete("/api/workspaces/tasks/missing-task")
    assert delete_response.status_code == 404


def test_abort_task_returns_active_review_to_todo_and_releases_sessions(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="abort-agent",
        port=12610,
        sent_messages=sent_messages,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Abort Repo", "path": str(repo), "session_prefix": "abort"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Abortable task", "prompt": "Start and get stuck"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    ready_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready, but reviewer may hang",
            "review_decision": "request",
            "review_reason": "Need independent review",
        },
    )
    assert ready_response.status_code == 201
    reviewing_task = workspace_manager.tasks[task["id"]]
    reviewer_id = reviewing_task.review_session_id
    assert reviewer_id is not None
    assert reviewing_task.status == WorkspaceTaskStatus.REVIEW
    assert workspace_manager.sessions[started["session_id"]].current_task_id == task["id"]
    assert workspace_manager.sessions[reviewer_id].current_task_id == task["id"]

    abort_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/abort",
        json={"reason": "Reviewer did not respond"},
    )

    assert abort_response.status_code == 200
    aborted = abort_response.json()
    assert aborted["status"] == "todo"
    assert aborted["session_id"] is None
    assert aborted["review_session_id"] is None
    assert aborted["review_requested_at"] is None
    assert aborted["manual_aborted_at"] is not None
    assert aborted["manual_abort_reason"] == "Reviewer did not respond"
    assert aborted["dispatch_reason"] == "Manually aborted: Reviewer did not respond"
    assert workspace_manager.sessions[started["session_id"]].current_task_id is None
    assert workspace_manager.sessions[started["session_id"]].status == ManagedSessionStatus.IDLE
    assert reviewer_id not in workspace_manager.sessions
    abort_report = list(workspace_manager.reports.values())[-1]
    assert abort_report.state.value == "blocked"
    assert "Reviewer did not respond" in abort_report.message
    assert abort_report.review_decision.value == "skip"

    late_worker_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Late worker completion",
        },
    )
    assert late_worker_response.status_code == 400
    assert "manually aborted" in late_worker_response.json()["detail"]
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.TODO
    assert workspace_manager.sessions[started["session_id"]].current_task_id is None

    late_reviewer_response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Late reviewer pass",
        },
    )
    assert late_reviewer_response.status_code == 404
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.TODO

    restarted = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    restarted_task = workspace_manager.tasks[task["id"]]
    assert restarted_task.manual_aborted_at is None
    assert restarted_task.manual_abort_reason is None
    restarted_report_response = client.post(
        f"/api/workspaces/sessions/{restarted['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Explicit restart accepted",
        },
    )
    assert restarted_report_response.status_code == 201
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.WORKING


def test_abort_task_rejects_done_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Abort Guard Repo", "path": str(repo)},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Done task", "prompt": "Already complete"},
    ).json()
    done_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={"status": "done"})
    assert done_response.status_code == 200

    abort_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/abort",
        json={"reason": "Should not be allowed"},
    )

    assert abort_response.status_code == 400
    assert "Only queued, working, or review tasks" in abort_response.json()["detail"]
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.DONE


def test_update_task_requires_status_and_persists_valid_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Patch Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Patch task",
            "prompt": "Move task",
            "agent_type": "codex",
        },
    ).json()

    empty_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={})
    assert empty_response.status_code == 400

    mode_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={
            "task_mode": "autonomous",
            "autonomy_policy": {"max_iterations": 2},
        },
    )
    assert mode_response.status_code == 200
    assert mode_response.json()["task_mode"] == "autonomous"
    assert mode_response.json()["autonomous_run"]["max_iterations"] == 2

    done_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={"status": "done"})
    assert done_response.status_code == 200
    assert done_response.json()["status"] == "done"


def test_update_todo_task_title_and_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Editable Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Original task",
            "prompt": "Original description",
            "agent_type": "codex",
        },
    ).json()

    response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={
            "title": "  Updated task  ",
            "prompt": "  Updated description  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Updated task"
    assert response.json()["prompt"] == "Updated description"
    stored_task = workspace_manager.tasks[task["id"]]
    assert stored_task.title == "Updated task"
    assert stored_task.prompt == "Updated description"
    assert stored_task.status == WorkspaceTaskStatus.TODO


def test_update_attachment_only_todo_task_title_without_prompt(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Screenshot Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Original screenshot task",
            "prompt": "",
            "agent_type": "codex",
            "attachments": [
                {
                    "filename": "screen shot.png",
                    "mime_type": "image/png",
                    "data_url": PNG_DATA_URL,
                }
            ],
        },
    ).json()
    assert task["prompt"] == ""
    assert len(task["attachments"]) == 1

    response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"title": "Updated screenshot task"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Updated screenshot task"
    assert response.json()["prompt"] == ""
    stored_task = workspace_manager.tasks[task["id"]]
    assert stored_task.title == "Updated screenshot task"
    assert stored_task.prompt == ""
    assert len(stored_task.attachments) == 1


def test_update_task_rejects_blank_title_or_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Validation Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Original task",
            "prompt": "Original description",
            "agent_type": "codex",
        },
    ).json()

    title_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"title": "   "},
    )
    prompt_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"prompt": "   "},
    )

    assert title_response.status_code == 400
    assert title_response.json()["detail"] == "Task title is required"
    assert prompt_response.status_code == 400
    assert prompt_response.json()["detail"] == "Task description is required"
    stored_task = workspace_manager.tasks[task["id"]]
    assert stored_task.title == "Original task"
    assert stored_task.prompt == "Original description"


def test_update_task_rejects_title_prompt_edit_after_todo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Started Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Original task",
            "prompt": "Original description",
            "agent_type": "codex",
        },
    ).json()
    status_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"status": "done"},
    )
    assert status_response.status_code == 200

    response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={
            "title": "Changed after done",
            "prompt": "Changed description",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only todo tasks can be edited"
    stored_task = workspace_manager.tasks[task["id"]]
    assert stored_task.title == "Original task"
    assert stored_task.prompt == "Original description"
    assert stored_task.status == WorkspaceTaskStatus.DONE


def _seed_workspace_with_reports() -> str:
    """Register a workspace and a spread of reports directly in the manager."""
    now = datetime.now()
    workspace_id = "ws-reports"
    workspace_manager.workspaces[workspace_id] = Workspace(
        id=workspace_id,
        name="Reports WS",
        path="/tmp/reports-ws",
        default_branch="main",
        session_prefix="rep",
        created_at=now,
        updated_at=now,
    )

    def _add(report_id: str, task_id: Optional[str], offset_seconds: int) -> None:
        workspace_manager.reports[report_id] = AgentReport(
            id=report_id,
            workspace_id=workspace_id,
            task_id=task_id,
            session_id="rep-session",
            state=AgentReportState.WORKING,
            message=report_id,
            created_at=now + timedelta(seconds=offset_seconds),
        )

    # task-a: three reports; task-b: one; plus a task_id=None report and a
    # report belonging to another workspace that must never leak in.
    _add("a1", "task-a", 0)
    _add("a2", "task-a", 1)
    _add("a3", "task-a", 2)
    _add("b1", "task-b", 1)
    _add("none1", None, 3)
    workspace_manager.reports["other"] = AgentReport(
        id="other",
        workspace_id="ws-other",
        task_id="task-a",
        session_id="rep-session",
        state=AgentReportState.WORKING,
        message="other",
        created_at=now,
    )
    return workspace_id


def test_latest_reports_per_task_keeps_newest_per_task() -> None:
    workspace_id = _seed_workspace_with_reports()

    latest = workspace_manager.latest_reports_per_task_for_workspace(workspace_id)
    by_task = {report.task_id: report.id for report in latest}

    # One entry per task_id (including the None-task bucket), newest wins.
    assert by_task == {"task-a": "a3", "task-b": "b1", None: "none1"}
    # Sorted ascending by created_at, and no cross-workspace leakage.
    assert [report.id for report in latest] == ["b1", "a3", "none1"]
    assert all(report.workspace_id == workspace_id for report in latest)


def test_reports_for_task_returns_full_history() -> None:
    workspace_id = _seed_workspace_with_reports()

    history = workspace_manager.reports_for_task(workspace_id, "task-a")

    # Full ascending history for the task, scoped to this workspace only.
    assert [report.id for report in history] == ["a1", "a2", "a3"]


def test_reports_for_task_unknown_workspace_raises() -> None:
    with pytest.raises(KeyError):
        workspace_manager.reports_for_task("missing-ws", "task-a")


def test_get_task_reports_endpoint_returns_history(tmp_path: Path) -> None:
    workspace_id = _seed_workspace_with_reports()
    client = TestClient(app)

    response = client.get(f"/api/workspaces/{workspace_id}/tasks/task-a/reports")

    assert response.status_code == 200
    assert [report["id"] for report in response.json()] == ["a1", "a2", "a3"]


def test_get_task_reports_endpoint_unknown_workspace_returns_404() -> None:
    client = TestClient(app)

    response = client.get("/api/workspaces/missing-ws/tasks/task-a/reports")

    assert response.status_code == 404
    assert response.json()["detail"] == "Workspace not found"
