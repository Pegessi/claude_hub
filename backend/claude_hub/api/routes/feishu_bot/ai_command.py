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
    workspace_id: Optional[str] = Field(default=None, max_length=128)
    watch_chats: Optional[List[str]] = None
    poll_interval_seconds: Optional[float] = Field(default=None, ge=2.0, le=300.0)
    max_active_chats: Optional[int] = Field(default=None, ge=1, le=50)
    max_context_messages: Optional[int] = Field(default=None, ge=1, le=100)
    ack_emoji: Optional[str] = Field(default=None, max_length=64)
    done_emoji: Optional[str] = Field(default=None, max_length=64)
    footer: Optional[str] = Field(default=None, max_length=256)
    reply_in_thread: Optional[bool] = None
    send_mode: Optional[str] = Field(default=None, max_length=32)


class FeishuAiWebSessionLoginStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str = Field(default="feishu", max_length=32)


class FeishuAiWebSessionLoginPollInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_id: Optional[str] = Field(default=None, max_length=128)
    switch_owner: bool = False


class FeishuAiWebSessionImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Optional[str] = Field(default=None, max_length=1024)
    switch_owner: bool = False


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
    cfg = service.get_config(auto_import_lark_cli=auto_import_lark_cli)
    preflight = service.preflight_status(auto_import_lark_cli=auto_import_lark_cli, config=cfg)
    return {
        "config": cfg.to_dict(),
        "token_status": preflight.to_dict(),
        "web_session_status": service.web_login_manager.status_dict(),
        "poller_status": poller.status_dict(),
        "capabilities": service.capabilities_dict(),
    }


async def abuild_ai_command_status_payload(
    service: FeishuAiCommandService,
    poller: FeishuAiPoller,
    *,
    auto_import_lark_cli: bool = True,
) -> dict[str, Any]:
    cfg, preflight = await service.aget_config_and_preflight(
        auto_import_lark_cli=auto_import_lark_cli
    )
    capabilities = (
        await service.acapabilities_dict()
        if hasattr(service, "acapabilities_dict")
        else service.capabilities_dict()
    )
    return {
        "config": cfg.to_dict(),
        "token_status": preflight.to_dict(),
        "web_session_status": service.web_login_manager.status_dict(),
        "poller_status": poller.status_dict(),
        "capabilities": capabilities,
    }


def register_ai_command_routes(
    router: APIRouter,
    *,
    require_hub_user: Callable[..., Any],
    service_getter: Callable[[], FeishuAiCommandService] | None = None,
    poller_getter: Callable[[], FeishuAiPoller] | None = None,
) -> None:
    """Register ``/api/feishu/bot/ai-command`` endpoints on the Feishu router."""

    def _get_service() -> FeishuAiCommandService:
        if service_getter is not None:
            return service_getter()
        return get_feishu_ai_command_service()

    def _get_poller() -> FeishuAiPoller:
        if poller_getter is not None:
            return poller_getter()
        return get_feishu_ai_poller()

    @router.get("/ai-command")
    async def get_ai_command_status(
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        return await abuild_ai_command_status_payload(service, poller)

    @router.put("/ai-command")
    @router.patch("/ai-command")
    async def update_ai_command_config(
        body: FeishuAiCommandPatchInput,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        try:
            patch_dict = body.model_dump(exclude_none=True)
            service.update_config(patch_dict)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_ai_command_config", "message": str(exc)},
            ) from exc
        await poller.reconcile()
        return await abuild_ai_command_status_payload(service, poller)

    @router.post("/ai-command/import-token")
    async def import_lark_cli_user_token(
        body: Optional[FeishuAiCommandTokenImportInput] = None,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        explicit_app_id = (body.app_id.strip() if body and body.app_id else "") or None
        explicit_open_id = (body.open_id.strip() if body and body.open_id else "") or None
        explicit_profile = (body.profile.strip() if body and body.profile else "") or None
        imported = await service._token_store.aimport_from_lark_cli(
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
        payload = await abuild_ai_command_status_payload(
            service, poller, auto_import_lark_cli=False
        )
        payload["imported"] = imported is not None
        return payload

    @router.post("/ai-command/dry-run")
    async def dry_run_ai_command(
        body: FeishuAiCommandDryRunInput,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        cfg, preflight = await service.aget_config_and_preflight()
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
            config=cfg,
        )
        return {
            "dry_run": result.to_dict(),
            "token_status": preflight.to_dict(),
        }

    @router.get("/ai-command/auth-guide")
    async def get_ai_command_auth_guide(
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        preflight = await service.apreflight_status()
        return {
            "token_status": preflight.to_dict(),
            "auth_login_command": preflight.auth_login_command,
            "remediation_steps": list(preflight.remediation_steps),
        }

    @router.get("/ai-command/web-session")
    @router.get("/ai-command/web-session/status")
    async def get_ai_command_web_session_status(
        validate: bool = False,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        web_status = await service.web_login_manager.astatus_dict(validate=validate)
        payload = await abuild_ai_command_status_payload(
            service, poller, auto_import_lark_cli=False
        )
        payload["web_session_status"] = web_status
        return payload

    @router.post("/ai-command/web-session/login/start")
    async def start_ai_command_web_session_login(
        body: Optional[FeishuAiWebSessionLoginStartInput] = None,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        region = (body.region if body is not None else "feishu").strip() or "feishu"
        try:
            qr_session = await service.web_login_manager.start_login(region=region)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "web_login_start_failed", "message": str(exc)},
            ) from exc
        return {
            "status": "waiting",
            "login": qr_session.to_public_dict(),
            "web_session_status": service.web_login_manager.status_dict(),
        }

    @router.post("/ai-command/web-session/login/poll")
    async def poll_ai_command_web_session_login(
        body: Optional[FeishuAiWebSessionLoginPollInput] = None,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        login_id = (body.login_id.strip() if body and body.login_id else "") or None
        try:
            st, sess, qr = await service.web_login_manager.poll_login(login_id=login_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "web_login_poll_failed", "message": str(exc)},
            ) from exc
        if st == "done" and sess is not None:
            if body and body.switch_owner:
                service.switch_owner_to_web_session()
            await poller.reconcile()
        payload = await abuild_ai_command_status_payload(
            service, poller, auto_import_lark_cli=False
        )
        payload["login_status"] = st
        payload["login"] = qr.to_public_dict() if (qr is not None and st == "waiting") else None
        return payload

    @router.post("/ai-command/web-session/import-larkx")
    async def import_ai_command_web_session_larkx(
        body: Optional[FeishuAiWebSessionImportInput] = None,
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        custom_path = (body.path.strip() if body and body.path else "") or None
        imported = service.web_session_store.import_from_larkx(custom_path)
        if imported is not None:
            if body and body.switch_owner:
                service.switch_owner_to_web_session()
            await poller.reconcile()
        payload = await abuild_ai_command_status_payload(
            service, poller, auto_import_lark_cli=False
        )
        payload["imported"] = imported is not None
        return payload

    @router.post("/ai-command/web-session/logout")
    @router.delete("/ai-command/web-session")
    async def logout_ai_command_web_session(
        _user: Any = Depends(require_hub_user),
    ) -> dict[str, Any]:
        service = _get_service()
        poller = _get_poller()
        try:
            cleared = service.web_login_manager.logout()
        except OSError as exc:
            await poller.reconcile()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete Feishu web session file: {exc}",
            ) from exc
        await poller.reconcile()
        payload = await abuild_ai_command_status_payload(
            service, poller, auto_import_lark_cli=False
        )
        payload["cleared"] = cleared
        return payload
