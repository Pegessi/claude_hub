"""End-to-end tests for Work Standard Heterogeneous Pipeline recipes, Git worktrees, remote push, InfoWall sync, and Dot delegation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from click.testing import CliRunner
from pytest import MonkeyPatch

from claude_hub.cli.main import cli
from claude_hub.models import (
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ContinueTaskRequest,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    GoalPacket,
    ReviewDecision,
    SessionKind,
    StartTaskRequest,
    StreamModelOption,
    TerminalTab,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskRecipe,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.services import cross_reviewer, managed_chat, maso_discovery
from claude_hub.services.dot import DotService, DotStore, reset_dot_service
from claude_hub.services.work_pipeline import (
    _infowall_env,
    _run_bounded_subprocess,
    push_task_feature_branch,
    read_infowall_demand_summary,
    sync_task_progress_to_infowall,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


def _init_git_repo_with_bare_origin(tmp_path: Path) -> tuple[Path, Path]:
    bare_origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare_origin)], check=True, capture_output=True)

    repo = tmp_path / "sample_repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Sample Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "chore: initial commit"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(bare_origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    return repo, bare_origin


def _setup_fake_chat_runtime(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> tuple[WorkspaceManager, list[str]]:
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    index = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index)

    tabs = _wm.ttyd_manager
    monkeypatch.setattr(tabs, "processes", {})
    monkeypatch.setattr(tabs, "_tab_order", [])
    monkeypatch.setattr(tabs, "_status_snapshots", {})
    monkeypatch.setattr(tabs, "_status_cache", {})
    monkeypatch.setattr(tabs, "_start_locks", {})
    monkeypatch.setattr(tabs, "_save_state", lambda: None)
    monkeypatch.setattr(tabs, "_save_order", lambda: None)
    monkeypatch.setattr(tabs, "_get_next_port", lambda: 25000 + len(tabs.processes))

    deleted_tabs: list[str] = []
    orig_delete_tab = tabs.delete_tab

    async def tracking_delete_tab(tab_id: str) -> bool:
        deleted_tabs.append(tab_id)
        return bool(await orig_delete_tab(tab_id))

    monkeypatch.setattr(tabs, "delete_tab", tracking_delete_tab)

    async def fake_send(_session, _text, _attachments, _call_id):
        return None

    monkeypatch.setattr(managed_chat, "send_message", fake_send)
    monkeypatch.setattr(managed_chat, "delivery_state", lambda _s, _cid: "accepted")
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda _s: False)
    monkeypatch.setattr(managed_chat, "runtime_status", lambda _s: AgentRuntimeStatus.IDLE)

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: "")
    return manager, deleted_tabs


def test_recipe_schema_defaults_and_legacy_normalization(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="Schema Repo", path=str(repo)))

    # 1. STANDARD preserves 100% existing defaults
    std_task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(title="Standard task", prompt="Do standard work"),
    )
    assert std_task.recipe == WorkspaceTaskRecipe.STANDARD
    assert std_task.auto_worktree is False
    assert std_task.auto_push_remote is False
    assert std_task.task_mode == WorkspaceTaskMode.REVIEWED
    assert std_task.session_kind == SessionKind.TERMINAL

    # 2. SPEC_IMPLEMENT_REVIEW defaults auto_worktree=True, auto_push_remote=True
    spec_task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Implement pipeline",
            prompt="Build feature and review",
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            agent_type=AgentType.MASO,
        ),
    )
    assert spec_task.recipe == WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW
    assert spec_task.auto_worktree is True
    assert spec_task.auto_push_remote is True
    assert spec_task.session_kind == SessionKind.CHAT

    # 3. TRIAGE_DIAGNOSE defaults task_mode=SUBAGENT and session_kind=CHAT
    triage_task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Triage crash",
            prompt="Check logs",
            recipe=WorkspaceTaskRecipe.TRIAGE_DIAGNOSE,
            infowall_demand_id="dem-42",
        ),
    )
    assert triage_task.recipe == WorkspaceTaskRecipe.TRIAGE_DIAGNOSE
    assert triage_task.task_mode == WorkspaceTaskMode.SUBAGENT
    assert triage_task.session_kind == SessionKind.CHAT
    assert triage_task.infowall_demand_id == "dem-42"

    # 4. Todo edit updates recipe defaults
    edited = manager._prepare_task_edit(
        std_task,
        WorkspaceTaskUpdate(
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            infowall_demand_id="dem-99",
        ),
        datetime.now(),
    )
    assert edited.update["recipe"] == WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW
    assert edited.update["auto_worktree"] is True
    assert edited.update["auto_push_remote"] is True
    assert edited.update["infowall_demand_id"] == "dem-99"

    # 5. Legacy persisted task dict missing new fields normalizes cleanly
    legacy_norm = manager._normalize_task_item(
        {
            "id": "legacy-1",
            "workspace_id": ws.id,
            "title": "Legacy",
            "prompt": "Old task",
            "agent_type": "codex",
            "status": "todo",
        }
    )
    assert legacy_norm["recipe"] == "standard"
    assert legacy_norm["auto_worktree"] is False
    assert legacy_norm["auto_push_remote"] is False
    assert legacy_norm["infowall_demand_id"] is None
    assert legacy_norm["worktree_path"] is None
    assert legacy_norm["remote_pushed_ref"] is None


@pytest.mark.asyncio
async def test_spec_implement_review_worktree_cross_review_and_remote_push(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, bare_origin = _init_git_repo_with_bare_origin(tmp_path)
    manager, deleted_tabs = _setup_fake_chat_runtime(monkeypatch, tmp_path)

    async def fake_discover_maso(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            [
                StreamModelOption(id="gemini-4-argon", label="gemini-4-argon"),
                StreamModelOption(id="gpt-6-astra", label="gpt-6-astra"),
            ],
            "gemini-4-argon",
        )

    monkeypatch.setattr(cross_reviewer, "discover_maso_service", fake_discover_maso)
    monkeypatch.setattr(maso_discovery, "discover_maso_service", fake_discover_maso)

    ws = manager.create_workspace(
        WorkspaceCreate(
            name="Sample Repo",
            path=str(repo),
            default_branch="main",
            target=ExecutionTarget.LOCAL,
        )
    )

    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Add pipeline helper",
            prompt="Create helper.py and commit on feature branch",
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    assert task.auto_worktree is True
    assert task.auto_push_remote is True

    # Starting the task automatically creates an isolated worktree under sample_repo_worktree/
    # and routes execution to a task-scoped ephemeral orchestrator in that worktree.
    started = await manager.start_task(task.id)
    assert started.status == WorkspaceTaskStatus.WORKING
    assert started.worktree_path is not None
    assert started.worktree_branch is not None
    assert started.worktree_branch.startswith("feat/add-pipeline-helper-")

    wt_path = Path(started.worktree_path)
    assert wt_path.exists()
    assert wt_path.parent == tmp_path / "sample_repo_worktree"
    assert wt_path.resolve() != repo.resolve()
    assert started.session_id is not None

    worker = manager.sessions[started.session_id]
    assert Path(worker.workspace_path).resolve() == wt_path.resolve()
    assert worker.ephemeral is True
    assert worker.caller_owned_ephemeral is True

    # Verify assignment prompt has streamlined spec_implement_review instructions
    prompt_text = manager._build_task_assignment_prompt(ws, started, worker)
    assert "Pipeline recipe: spec_implement_review" in prompt_text
    assert "Do NOT stop and wait for a separate Goal Packet approval round" in prompt_text

    # Worker posts a working report with a Goal Packet: must NOT stop-and-wait in REVIEW!
    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            call_id="work-gp-1",
            message="Starting implementation",
            goal_packet=GoalPacket(
                objective="Create helper.py",
                acceptance_criteria=["helper.py committed"],
                validation_plan=["pytest"],
            ),
        ),
    )
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING

    # Simulate worker committing a change on the feature branch in the isolated worktree
    (wt_path / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt_path), "add", "helper.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "feat: add helper.py"],
        check=True,
        capture_output=True,
    )
    head_sha = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Primary checkout must remain untouched and clean on main
    primary_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert primary_status == ""
    assert not (repo / "helper.py").exists()

    # Even if worker passes review_decision=SKIP, SPEC_IMPLEMENT_REVIEW forces cross-review!
    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            call_id="work-done-1",
            message="Implemented helper.py",
            changed_files=["helper.py"],
            validation="pytest passed",
            risks="none",
            review_decision=ReviewDecision.SKIP,
        ),
    )

    in_review = manager.tasks[task.id]
    assert in_review.status == WorkspaceTaskStatus.REVIEW
    reviewer_id = in_review.review_session_id
    assert reviewer_id is not None
    reviewer = manager.sessions[reviewer_id]

    # Cross-reviewer posts REVIEW_PASSED -> triggers automatic git push -u origin <feature_branch>
    await manager.create_report(
        reviewer.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_PASSED,
            call_id="rev-pass-1",
            message="Verified commit and tests.",
        ),
    )

    reviewed = manager.tasks[task.id]
    assert reviewed.status == WorkspaceTaskStatus.REVIEW
    assert reviewed.remote_push_error is None
    assert reviewed.remote_pushed_ref == f"origin/{started.worktree_branch}@{head_sha[:7]}"
    assert reviewer_id not in manager.sessions

    # Verify the ref actually exists on the bare origin repository!
    origin_sha = subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare_origin),
            "rev-parse",
            f"refs/heads/{started.worktree_branch}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin_sha == head_sha

    # Cycle 2: reopen the task via continue_task, commit a second change (commit B),
    # and verify the second REVIEW_PASSED re-pushes commit B and updates remote_pushed_ref!
    await manager.continue_task(
        task.id,
        ContinueTaskRequest(message="Add second helper constant"),
    )
    (wt_path / "helper.py").write_text("VALUE = 42\nSECOND = 99\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt_path), "add", "helper.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "feat: add SECOND constant"],
        check=True,
        capture_output=True,
    )
    head_sha_b = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_sha_b != head_sha

    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            call_id="work-done-2",
            message="Added SECOND constant",
            changed_files=["helper.py"],
            validation="pytest passed",
            risks="none",
            review_decision=ReviewDecision.REQUEST,
        ),
    )
    in_review_2 = manager.tasks[task.id]
    assert in_review_2.status == WorkspaceTaskStatus.REVIEW
    reviewer_2_id = in_review_2.review_session_id
    assert reviewer_2_id is not None

    await manager.create_report(
        reviewer_2_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_PASSED,
            call_id="rev-pass-2",
            message="Verified cycle 2 commit B.",
        ),
    )

    reviewed_2 = manager.tasks[task.id]
    assert reviewed_2.remote_push_error is None
    assert reviewed_2.remote_pushed_ref == f"origin/{started.worktree_branch}@{head_sha_b[:7]}"
    origin_sha_b = subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare_origin),
            "rev-parse",
            f"refs/heads/{started.worktree_branch}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin_sha_b == head_sha_b

    # Human acceptance transitions task to DONE and cleans up the ephemeral worktree worker session
    done_task = await manager.update_task(
        task.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE)
    )
    assert done_task.status == WorkspaceTaskStatus.DONE
    assert worker.id not in manager.sessions
    assert worker.tab_id in deleted_tabs


@pytest.mark.asyncio
async def test_push_refuses_protected_main_branch(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, _bare_origin = _init_git_repo_with_bare_origin(tmp_path)
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    ws = manager.create_workspace(
        WorkspaceCreate(name="Main Guard Repo", path=str(repo), default_branch="main")
    )
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Attempt push on main",
            prompt="Should refuse pushing main",
            auto_worktree=False,
            auto_push_remote=True,
        ),
    )
    pushed_ref, push_error = await push_task_feature_branch(ws, task, cwd=str(repo))
    assert pushed_ref is None
    assert push_error is not None
    assert "Refused to push protected or default branch 'main'" in push_error


@pytest.mark.asyncio
async def test_triage_diagnose_auto_accepts_and_deletes_ephemeral_session(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    manager, deleted_tabs = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    ws = manager.create_workspace(
        WorkspaceCreate(name="Triage Repo", path=str(repo), default_branch="main")
    )
    worker = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            ephemeral=True,
            caller_owned_ephemeral=True,
            reuse_existing=False,
            cwd=str(repo),
        ),
    )
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Diagnose OOM in trial 123",
            prompt="Inspect logs and summarize root cause",
            agent_type=AgentType.CLAUDE,
            recipe=WorkspaceTaskRecipe.TRIAGE_DIAGNOSE,
        ),
    )
    assert task.task_mode == WorkspaceTaskMode.SUBAGENT
    assert task.session_kind == SessionKind.CHAT

    started = await manager.start_task(task.id, StartTaskRequest(target_session_id=worker.id))
    prompt_text = manager._build_task_assignment_prompt(ws, started, worker)
    assert "Pipeline recipe: triage_diagnose" in prompt_text
    assert "copy-paste reply draft" in prompt_text

    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            call_id="triage-done-1",
            message="Root cause: batch_size=64 exceeded KV cache. Reply draft: lower batch_size to 32.",
            changed_files=[],
            validation="Inspected trial 123 logs",
        ),
    )

    finished = manager.tasks[task.id]
    assert finished.status == WorkspaceTaskStatus.DONE
    assert (
        finished.review_skip_reason
        == "triage_diagnose pipeline auto-accepted after evidence report"
    )
    assert finished.completed_at is not None
    assert worker.id not in manager.sessions
    assert worker.tab_id in deleted_tabs


@pytest.mark.asyncio
async def test_infowall_writeback_and_demand_summary_without_reading_key_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture_file = tmp_path / "infowall_captured.json"
    env_capture_file = tmp_path / "infowall_env.json"
    fake_bin = tmp_path / "fake_infowall"
    # Key file intentionally does not exist on disk to prove Python never opens/reads it!
    unreadable_key_path = tmp_path / "nonexistent-api-key-path"

    script = f"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open({str(env_capture_file)!r}, "w", encoding="utf-8") as f:
    json.dump({{
        "INFOWALL_URL": os.environ.get("INFOWALL_URL"),
        "INFOWALL_API_KEY_FILE": os.environ.get("INFOWALL_API_KEY_FILE"),
        "args": args,
    }}, f)
if args[:1] == ["health"]:
    print(json.dumps({{"ok": True, "instance_role": "primary", "read_only": False}}))
    sys.exit(0)
if args[:2] == ["demand", "get"]:
    print(json.dumps({{
        "id": args[2],
        "title": "合入 xperf_evo 部署规划（MR 262）",
        "priority": "p1",
        "status": "active",
        "next_action": "完成跨模型 review 并推送分支",
    }}))
    sys.exit(0)
if args[:2] == ["demand", "apply"]:
    raw = sys.stdin.read()
    with open({str(capture_file)!r}, "w", encoding="utf-8") as f:
        f.write(raw)
    print(json.dumps({{"created": 0, "updated": 1, "skipped": 0}}))
    sys.exit(0)
sys.exit(1)
"""
    fake_bin.write_text(script, encoding="utf-8")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("INFOWALL_BIN", str(fake_bin))
    monkeypatch.setenv("INFOWALL_URL", "http://127.0.0.1:18899")
    monkeypatch.setenv("INFOWALL_API_KEY_FILE", str(unreadable_key_path))

    summary = await read_infowall_demand_summary("dem-262")
    assert summary is not None
    assert "InfoWall Demand [dem-262]: 合入 xperf_evo 部署规划（MR 262）" in summary
    assert "priority=p1, status=active" in summary
    assert "完成跨模型 review 并推送分支" in summary

    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="IW Repo", path=str(repo)))
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Follow up MR 262",
            prompt="Verify and report",
            infowall_demand_id="dem-262",
        ),
    )
    task = task.model_copy(update={"remote_pushed_ref": "origin/feat/mr-262@abc1234"})

    ok = await sync_task_progress_to_infowall(
        task,
        milestone="review_passed",
        summary=(
            "Cross-review passed for https://code.byted.org/seed/xperf_evo/merge_requests/262 "
            "and doc https://bytedance.larkoffice.com/docx/abc123"
        ),
    )
    assert ok is True
    env_recorded = json.loads(env_capture_file.read_text(encoding="utf-8"))
    assert env_recorded["INFOWALL_API_KEY_FILE"] == str(unreadable_key_path)
    assert not unreadable_key_path.exists()

    applied = json.loads(capture_file.read_text(encoding="utf-8"))
    demands = applied["demands"]
    assert len(demands) == 1
    assert demands[0]["id"] == "dem-262"
    prog = demands[0]["progress"][0]
    assert prog["dedupe_key"] == f"claude-hub-pipeline:{task.id}:review_passed"
    assert "origin/feat/mr-262@abc1234" in prog["text"]
    link_kinds = {item["kind"]: item["url"] for item in prog["links"]}
    assert link_kinds["mr"] == "https://code.byted.org/seed/xperf_evo/merge_requests/262"
    assert link_kinds["doc"] == "https://bytedance.larkoffice.com/docx/abc123"


