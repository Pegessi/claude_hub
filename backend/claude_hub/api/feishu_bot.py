"""Authenticated Feishu Bot binding and event callback routes."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from claude_hub.api.agent_stream import CHAT_ERROR_REASON_HEADER, dispatch_tab_chat_and_wait
from claude_hub.auth.dependencies import get_current_user_from_cookie
from claude_hub.config import settings
from claude_hub.models import ExecutionTarget, SessionKind, User
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V1,
    format_feishu_provider_text_v1,
)
from claude_hub.services.feishu_bot import (
    MAX_FEISHU_EVENT_BYTES,
    BindingCodeError,
    BindingRateLimitError,
    FeishuBinding,
    FeishuBindingStore,
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotConfigurationError,
    FeishuEventPayloadError,
    FeishuEventVerificationError,
    FeishuMessageEvent,
    parse_feishu_callback,
)
from claude_hub.services.public_base_url import get_public_base_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feishu/bot", tags=["feishu-bot"])
_BIND_CODE_RE = re.compile(r"^(?:bind\s+)?(CH-[A-Z0-9]{10})$", re.IGNORECASE)
_binding_store = FeishuBindingStore()


class FeishuBindStartRequest(BaseModel):
    """An existing direct Chat tab selected by the authenticated Web user."""

    tab_id: str = Field(..., min_length=1, max_length=128)
    workspace_id: str | None = Field(None, min_length=1, max_length=128)


class FeishuBindStartResponse(BaseModel):
    code: str
    expires_at: datetime
    event_url: str


class FeishuBindingResponse(BaseModel):
    owner_open_id: str
    sender_open_id: str
    app_id: str
    chat_id: str
    tab_id: str
    workspace_id: str | None
    created_at: datetime


class FeishuBindingEnvelope(BaseModel):
    binding: FeishuBindingResponse | None


async def require_real_feishu_user(
    session_id: str | None = Cookie(None, alias=settings.session_cookie_name),
) -> User:
    """Require a real stored login session, never the local-network identity."""

    if not settings.auth_enabled or not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = await get_current_user_from_cookie(session_id)
    if user is None or user.open_id == "local":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def _owner_is_authorized(open_id: str, email: str) -> bool:
    """Re-evaluate a binding owner against the current OAuth allowlist."""

    if not settings.auth_enabled or open_id == "local":
        return False
    allowed_open_ids = settings.allowed_open_ids_list
    if allowed_open_ids:
        return open_id in allowed_open_ids
    allowed_emails = [value.lower() for value in settings.allowed_emails_list]
    if allowed_emails:
        return bool(email) and email.lower() in allowed_emails
    return True


def _bot_config() -> FeishuBotConfig:
    try:
        return FeishuBotConfig.from_env()
    except FeishuBotConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _binding_response(binding: FeishuBinding) -> FeishuBindingResponse:
    return FeishuBindingResponse(
        owner_open_id=binding.owner_open_id,
        sender_open_id=binding.sender_open_id,
        app_id=binding.app_id,
        chat_id=binding.chat_id,
        tab_id=binding.tab_id,
        workspace_id=binding.workspace_id,
        created_at=datetime.fromtimestamp(binding.created_at, tz=timezone.utc),
    )


def _validate_bind_target(tab_id: str, requested_workspace_id: str | None) -> str | None:
    tab = ttyd_manager.get_tab(tab_id)
    process = ttyd_manager.processes.get(tab_id)
    if tab is None or process is None or getattr(process, "archived", False):
        raise HTTPException(status_code=404, detail="Chat tab not found")
    if tab.session_kind != SessionKind.CHAT or tab.target != ExecutionTarget.LOCAL:
        raise HTTPException(
            status_code=400, detail="Feishu Bot requires an existing local Chat tab"
        )
    if tab.workspace_role is not None:
        raise HTTPException(
            status_code=400, detail="Managed workspace terminals are not direct Chat tabs"
        )

    actual_workspace_id = str(tab.workspace_id) if tab.workspace_id is not None else None
    if requested_workspace_id is not None and requested_workspace_id != actual_workspace_id:
        raise HTTPException(
            status_code=403, detail="Chat tab does not belong to that workspace target"
        )
    if actual_workspace_id is not None and actual_workspace_id not in workspace_manager.workspaces:
        raise HTTPException(status_code=409, detail="Chat tab workspace target no longer exists")
    return actual_workspace_id


@router.post("/bind/start", response_model=FeishuBindStartResponse, status_code=201)
async def start_feishu_binding(
    payload: FeishuBindStartRequest,
    current_user: User = Depends(require_real_feishu_user),
) -> FeishuBindStartResponse:
    """Issue a short-lived code for one authorized existing Chat target."""

    _bot_config()
    workspace_id = _validate_bind_target(payload.tab_id, payload.workspace_id)
    try:
        code, pending = _binding_store.create_code(
            current_user.open_id,
            current_user.email,
            payload.tab_id,
            workspace_id,
        )
    except BindingRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return FeishuBindStartResponse(
        code=code,
        expires_at=datetime.fromtimestamp(pending.expires_at, tz=timezone.utc),
        event_url=f"{get_public_base_url()}/api/feishu/bot/events",
    )


@router.get("/binding", response_model=FeishuBindingEnvelope)
async def read_feishu_binding(
    current_user: User = Depends(require_real_feishu_user),
) -> FeishuBindingEnvelope:
    binding = _binding_store.get_owner_binding(current_user.open_id)
    return FeishuBindingEnvelope(
        binding=_binding_response(binding) if binding is not None else None
    )


@router.delete("/binding", status_code=204)
async def delete_feishu_binding(
    current_user: User = Depends(require_real_feishu_user),
) -> None:
    _binding_store.delete_owner_binding(current_user.open_id)


def _binding_code(text: str) -> str | None:
    match = _BIND_CODE_RE.fullmatch(text.strip())
    return match.group(1).upper() if match is not None else None


def _binding_is_current(binding: FeishuBinding, config: FeishuBotConfig) -> bool:
    if binding.app_id != config.app_id:
        return False
    if not _owner_is_authorized(binding.owner_open_id, binding.owner_email):
        return False
    current = _binding_store.get_sender_binding(
        binding.sender_open_id,
        binding.app_id,
        binding.chat_id,
    )
    return current == binding


def _turn_id(message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    return f"feishu-{digest}"


def _turn_metadata(event: FeishuMessageEvent) -> dict[str, Any]:
    return {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V1,
        "feishu": {
            "app_id": event.app_id,
            "chat_id": event.chat_id,
            "message_id": event.message_id,
            "sender_open_id": event.sender_open_id,
        },
    }


def _chat_error_reason(exc: HTTPException) -> str | None:
    if exc.status_code != 409:
        return None
    return (exc.headers or {}).get(CHAT_ERROR_REASON_HEADER)


async def _send_failure(client: FeishuBotClient, message_id: str, text: str) -> None:
    try:
        await client.reply_text(message_id, text)
    except Exception:
        logger.exception("Feishu Bot could not send a failure response")


async def _handle_message_event(
    event: FeishuMessageEvent,
    config: FeishuBotConfig,
) -> None:
    client = FeishuBotClient(config)
    status_value = "failed"
    try:
        code = _binding_code(event.text)
        if code is not None:
            try:
                created_binding = _binding_store.consume_code(
                    code,
                    sender_open_id=event.sender_open_id,
                    app_id=event.app_id,
                    chat_id=event.chat_id,
                    owner_is_authorized=_owner_is_authorized,
                )
            except BindingCodeError as exc:
                await client.reply_text(event.message_id, f"绑定失败：{exc}")
                return
            try:
                _validate_bind_target(created_binding.tab_id, created_binding.workspace_id)
            except HTTPException:
                _binding_store.delete_owner_binding(created_binding.owner_open_id)
                await client.reply_text(event.message_id, "绑定失败：Claude Hub 目标当前不可用。")
                return
            if not _binding_is_current(created_binding, config):
                return
            await client.reply_text(
                event.message_id,
                f"已连接 Claude Hub Chat：{created_binding.tab_id}",
            )
            status_value = "completed"
            return

        binding = _binding_store.get_sender_binding(
            event.sender_open_id,
            event.app_id,
            event.chat_id,
        )
        if binding is None:
            await client.reply_text(
                event.message_id,
                "当前单聊尚未连接 Claude Hub，请先在 Hub 网页生成绑定码。",
            )
            return
        if not _binding_is_current(binding, config):
            _binding_store.delete_owner_binding(binding.owner_open_id)
            await client.reply_text(event.message_id, "Claude Hub 授权已失效，请在网页重新绑定。")
            return
        _validate_bind_target(binding.tab_id, binding.workspace_id)
        assistant_text = await dispatch_tab_chat_and_wait(
            binding.tab_id,
            format_feishu_provider_text_v1(
                event.text,
                app_id=event.app_id,
                chat_id=event.chat_id,
                message_id=event.message_id,
                sender_open_id=event.sender_open_id,
            ),
            _turn_id(event.message_id),
            visible_text=event.text,
            turn_metadata=_turn_metadata(event),
        )
        if not _binding_is_current(binding, config):
            logger.info("Feishu Bot reply suppressed because the binding changed during the turn")
            return
        await client.reply_text(event.message_id, assistant_text)
        status_value = "completed"
    except HTTPException as exc:
        logger.warning("Feishu Bot target rejected: status=%s", exc.status_code)
        if _chat_error_reason(exc) == "chat_busy":
            message = "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"
        elif exc.status_code == 409:
            message = (
                "Claude Hub Chat 当前不可用，本条消息尚未执行。" "请在网页检查 Chat 状态后重试。"
            )
        else:
            message = "Claude Hub 目标当前不可用，请在网页重新绑定。"
        await _send_failure(client, event.message_id, message)
    except Exception:
        logger.exception("Feishu Bot message dispatch failed")
        await _send_failure(client, event.message_id, "Claude Hub 处理消息失败，请稍后重试。")
    finally:
        _binding_store.finish_message(event.message_id, status_value)


async def _read_bounded_event_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_FEISHU_EVENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Feishu event exceeds {MAX_FEISHU_EVENT_BYTES} byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/events")
async def receive_feishu_event(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Validate and acknowledge a Feishu URL challenge or p2p message event."""

    config = _bot_config()
    raw_body = await _read_bounded_event_body(request)
    try:
        event_kind, value = parse_feishu_callback(raw_body, request.headers, config)
    except FeishuEventVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except FeishuEventPayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event_kind == "challenge":
        return {"challenge": value}

    if not isinstance(value, FeishuMessageEvent):
        raise HTTPException(status_code=400, detail="Unsupported Feishu callback")
    if not _binding_store.claim_message(value.message_id):
        return {"ok": True, "duplicate": True}
    background_tasks.add_task(_handle_message_event, value, config)
    return {"ok": True}


__all__ = ["require_real_feishu_user", "router"]
