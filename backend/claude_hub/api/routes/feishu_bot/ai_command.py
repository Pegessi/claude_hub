"""REST API routes for Feishu personal ``/ai`` command configuration and diagnostics."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from claude_hub.services.feishu_ai_command import (
    FeishuAiCommandService,
    get_feishu_ai_command_service,
)
from claude_hub.services.feishu_ai_poller import (
    FeishuAiPoller,
    get_feishu_ai_poller,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage


class FeishuAiCommandPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    prefix: Optional[str] = Field(default=None, max_length=32)
    owner_open_id: Optional[str] = Field(default=None, max_length=128)
    app_id: Optional[str] = Field(default=None, max_length=128)
    provider: Optional[str] = Field(default=None, max_length=32)
    model: Optional[str] = Field(default=None, max_length=128)
    reasoning_effort: Optional[str] = Field(default=None, max_length=32)
    cwd: Optional[str] = Field(default=None, max_length=1024)
    watch_chats: Optional[List[str]] = None
    poll_interval_seconds: Optional[float] = Field(default=None, ge=2.0, le=300.0)
    max_active_chats: Optional[int] = Field(default=None, ge=1, le=50)
    max_context_messages: Optional[int] = Field(default=None, ge=1, le=100)
    ack_emoji: Optional[str] = Field(default=None, max_length=64)
    done_emoji: Optional[str] = Field(default=None, max_length=64)
    footer: Optional[str] = Field(default=None, max_length=256)
    reply_in_thread: Optional[bool] = None


class FeishuAiCommandDryRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10_000)
    chat_id: str = Field(default="oc_dry_run_chat", max_length=128)
    thread_id: Optional[str] = Field(default="omt_dry_run_thread", max_length=128)
    message_id: str = Field(default="om_dry_run_msg", max_length=128)
    sender_open_id: Optional[str] = Field(default=None, max_length=128)
    context_messages: List[str] = Field(default_factory=list, max_length=50)


class FeishuAiCommandTokenImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: Optional[str] = Field(default=None, max_length=128)
    open_id: Optional[str] = Field(default=None, max_length=128)
    profile: Optional[str] = Field(default=None, max_length=128)


def build_ai_command_status_payload(
    service: FeishuAiCommandService,
    poller: FeishuAiPoller,
    *,
    auto_import_lark_cli: bool = True,
) -> dict[str, Any]:
    cfg = service.get_config()
    preflight = service.preflight_status(auto_import_lark_cli=auto_import_lark_cli)
    return {
        "config": cfg.to_dict(),
        "token_status": preflight.to_dict(),
        "poller_status": poller.status_dict(),
        "capabilities": service.capabilities_dict(),
    }


def register_ai_command_routes(
    router: APIRouter,
    *,
    require_hub_user: Callable[..., Any],
    service_getter: Callable[[], FeishuAiCommandService] = get_feishu_ai_command_service,
    poller_getter: Callable[[], FeishuAiPoller] = get_feishu_ai_poller,
) -> None:
    """Register ``/api/feishu/bot/ai-command`` endpoints on the Feishu router."""

    @router.get("/ai-command")
    async def get_ai_command_status(
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = service_getter()
        poller = poller_getter()
        return build_ai_command_status_payload(service, poller)

    @router.put("/ai-command")
    @router.patch("/ai-command")
    async def update_ai_command_config(
        body: FeishuAiCommandPatchInput,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = service_getter()
        poller = poller_getter()
        try:
            patch_dict = body.model_dump(exclude_none=True)
            service.update_config(patch_dict)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_ai_command_config", "message": str(exc)},
            ) from exc
        await poller.reconcile()
        return build_ai_command_status_payload(service, poller)

    @router.post("/ai-command/import-token")
    async def import_lark_cli_user_token(
        body: Optional[FeishuAiCommandTokenImportInput] = None,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = service_getter()
        poller = poller_getter()
        explicit_app_id = (body.app_id.strip() if body and body.app_id else "") or None
        explicit_open_id = (body.open_id.strip() if body and body.open_id else "") or None
        explicit_profile = (body.profile.strip() if body and body.profile else "") or None
        imported = service._token_store.import_from_lark_cli(
            app_id=explicit_app_id,
            open_id=explicit_open_id,
            profile=explicit_profile,
        )
        if imported is not None:
            service.update_config(
                {
                    "owner_open_id": imported.open_id,
                    "app_id": imported.app_id,
                }
            )
        await poller.reconcile()
        payload = build_ai_command_status_payload(service, poller, auto_import_lark_cli=False)
        payload["imported"] = imported is not None
        return payload

    @router.post("/ai-command/dry-run")
    async def dry_run_ai_command(
        body: FeishuAiCommandDryRunInput,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = service_getter()
        cfg = service.get_config()
        sender = (body.sender_open_id or cfg.owner_open_id or "ou_dry_run_owner").strip()
        msg = FeishuUserMessage(
            message_id=body.message_id,
            chat_id=body.chat_id,
            sender_open_id=sender,
            sender_type="user",
            msg_type="text",
            text=body.text,
            create_time_ms=0,
            thread_id=body.thread_id,
        )
        seeded = [
            FeishuUserMessage(
                message_id=f"om_ctx_{idx}",
                chat_id=body.chat_id,
                sender_open_id="ou_context_peer",
                sender_type="user",
                msg_type="text",
                text=ctx_text,
                create_time_ms=idx * 1000,
                thread_id=body.thread_id,
            )
            for idx, ctx_text in enumerate(body.context_messages, start=1)
        ]
        result = await service.handle_message(
            msg,
            source="dry_run",
            dry_run=True,
            seeded_context=seeded,
        )
        preflight = service.preflight_status()
        return {
            "dry_run": result.to_dict(),
            "token_status": preflight.to_dict(),
        }

    @router.get("/ai-command/auth-guide")
    async def get_ai_command_auth_guide(
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = service_getter()
        preflight = service.preflight_status()
        return {
            "token_status": preflight.to_dict(),
            "auth_login_command": preflight.auth_login_command,
            "remediation_steps": list(preflight.remediation_steps),
        }
