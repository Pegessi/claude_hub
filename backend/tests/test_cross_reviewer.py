"""Tests for heterogeneous cross-reviewer routing and temporary reviewer lifecycle."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Dict, Optional

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSessionStatus,
    ReviewDecision,
    SessionKind,
    StartTaskRequest,
    StreamModelOption,
    StreamReasoningEffortOption,
    TerminalTab,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskRecipe,
    WorkspaceTaskStatus,
)
from claude_hub.services import cross_reviewer, managed_chat
from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable
from claude_hub.services.cross_reviewer import (
    DEFAULT_ARGON_REVIEW_MODEL,
    DEFAULT_ASTRA_REVIEW_MODEL,
    is_heterogeneous_reviewer,
    maso_model_family,
    resolve_cross_reviewer_target,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


def _maso_catalog(
    *, include_argon: bool = True, include_astra: bool = True, include_deepseek: bool = False
) -> list[StreamModelOption]:
    models: list[StreamModelOption] = []
    if include_argon:
        models.append(
            StreamModelOption(
                id="gemini-4-argon",
                label="gemini-4-argon",
                provider_model_id="gemini-4-argon",
                supported_reasoning_efforts=[
                    StreamReasoningEffortOption(
                        id="high",
                        label="High",
                        provider_model_id="gemini-4-argon_reasoning_high",
                    )
                ],
            )
        )
    if include_astra:
        models.append(
            StreamModelOption(
                id="gpt-6-astra",
                label="gpt-6-astra",
                provider_model_id="gpt-6-astra",
                supported_reasoning_efforts=[
                    StreamReasoningEffortOption(
                        id="high",
                        label="High",
                        provider_model_id="gpt-6-astra_reasoning_high",
                    )
                ],
            )
        )
    if include_deepseek:
        models.append(
            StreamModelOption(
                id="deepseek-v41-flash",
                label="deepseek-v41-flash",
                provider_model_id="deepseek-v41-flash",
                supported_reasoning_efforts=[
                    StreamReasoningEffortOption(
                        id="high",
                        label="High",
                        provider_model_id="deepseek-v41-flash-reasoning-high",
                    )
                ],
            )
        )
    return models


def test_heterogeneity_matrix() -> None:
    assert maso_model_family({"MASO_MODEL": "gemini-4-argon_reasoning_high"}) == "argon"
    assert maso_model_family({"MASO_MODEL": "gpt-6-astra_reasoning_high"}) == "astra"
    assert maso_model_family({"MASO_MODEL": "deepseek-v41-flash-reasoning"}) == "deepseek"
    assert maso_model_family({"MASO_MODEL": "seed-nightly-reasoning-high"}) == "seed"
    assert maso_model_family({}) == ""

    # Different agent_type -> True
    assert is_heterogeneous_reviewer(
        AgentType.CLAUDE,
        {"ANTHROPIC_MODEL": "claude-opus-4-6"},
        AgentType.MASO,
        {"MASO_MODEL": DEFAULT_ARGON_REVIEW_MODEL},
    )
    assert is_heterogeneous_reviewer(
        AgentType.CODEX,
        {},
        AgentType.CLAUDE,
        {},
    )

    # Both MASO with different model families -> True
    assert is_heterogeneous_reviewer(
        AgentType.MASO,
        {"MASO_MODEL": "gemini-4-argon_reasoning_high"},
        AgentType.MASO,
        {"MASO_MODEL": "gpt-6-astra_reasoning_high"},
    )
    assert is_heterogeneous_reviewer(
        AgentType.MASO,
        {"MASO_MODEL": "gpt-6-astra"},
        AgentType.MASO,
        {"MASO_MODEL": "gemini-4-argon_reasoning_medium"},
    )

    # Same provider + same model family -> False
    assert not is_heterogeneous_reviewer(
        AgentType.MASO,
        {"MASO_MODEL": "gemini-4-argon"},
        AgentType.MASO,
        {"MASO_MODEL": "gemini-4-argon_reasoning_high"},
    )
    assert not is_heterogeneous_reviewer(
        AgentType.MASO,
        {"MASO_MODEL": "gpt-6-astra"},
        AgentType.MASO,
        {"MASO_MODEL": "gpt-6-astra_reasoning_xhigh"},
    )
    assert not is_heterogeneous_reviewer(
        AgentType.CLAUDE,
        {"ANTHROPIC_MODEL": "sonnet"},
        AgentType.CLAUDE,
        {"ANTHROPIC_MODEL": "opus"},
    )
    assert not is_heterogeneous_reviewer(
        AgentType.CODEX,
        {},
        AgentType.CODEX,
        {},
    )


@pytest.mark.asyncio
async def test_resolve_cross_reviewer_target_pairing_and_fallbacks(
    monkeypatch: MonkeyPatch,
) -> None:
    # By default in pytest without custom probe or env flag, live network probing is disabled.
    monkeypatch.delenv("CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS", raising=False)
    assert await resolve_cross_reviewer_target(AgentType.CLAUDE, {}) is None

    async def fake_probe(env: dict[str, str]):
        return ("http://127.0.0.1:11004", _maso_catalog(), "gemini-4-argon")

    # 1. MASO Argon worker -> paired with MASO Astra
    target_for_argon = await resolve_cross_reviewer_target(
        AgentType.MASO,
        {"MASO_BASE_URL": "http://127.0.0.1:11004", "MASO_MODEL": "gemini-4-argon_reasoning_high"},
        maso_probe=fake_probe,
    )
    assert target_for_argon is not None
    assert target_for_argon.agent_type == AgentType.MASO
    assert target_for_argon.session_kind == SessionKind.CHAT
    assert target_for_argon.env == {
        "MASO_BASE_URL": "http://127.0.0.1:11004",
        "MASO_MODEL": DEFAULT_ASTRA_REVIEW_MODEL,
    }

    # 2. MASO Astra worker -> paired with MASO Argon
    target_for_astra = await resolve_cross_reviewer_target(
        AgentType.MASO,
        {"MASO_BASE_URL": "http://127.0.0.1:11004", "MASO_MODEL": "gpt-6-astra_reasoning_high"},
        maso_probe=fake_probe,
    )
    assert target_for_astra is not None
    assert target_for_astra.env["MASO_MODEL"] == DEFAULT_ARGON_REVIEW_MODEL

    # 3. Claude / Codex / Cursor workers -> paired with MASO Argon without leaking worker secrets
    for provider in (AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR, AgentType.TRAEX):
        target = await resolve_cross_reviewer_target(
            provider,
            {"ANTHROPIC_API_KEY": "secret-do-not-leak", "OPENAI_API_KEY": "secret-2"},
            maso_probe=fake_probe,
        )
        assert target is not None
        assert target.agent_type == AgentType.MASO
        assert target.env == {
            "MASO_BASE_URL": "http://127.0.0.1:11004",
            "MASO_MODEL": DEFAULT_ARGON_REVIEW_MODEL,
        }
        assert "ANTHROPIC_API_KEY" not in target.env
        assert "OPENAI_API_KEY" not in target.env

    # 4. Fallback when Argon is absent from catalog -> picks Astra for Claude worker
    async def astra_only_probe(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            _maso_catalog(include_argon=False, include_astra=True),
            "gpt-6-astra",
        )

    fallback_target = await resolve_cross_reviewer_target(
        AgentType.CLAUDE,
        {},
        maso_probe=astra_only_probe,
    )
    assert fallback_target is not None
    assert fallback_target.env["MASO_MODEL"] == DEFAULT_ASTRA_REVIEW_MODEL

    # 5. Fallback when Astra is absent for Argon worker -> picks DeepSeek reasoning
    async def argon_and_deepseek_probe(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            _maso_catalog(include_argon=True, include_astra=False, include_deepseek=True),
            "gemini-4-argon",
        )

    alt_target = await resolve_cross_reviewer_target(
        AgentType.MASO,
        {"MASO_MODEL": "gemini-4-argon_reasoning_high"},
        maso_probe=argon_and_deepseek_probe,
    )
    assert alt_target is not None
    assert alt_target.env["MASO_MODEL"] == "deepseek-v41-flash-reasoning-high"

    # 6. Offline / unavailable MASO -> returns None gracefully
    async def failing_probe(_env: dict[str, str]):
        raise MasoServiceUnavailable("no local maso")

    assert (
        await resolve_cross_reviewer_target(AgentType.CLAUDE, {}, maso_probe=failing_probe) is None
    )


@pytest.mark.asyncio
async def test_workspace_manager_prefers_heterogeneous_reviewer_and_cleans_up_ephemeral_on_pass(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    index = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index)

    repo = tmp_path / "repo"
    repo.mkdir()

    tabs = _wm.ttyd_manager
    monkeypatch.setattr(tabs, "processes", {})
    monkeypatch.setattr(tabs, "_tab_order", [])
    monkeypatch.setattr(tabs, "_status_snapshots", {})
    monkeypatch.setattr(tabs, "_status_cache", {})
    monkeypatch.setattr(tabs, "_start_locks", {})
    monkeypatch.setattr(tabs, "_save_state", lambda: None)
    monkeypatch.setattr(tabs, "_save_order", lambda: None)
    monkeypatch.setattr(tabs, "_get_next_port", lambda: 24000 + len(tabs.processes))

    deleted_tabs: list[str] = []
    orig_delete_tab = tabs.delete_tab

    async def tracking_delete_tab(tab_id: str) -> bool:
        deleted_tabs.append(tab_id)
        return bool(await orig_delete_tab(tab_id))

    monkeypatch.setattr(tabs, "delete_tab", tracking_delete_tab)

    async def fake_discover_maso(_env: dict[str, str]):
        return ("http://127.0.0.1:11004", _maso_catalog(), "gemini-4-argon")

    from claude_hub.services import maso_discovery

    monkeypatch.setattr(maso_discovery, "discover_maso_service", fake_discover_maso)
    monkeypatch.setattr(cross_reviewer, "discover_maso_service", fake_discover_maso)

    sent_calls: list[tuple[str, str]] = []

    async def fake_send(session, text, attachments, call_id):
        sent_calls.append((session.id, call_id))

    monkeypatch.setattr(managed_chat, "send_message", fake_send)
    monkeypatch.setattr(managed_chat, "delivery_state", lambda _s, _cid: "accepted")
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda _s: False)
    monkeypatch.setattr(managed_chat, "runtime_status", lambda _s: AgentRuntimeStatus.IDLE)

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: "")

    workspace = manager.create_workspace(
        WorkspaceCreate(name="Cross Review Repo", path=str(repo), target=ExecutionTarget.LOCAL)
    )

    # Create a MASO Argon worker session
    worker = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            cwd=str(repo),
            env={
                "MASO_BASE_URL": "http://127.0.0.1:11004",
                "MASO_MODEL": "gemini-4-argon_reasoning_high",
            },
            reuse_existing=False,
        ),
    )

    # Create two idle reviewers: one same-model (Argon) created earlier, and one heterogeneous (Astra)
    same_model_reviewer = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.REVIEWER,
            cwd=str(repo),
            env={
                "MASO_BASE_URL": "http://127.0.0.1:11004",
                "MASO_MODEL": "gemini-4-argon_reasoning_high",
            },
            reuse_existing=False,
        ),
    )
    hetero_reviewer = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.REVIEWER,
            cwd=str(repo),
            env={
                "MASO_BASE_URL": "http://127.0.0.1:11004",
                "MASO_MODEL": "gpt-6-astra_reasoning_high",
            },
            reuse_existing=False,
        ),
    )

    for s_id in (worker.id, same_model_reviewer.id, hetero_reviewer.id):
        manager.sessions[s_id] = manager.sessions[s_id].model_copy(
            update={"status": ManagedSessionStatus.IDLE, "runtime_status": AgentRuntimeStatus.IDLE}
        )

    task1 = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Task preferring idle heterogeneous reviewer",
            prompt="Implement feature A",
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            task_mode=WorkspaceTaskMode.REVIEWED,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            auto_worktree=False,
            auto_push_remote=False,
        ),
    )
    manager.tasks[task1.id] = task1.model_copy(update={"session_id": worker.id})
    selected = await manager._select_or_create_reviewer(
        workspace,
        manager.tasks[task1.id],
    )
    assert selected.id == hetero_reviewer.id
    manager.tasks[task1.id] = task1

    # Now remove idle reviewers and verify auto-spawned temporary cross-reviewer uses Astra
    manager.sessions.pop(same_model_reviewer.id, None)
    manager.sessions.pop(hetero_reviewer.id, None)

    started = await manager.start_task(task1.id, StartTaskRequest(target_session_id=worker.id))
    assert started.status == WorkspaceTaskStatus.WORKING

    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task1.id,
            state=AgentReportState.COMPLETED,
            call_id="worker-complete-1",
            message="Completed feature A",
            changed_files=["a.py"],
            validation="pytest -q",
            risks="none",
            review_decision=ReviewDecision.REQUEST,
        ),
    )

    in_review = manager.tasks[task1.id]
    assert in_review.status == WorkspaceTaskStatus.REVIEW
    spawned_reviewer_id = in_review.review_session_id
    assert spawned_reviewer_id is not None
    spawned_reviewer = manager.sessions[spawned_reviewer_id]
    assert spawned_reviewer.agent_type == AgentType.MASO
    assert spawned_reviewer.session_kind == SessionKind.CHAT
    assert spawned_reviewer.env["MASO_MODEL"] == DEFAULT_ASTRA_REVIEW_MODEL
    assert spawned_reviewer.ephemeral is True
    assert spawned_reviewer.caller_owned_ephemeral is False

    # When the temporary cross-reviewer posts REVIEW_PASSED, it is auto-deleted
    # while task.review_session_id remains recorded for audit.
    await manager.create_report(
        spawned_reviewer.id,
        AgentReportCreate(
            task_id=task1.id,
            state=AgentReportState.REVIEW_PASSED,
            call_id="reviewer-pass-1",
            message="LGTM, verified heterogeneously.",
        ),
    )

    after_pass = manager.tasks[task1.id]
    assert after_pass.status == WorkspaceTaskStatus.REVIEW
    assert after_pass.human_acceptance_requested_at is not None
    assert after_pass.review_session_id == spawned_reviewer_id
    assert spawned_reviewer_id not in manager.sessions
    assert spawned_reviewer.tab_id in deleted_tabs