@pytest.mark.asyncio
async def test_dot_delegate_maso_model_recipe_and_cli(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)

    from claude_hub.services import workspace_manager as global_wm

    ws = manager.create_workspace(
        WorkspaceCreate(name="Dot Delegate Repo", path=str(repo), default_branch="main")
    )
    monkeypatch.setattr(global_wm, "workspaces", manager.workspaces)
    monkeypatch.setattr(global_wm, "tasks", manager.tasks)
    monkeypatch.setattr(global_wm, "sessions", manager.sessions)
    monkeypatch.setattr(global_wm, "register_task", manager.register_task)
    monkeypatch.setattr(global_wm, "ensure_workspace_agent", manager.ensure_workspace_agent)
    monkeypatch.setattr(global_wm, "start_task", manager.start_task)

    async def fake_discover_maso(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            [StreamModelOption(id="gemini-4-argon", label="gemini-4-argon")],
            "gemini-4-argon",
        )

    monkeypatch.setattr(maso_discovery, "discover_maso_service", fake_discover_maso)

    from claude_hub.services import work_pipeline

    async def fake_demand_summary(demand_id: str) -> str:
        return f"InfoWall Demand [{demand_id}]: Optimize KV transfer (priority=p0, status=active)"

    monkeypatch.setattr(work_pipeline, "read_infowall_demand_summary", fake_demand_summary)

    dot_store = DotStore(tmp_path / "dot")
    dot_store.ensure()
    dot_store.update_state(tab_id="dot-tab-1")
    dot_service = DotService(dot_store)
    reset_dot_service(dot_service)
    try:
        result = await dot_service.delegate(
            workspace_id=ws.id,
            title="Implement KV cache optimization",
            goal="Reduce KV transfer latency by 20%",
            done_when=["pytest tests/test_kv.py passes"],
            output="src/kv.py",
            agent_type="maso",
            model="gemini-4-argon_reasoning_high",
            recipe="spec_implement_review",
            demand_id="dem-kv-1",
            ephemeral=True,
            confirm=True,
            actor_key="actor-1",
        )
        assert result["held"] is False
        task_id = result["work"]["task_id"]
        task = manager.tasks[task_id]
        assert task.agent_type == AgentType.MASO
        assert task.session_kind == SessionKind.CHAT
        assert task.recipe == WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW
        assert task.infowall_demand_id == "dem-kv-1"
        assert "InfoWall Demand [dem-kv-1]" in task.prompt
        assert task.worktree_path is not None
        assert Path(task.worktree_path).exists()
        assert task.session_id is not None

        assigned_session = manager.sessions[task.session_id]
        assert assigned_session.agent_type == AgentType.MASO
        assert assigned_session.session_kind == SessionKind.CHAT
        assert assigned_session.ephemeral is True
        assert assigned_session.caller_owned_ephemeral is True
        assert assigned_session.env["MASO_BASE_URL"] == "http://127.0.0.1:11004"
        assert assigned_session.env["MASO_MODEL"] == "gemini-4-argon_reasoning_high"
        assert Path(assigned_session.workspace_path).resolve() == Path(task.worktree_path).resolve()
    finally:
        reset_dot_service(None)

    # Also verify CLI `dot delegate` and `task create` serialize all new options cleanly
    captured_requests: list[tuple[str, dict[str, Any]]] = []

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method: str, path: str, json: Any = None, params: Any = None) -> Any:
            captured_requests.append((path, dict(json or {})))
            return {
                "held": False,
                "warnings": [],
                "work": {"task_id": "t-1", "status": "working", "title": "CLI Task"},
            }

        def create_task(self, workspace_id: str, body: dict[str, Any]) -> Any:
            captured_requests.append((f"/api/workspaces/{workspace_id}/tasks", dict(body)))
            return {"id": "t-2", "status": "todo"}

    from claude_hub.cli import main as cli_main

    monkeypatch.setattr(cli_main, "get_client", lambda _ctx: DummyClient())
    runner = CliRunner()
    res_dot = runner.invoke(
        cli,
        [
            "dot",
            "delegate",
            "--workspace",
            "ws-1",
            "--title",
            "CLI Task",
            "--goal",
            "Ship it",
            "--done-when",
            "tests pass",
            "--agent",
            "maso",
            "--model",
            "gemini-4-argon_reasoning_high",
            "--recipe",
            "spec_implement_review",
            "--demand-id",
            "dem-cli-1",
            "--ephemeral",
            "--confirm",
        ],
    )
    assert res_dot.exit_code == 0, res_dot.output
    path, delegate_body = captured_requests[-1]
    assert path == "/api/dot/delegate"
    assert delegate_body["agent_type"] == "maso"
    assert delegate_body["model"] == "gemini-4-argon_reasoning_high"
    assert delegate_body["recipe"] == "spec_implement_review"
    assert delegate_body["demand_id"] == "dem-cli-1"
    assert delegate_body["ephemeral"] is True

    res_task = runner.invoke(
        cli,
        [
            "task",
            "create",
            "ws-1",
            "--title",
            "Triage CLI",
            "--prompt",
            "Check issue",
            "--recipe",
            "triage_diagnose",
            "--demand-id",
            "dem-cli-2",
            "--no-auto-worktree",
            "--no-auto-push-remote",
        ],
    )
    assert res_task.exit_code == 0, res_task.output
    task_path, task_body = captured_requests[-1]
    assert task_path == "/api/workspaces/ws-1/tasks"
    assert task_body["recipe"] == "triage_diagnose"
    assert task_body["infowall_demand_id"] == "dem-cli-2"
    assert task_body["auto_worktree"] is False
    assert task_body["auto_push_remote"] is False
    assert "task_mode" not in task_body


