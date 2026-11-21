"""Bounded, read-only recommendations for the daily digest."""

import asyncio

from claude_hub.models import EnsureWorkspaceAgentRequest, SessionKind, WorkspaceSessionRole

from .feishu_triage import (
    _archive_patrol_session,
    _find_retained_patrol_session,
    _resolve_patrol_workspace_id,
    _run_workspace_triage_turn,
    patrol_provider_env,
    workspace_manager,
)


async def recommend_daily_decisions(prompt: str, workspace_id: str) -> str:
    agent_type, env = await patrol_provider_env(prefer_low_cost=True)
    target = _resolve_patrol_workspace_id(workspace_id)
    title = "Patrol daily decision brief"
    retained = _find_retained_patrol_session(target, title)
    if retained is not None:
        await _archive_patrol_session(retained.workspace_id, retained.id)
    session = await workspace_manager.ensure_workspace_agent(
        target,
        EnsureWorkspaceAgentRequest(
            agent_type=agent_type,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.WORKER,
            title=title,
            solo_mode=False,
            reuse_existing=False,
            ephemeral=True,
            caller_owned_ephemeral=True,
            env=env,
        ),
        bootstrap=False,
    )
    try:
        return await _run_workspace_triage_turn(session, prompt, timeout_seconds=25)
    finally:
        await asyncio.shield(_archive_patrol_session(session.workspace_id, session.id))
