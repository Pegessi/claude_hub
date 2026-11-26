"""Execution settings for background Feishu WebSocket triage."""

import asyncio
from dataclasses import replace
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from claude_hub.services.feishu_ai_command_models import validate_ai_command_agent_config
from claude_hub.services.feishu_ai_poller import get_feishu_ai_poller
from claude_hub.services.feishu_realtime_agent import resolve_realtime_agent
from claude_hub.services.feishu_realtime_matching import FeishuRealtimeEventConfig
from claude_hub.services.feishu_realtime_settings import write_realtime_settings


class RealtimePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)
    reasoning_effort: str | None = Field(default=None, max_length=32)
    workspace_id: str | None = Field(default=None, max_length=128)
    p2p_debounce_seconds: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    p2p_active_seconds: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)


async def realtime_settings_payload() -> dict[str, Any]:
    config = await asyncio.to_thread(FeishuRealtimeEventConfig.from_runtime)
    values = {key: getattr(config, key) for key in RealtimePatch.model_fields}
    resolution_error = None
    effective_agent = None
    try:
        agent = await resolve_realtime_agent(config)
        effective_agent = {
            "provider": agent.provider.value,
            "model": agent.model,
            "reasoning_effort": agent.reasoning_effort,
        }
    except Exception:
        resolution_error = "Execution agent unavailable; check the provider and model settings."
    return {
        "config": values,
        "status": await asyncio.to_thread(get_feishu_ai_poller().realtime_status),
        "resolution_error": resolution_error,
        "effective_agent": effective_agent,
    }


def register_realtime_routes(router: APIRouter, *, require_hub_user: Callable[..., Any]) -> None:
    @router.get("/realtime-events")
    async def get_settings(_user: Any = Depends(require_hub_user)) -> dict[str, Any]:
        return await realtime_settings_payload()

    @router.patch("/realtime-events")
    async def patch_settings(
        patch: RealtimePatch, _user: Any = Depends(require_hub_user)
    ) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise HTTPException(
                422, "Settings cannot be null; use an empty string to clear a selection"
            )
        config = replace(FeishuRealtimeEventConfig.from_runtime(), **changes)
        try:
            validate_ai_command_agent_config(config.provider, "", config.reasoning_effort)
            if config.model:
                await resolve_realtime_agent(config)
            if config.workspace_id:
                from claude_hub.models import ExecutionTarget
                from claude_hub.services.workspace_manager import workspace_manager

                workspace = workspace_manager.get_workspace(config.workspace_id)
                if workspace is None or workspace.target != ExecutionTarget.LOCAL:
                    raise ValueError("Choose an existing local Workspace")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(422, "Could not validate the live provider model") from exc
        try:
            await asyncio.to_thread(write_realtime_settings, changes)
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "Could not save realtime settings") from exc
        return await realtime_settings_payload()