@pytest.mark.asyncio
async def test_fail_closed_auto_worktree_concurrent_start_and_rollback_compensation(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio

    manager, deleted_tabs = _setup_fake_chat_runtime(monkeypatch, tmp_path)

    # 1. Non-git workspace directory with auto_worktree=True MUST fail closed (never fall back to workspace.path)
    non_git_dir = tmp_path / "not_a_git_repo"
    non_git_dir.mkdir()
    ws_non_git = manager.create_workspace(
        WorkspaceCreate(name="Non-Git Workspace", path=str(non_git_dir))
    )
    task_non_git = manager.create_task(
        ws_non_git.id,
        WorkspaceTaskCreate(
            title="Must fail closed",
            prompt="Should not run in non-git primary path",
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    with pytest.raises(RuntimeError, match="not a valid Git repository"):
        await manager.start_task(task_non_git.id)
    assert manager.tasks[task_non_git.id].status == WorkspaceTaskStatus.TODO
    assert manager.tasks[task_non_git.id].session_id is None
    assert len(manager.sessions) == 0

    # 2. Failed start_task (bad related_task_id) must reject before leaking any ephemeral session
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    ws = manager.create_workspace(
        WorkspaceCreate(name="Concurrency Repo", path=str(repo), default_branch="main")
    )
    task_bad_rel = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Bad related task",
            prompt="Should fail without leaking session",
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    with pytest.raises(KeyError):
        await manager.start_task(
            task_bad_rel.id,
            StartTaskRequest(related_task_id="nonexistent-related-id"),
        )
    assert len(manager.sessions) == 0

    # Also verify compensation if _choose_dispatch_target raises after worktree agent creation
    orig_choose = manager._choose_dispatch_target

    async def failing_choose(*_a, **_k):
        raise RuntimeError("dispatch target failure after ephemeral creation")

    monkeypatch.setattr(manager, "_choose_dispatch_target", failing_choose)
    with pytest.raises(RuntimeError, match="dispatch target failure"):
        await manager.start_task(task_bad_rel.id)
    assert len(manager.sessions) == 0
    assert len(deleted_tabs) == 1
    monkeypatch.setattr(manager, "_choose_dispatch_target", orig_choose)

    # 3. Concurrent asyncio.gather(start_task, start_task) on the same auto_worktree task
    # serializes via _task_start_locks and leaves exactly 1 worker session.
    task_concurrent = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Concurrent start task",
            prompt="Run concurrently",
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    res1, res2 = await asyncio.gather(
        manager.start_task(task_concurrent.id),
        manager.start_task(task_concurrent.id),
    )
    assert res1.session_id == res2.session_id
    assert res1.session_id is not None
    assert len(manager.sessions) == 1


@pytest.mark.asyncio
async def test_dot_delegate_request_key_idempotency_with_changing_demand_summary_and_subprocess_reaping(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)

    from claude_hub.services import work_pipeline
    from claude_hub.services import workspace_manager as global_wm

    ws = manager.create_workspace(
        WorkspaceCreate(name="Idempotency Repo", path=str(repo), default_branch="main")
    )
    monkeypatch.setattr(global_wm, "workspaces", manager.workspaces)
    monkeypatch.setattr(global_wm, "tasks", manager.tasks)
    monkeypatch.setattr(global_wm, "sessions", manager.sessions)
    monkeypatch.setattr(global_wm, "register_task", manager.register_task)
    monkeypatch.setattr(global_wm, "ensure_workspace_agent", manager.ensure_workspace_agent)
    monkeypatch.setattr(global_wm, "start_task", manager.start_task)
    monkeypatch.setattr(global_wm, "delete_session", manager.delete_session)

    call_count = 0

    async def changing_demand_summary(demand_id: str) -> str | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return f"InfoWall Demand [{demand_id}]: Version 1 summary"
        return f"InfoWall Demand [{demand_id}]: Version 2 mutated summary"

    monkeypatch.setattr(work_pipeline, "read_infowall_demand_summary", changing_demand_summary)

    dot_store = DotStore(tmp_path / "dot_idem")
    dot_store.ensure()
    dot_store.update_state(tab_id="dot-tab-idem")
    dot_service = DotService(dot_store)
    reset_dot_service(dot_service)
    try:
        first = await dot_service.delegate(
            workspace_id=ws.id,
            title="Idempotent demand delegation",
            goal="Verify request_key replay safety",
            done_when=["pytest passes"],
            output="out.txt",
            demand_id="dem-idem-1",
            request_key="req-idem-key-1",
            start=False,
            confirm=True,
            actor_key="actor-idem",
        )
        second = await dot_service.delegate(
            workspace_id=ws.id,
            title="Idempotent demand delegation",
            goal="Verify request_key replay safety",
            done_when=["pytest passes"],
            output="out.txt",
            demand_id="dem-idem-1",
            request_key="req-idem-key-1",
            start=False,
            confirm=True,
            actor_key="actor-idem",
        )
        assert first["work"]["task_id"] == second["work"]["task_id"]
        assert call_count == 1

        # Concurrent delegate calls with identical request_key serialize cleanly
        import asyncio

        conc1, conc2 = await asyncio.gather(
            dot_service.delegate(
                workspace_id=ws.id,
                title="Concurrent demand delegation",
                goal="Verify concurrent request_key serialization",
                done_when=["pytest passes"],
                output="out2.txt",
                demand_id="dem-idem-2",
                request_key="req-idem-key-concurrent",
                start=False,
                confirm=True,
                actor_key="actor-idem",
            ),
            dot_service.delegate(
                workspace_id=ws.id,
                title="Concurrent demand delegation",
                goal="Verify concurrent request_key serialization",
                done_when=["pytest passes"],
                output="out2.txt",
                demand_id="dem-idem-2",
                request_key="req-idem-key-concurrent",
                start=False,
                confirm=True,
                actor_key="actor-idem",
            ),
        )
        assert conc1["work"]["task_id"] == conc2["work"]["task_id"]
        assert call_count == 2
    finally:
        reset_dot_service(None)

    # Verify _infowall_env sets NO_PROXY for loopback URLs
    monkeypatch.setenv("INFOWALL_URL", "http://127.0.0.1:18899")
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:8080")
    iw_env = _infowall_env()
    assert iw_env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert iw_env["no_proxy"] == "127.0.0.1,localhost,::1"

    # Verify _run_bounded_subprocess kills and reaps timed-out process groups cleanly,
    # even when the group leader exits immediately after forking a background descendant!
    grandchild_pid_file = tmp_path / "grandchild.pid"
    rc, _out, err = await _run_bounded_subprocess(
        [
            "python3",
            "-c",
            (
                f"import os, time, pathlib; "
                f"pid = os.fork(); "
                f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())) if pid == 0 else os._exit(0); "
                f"time.sleep(30)"
            ),
        ],
        timeout=0.4,
    )
    assert rc == 124
    assert "timed out" in err
    if grandchild_pid_file.exists():
        grandchild_pid = int(grandchild_pid_file.read_text().strip())
        for _ in range(25):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)

    # Verify _run_bounded_subprocess enforces cumulative max_output_bytes cap
    rc_cap, _out_cap, err_cap = await _run_bounded_subprocess(
        [
            "python3",
            "-c",
            "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()",
        ],
        timeout=5.0,
        max_output_bytes=32768,
    )
    assert rc_cap == 1
    assert "exceeded 32768 bytes cap" in err_cap


@pytest.mark.asyncio
async def test_concurrent_todo_edit_to_auto_worktree_and_reviewer_incarnation_safety(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)

    async def fake_discover_maso(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            [
                StreamModelOption(id="gemini-4-argon", label="gemini-4-argon"),
                StreamModelOption(id="gpt-6-astra", label="gpt-6-astra"),
            ],
            "gemini-4-argon",
        )

    monkeypatch.setattr(cross_reviewer, "discover_maso_service", fake_discover_maso)
    monkeypatch.setattr(maso_discovery, "discover_maso_service", fake_discover_maso)

    ws = manager.create_workspace(
        WorkspaceCreate(name="Round3 Repo", path=str(repo), default_branch="main")
    )

    # 1. Task starts with auto_worktree=False; while _choose_dispatch_target runs on attempt 0,
    # a concurrent TODO edit flips auto_worktree=True -> start_task retries and provisions worktree!
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Flip to auto worktree",
            prompt="Start as standard then flip to auto_worktree",
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            auto_worktree=False,
        ),
    )
    orig_choose = manager._choose_dispatch_target
    flipped = False

    async def choose_and_flip(workspace_arg, staged_arg, payload_arg):
        nonlocal flipped
        res = await orig_choose(workspace_arg, staged_arg, payload_arg)
        if not flipped:
            flipped = True
            live = manager.tasks[task.id]
            manager.tasks[task.id] = live.model_copy(
                update={
                    "recipe": WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
                    "auto_worktree": True,
                }
            )
        return res

    monkeypatch.setattr(manager, "_choose_dispatch_target", choose_and_flip)
    started = await manager.start_task(task.id)
    monkeypatch.setattr(manager, "_choose_dispatch_target", orig_choose)

    assert started.auto_worktree is True
    assert started.worktree_path is not None
    assert Path(started.worktree_path).resolve() != repo.resolve()
    assert started.session_id is not None
    assigned_worker = manager.sessions[started.session_id]
    assert Path(assigned_worker.workspace_path).resolve() == Path(started.worktree_path).resolve()

    # 2. Reviewer ID reservation & incarnation safety when Task A is accepted after Task B
    # spawns its own reviewer: accepting Task A must NEVER delete Task B's reviewer!
    await manager.create_report(
        assigned_worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            call_id="r3-work-1",
            message="Done task A",
            changed_files=["a.py"],
            validation="pytest",
            risks="none",
            review_decision=ReviewDecision.REQUEST,
        ),
    )
    rev_a_id = manager.tasks[task.id].review_session_id
    assert rev_a_id is not None
    await manager.create_report(
        rev_a_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_PASSED,
            call_id="r3-rev-1",
            message="Pass task A",
        ),
    )
    assert rev_a_id not in manager.sessions

    # Now create Task B and spawn a new reviewer while Task A is still in REVIEW
    task_b = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Task B",
            prompt="Concurrent task B",
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            auto_worktree=False,
        ),
    )
    rev_b = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.REVIEWER,
            ephemeral=True,
            caller_owned_ephemeral=False,
            reuse_existing=False,
            cwd=str(repo),
            env={"MASO_BASE_URL": "http://127.0.0.1:11004", "MASO_MODEL": "gemini-4-argon"},
        ),
    )
    # Task B's reviewer must not collide with Task A's reserved review_session_id
    assert rev_b.id != rev_a_id
    manager.tasks[task_b.id] = task_b.model_copy(
        update={"status": WorkspaceTaskStatus.REVIEW, "review_session_id": rev_b.id}
    )

    # Accepting Task A transitions Task A to DONE and leaves Task B's reviewer untouched
    await manager.update_task(task.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE))
    assert rev_b.id in manager.sessions
