"""Authenticated Feishu Bot binding, configuration, and event callback routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, cast

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
    BindingOwnerUnauthorizedError,
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
from claude_hub.services.feishu_bot_config import (
    EffectiveFeishuBotConfig,
    FeishuBotAppChangeConfirmationRequired,
    FeishuBotConfigRevisionError,
    FeishuBotConfigState,
    FeishuBotConfigStore,
    FeishuBotConfigStoreError,
    configured_admin_open_ids,
)
from claude_hub.services.public_base_url import get_public_base_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feishu/bot", tags=["feishu-bot"])
_BIND_CODE_RE = re.compile(r"^(?:bind\s+)?(CH-[A-Z0-9]{10})$", re.IGNORECASE)
_binding_store = FeishuBindingStore()
_config_store = FeishuBotConfigStore()
_config_publish_lock = asyncio.Lock()
_CONFIG_BODY_BYTES = 16 * 1024
_CONFIG_GATE_TIMEOUT_SECONDS = 20.0
_OUTBOUND_POST_TIMEOUT_SECONDS = 20.0
_OFFICIAL_FEISHU_API = "https://open.feishu.cn"
_PUBLIC_URL_ENV_NAMES = (
    "CLAUDE_HUB_PUBLIC_BASE_URL",
    "CLAUDE_HUB_PROVIDER_PUBLIC_URL",
)


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


class FeishuBotConfigStatusResponse(BaseModel):
    configured: bool
    source: str
    can_manage: bool
    editable: bool
    event_url: str | None
    revision: int | None


class FeishuBotConfigAdminResponse(FeishuBotConfigStatusResponse):
    app_id: str | None
    app_secret_configured: bool
    verification_token_configured: bool
    encrypt_key_configured: bool
    updated_at: datetime | None


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


async def require_feishu_bot_admin(
    current_user: User = Depends(require_real_feishu_user),
) -> User:
    if current_user.open_id not in configured_admin_open_ids():
        raise HTTPException(status_code=403, detail="feishu_bot_admin_required")
    return current_user


def _can_manage(user: User) -> bool:
    return user.open_id in configured_admin_open_ids()


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


def _bot_config() -> EffectiveFeishuBotConfig:
    try:
        return _config_store.require()
    except (FeishuBotConfigurationError, FeishuBotConfigStoreError) as exc:
        raise HTTPException(status_code=503, detail="bot_config_unavailable") from exc


def _config_state() -> FeishuBotConfigState:
    try:
        return _config_store.state()
    except (FeishuBotConfigurationError, FeishuBotConfigStoreError) as exc:
        raise HTTPException(status_code=503, detail="bot_config_unavailable") from exc


def _configured_event_url(state: FeishuBotConfigState) -> str | None:
    if not state.configured:
        return None
    if not any(os.environ.get(name, "").strip() for name in _PUBLIC_URL_ENV_NAMES):
        return None
    try:
        return f"{get_public_base_url()}/api/feishu/bot/events"
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="public_url_invalid") from exc


def _status_response(state: FeishuBotConfigState, user: User) -> FeishuBotConfigStatusResponse:
    can_manage = _can_manage(user)
    return FeishuBotConfigStatusResponse(
        configured=state.configured,
        source=state.source,
        can_manage=can_manage,
        editable=can_manage and state.editable,
        event_url=_configured_event_url(state),
        revision=state.revision,
    )


def _admin_response(state: FeishuBotConfigState, user: User) -> FeishuBotConfigAdminResponse:
    status_view = _status_response(state, user)
    config = state.config
    return FeishuBotConfigAdminResponse(
        **status_view.model_dump(),
        app_id=config.app_id if config is not None else None,
        app_secret_configured=bool(config and config.app_secret),
        verification_token_configured=bool(config and config.verification_token),
        encrypt_key_configured=bool(config and config.encrypt_key),
        updated_at=(
            datetime.fromtimestamp(state.updated_at, tz=timezone.utc)
            if state.updated_at is not None
            else None
        ),
    )


@asynccontextmanager
async def _config_gate() -> AsyncIterator[None]:
    try:
        await asyncio.wait_for(_config_publish_lock.acquire(), timeout=_CONFIG_GATE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="config_operation_busy") from exc
    try:
        yield
    finally:
        _config_publish_lock.release()


async def _read_config_json(request: Request) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _CONFIG_BODY_BYTES:
            raise HTTPException(status_code=400, detail="invalid_config_request")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_config_request") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid_config_request")
    return value


def _config_update_values(value: dict[str, Any]) -> tuple[FeishuBotConfig, int, bool]:
    expected_keys = {
        "app_id",
        "app_secret",
        "verification_token",
        "encrypt_key",
        "expected_revision",
        "allow_app_id_change",
    }
    if set(value) != expected_keys:
        raise HTTPException(status_code=400, detail="invalid_config_request")
    credentials = [
        value.get("app_id"),
        value.get("app_secret"),
        value.get("verification_token"),
        value.get("encrypt_key"),
    ]
    if not all(
        isinstance(item, str) and item.strip() and len(item) <= 4096 for item in credentials
    ):
        raise HTTPException(status_code=400, detail="invalid_config_request")
    revision = value.get("expected_revision")
    allow_change = value.get("allow_app_id_change")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(allow_change, bool)
    ):
        raise HTTPException(status_code=400, detail="invalid_config_request")
    return (
        FeishuBotConfig(
            app_id=cast(str, credentials[0]).strip(),
            app_secret=cast(str, credentials[1]).strip(),
            verification_token=cast(str, credentials[2]).strip(),
            encrypt_key=cast(str, credentials[3]).strip(),
            api_base_url=_OFFICIAL_FEISHU_API,
        ),
        revision,
        allow_change,
    )


def _ensure_stored_source(state: FeishuBotConfigState) -> None:
    if state.source not in {"stored", "none"}:
        raise HTTPException(status_code=409, detail="config_read_only")


def _ensure_oauth_app(candidate: FeishuBotConfig) -> None:
    if not settings.feishu_app_id or candidate.app_id != settings.feishu_app_id:
        raise HTTPException(status_code=409, detail="oauth_app_mismatch")


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
        raise HTTPException(
            status_code=409,
            detail="Chat tab workspace target no longer exists",
            headers={CHAT_ERROR_REASON_HEADER: "binding_target_missing"},
        )
    return actual_workspace_id


@router.get("/config/status", response_model=FeishuBotConfigStatusResponse)
async def read_feishu_bot_config_status(
    current_user: User = Depends(require_real_feishu_user),
) -> FeishuBotConfigStatusResponse:
    return _status_response(_config_state(), current_user)


@router.get("/config", response_model=FeishuBotConfigAdminResponse)
async def read_feishu_bot_config(
    current_user: User = Depends(require_feishu_bot_admin),
) -> FeishuBotConfigAdminResponse:
    return _admin_response(_config_state(), current_user)


@router.put("/config", response_model=FeishuBotConfigAdminResponse)
async def update_feishu_bot_config(
    request: Request,
    current_user: User = Depends(require_feishu_bot_admin),
) -> FeishuBotConfigAdminResponse:
    candidate, expected_revision, allow_change = _config_update_values(
        await _read_config_json(request)
    )
    before = _config_state()
    _ensure_stored_source(before)
    _ensure_oauth_app(candidate)
    if before.revision != expected_revision:
        raise HTTPException(status_code=409, detail="config_revision_conflict")
    if before.config is not None and before.config.app_id != candidate.app_id and not allow_change:
        raise HTTPException(status_code=409, detail="app_id_change_confirmation_required")
    try:
        await FeishuBotClient(candidate).validate_credentials()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="feishu_credentials_rejected") from exc
    async with _config_gate():
        current = _config_state()
        _ensure_stored_source(current)
        _ensure_oauth_app(candidate)
        if current.revision != expected_revision:
            raise HTTPException(status_code=409, detail="config_revision_conflict")
        try:
            saved = _config_store.replace(
                candidate,
                expected_revision=expected_revision,
                allow_app_id_change=allow_change,
            )
        except FeishuBotConfigRevisionError as exc:
            raise HTTPException(status_code=409, detail="config_revision_conflict") from exc
        except FeishuBotAppChangeConfirmationRequired as exc:
            raise HTTPException(
                status_code=409, detail="app_id_change_confirmation_required"
            ) from exc
        if saved.binding_generation != current.binding_generation:
            try:
                _binding_store.clear_routing_state()
            except Exception as exc:
                raise HTTPException(status_code=500, detail="routing_cleanup_failed") from exc
    return _admin_response(saved, current_user)


@router.delete("/config", response_model=FeishuBotConfigAdminResponse)
async def delete_feishu_bot_config(
    request: Request,
    current_user: User = Depends(require_feishu_bot_admin),
) -> FeishuBotConfigAdminResponse:
    value = await _read_config_json(request)
    if set(value) != {"expected_revision"}:
        raise HTTPException(status_code=400, detail="invalid_config_request")
    expected_revision = value.get("expected_revision")
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise HTTPException(status_code=400, detail="invalid_config_request")
    async with _config_gate():
        current = _config_state()
        _ensure_stored_source(current)
        if current.revision != expected_revision:
            raise HTTPException(status_code=409, detail="config_revision_conflict")
        try:
            disabled = _config_store.disable(expected_revision=expected_revision)
        except FeishuBotConfigRevisionError as exc:
            raise HTTPException(status_code=409, detail="config_revision_conflict") from exc
        try:
            _binding_store.clear_routing_state()
        except Exception as exc:
            raise HTTPException(status_code=500, detail="routing_cleanup_failed") from exc
    return _admin_response(disabled, current_user)


@router.post("/bind/start", response_model=FeishuBindStartResponse, status_code=201)
async def start_feishu_binding(
    payload: FeishuBindStartRequest,
    current_user: User = Depends(require_real_feishu_user),
) -> FeishuBindStartResponse:
    """Issue a short-lived code for one authorized existing Chat target."""

    snapshot = _bot_config()
    workspace_id = _validate_bind_target(payload.tab_id, payload.workspace_id)
    async with _config_gate():
        if not _config_store.is_current(snapshot):
            raise HTTPException(status_code=409, detail="config_revision_conflict")
        try:
            code, pending = _binding_store.create_code(
                current_user.open_id,
                current_user.email,
                payload.tab_id,
                workspace_id,
                app_id=snapshot.config.app_id,
                binding_generation=snapshot.binding_generation,
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
    snapshot = _bot_config()
    async with _config_gate():
        if not _config_store.is_current(snapshot):
            raise HTTPException(status_code=409, detail="config_revision_conflict")
        binding = _binding_store.get_owner_binding(current_user.open_id)
        if binding is not None and not _binding_is_current(binding, snapshot):
            if _binding_record_is_current(binding):
                _binding_store.delete_owner_binding(binding.owner_open_id)
            binding = None
    return FeishuBindingEnvelope(
        binding=_binding_response(binding) if binding is not None else None
    )


@router.delete("/binding", status_code=204)
async def delete_feishu_binding(
    current_user: User = Depends(require_real_feishu_user),
) -> None:
    async with _config_gate():
        _binding_store.delete_owner_binding(current_user.open_id)


def _binding_code(text: str) -> str | None:
    match = _BIND_CODE_RE.fullmatch(text.strip())
    return match.group(1).upper() if match is not None else None


def _binding_record_is_current(binding: FeishuBinding) -> bool:
    current = _binding_store.get_sender_binding(
        binding.sender_open_id, binding.app_id, binding.chat_id
    )
    return current == binding


def _binding_is_current(binding: FeishuBinding, snapshot: EffectiveFeishuBotConfig) -> bool:
    if not _config_store.is_current(snapshot):
        return False
    if binding.app_id != snapshot.config.app_id:
        return False
    if binding.binding_generation != snapshot.binding_generation:
        return False
    if not _owner_is_authorized(binding.owner_open_id, binding.owner_email):
        return False
    return _binding_record_is_current(binding)


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


async def _reply_if_current(
    client: FeishuBotClient,
    event: FeishuMessageEvent,
    snapshot: EffectiveFeishuBotConfig,
    text: str,
    *,
    binding: FeishuBinding | None = None,
) -> bool:
    try:
        # Token acquisition can block on Feishu and must stay outside the publish gate.
        token = await client.get_tenant_token()
        async with _config_gate():
            if not _config_store.is_current(snapshot):
                return False
            if binding is not None and not _binding_is_current(binding, snapshot):
                return False
            # Already-sent requests cannot be recalled or safely retried on timeout.
            await asyncio.wait_for(
                client.reply_text(event.message_id, text, access_token=token),
                timeout=_OUTBOUND_POST_TIMEOUT_SECONDS,
            )
            return True
    except Exception as exc:
        logger.warning("Feishu Bot reply failed: %s", type(exc).__name__)
        return False


async def _send_failure(
    client: FeishuBotClient,
    event: FeishuMessageEvent,
    snapshot: EffectiveFeishuBotConfig,
    text: str,
    *,
    binding: FeishuBinding | None = None,
) -> None:
    try:
        await _reply_if_current(client, event, snapshot, text, binding=binding)
    except Exception:
        logger.exception("Feishu Bot could not send a failure response")


async def _handle_message_event(
    event: FeishuMessageEvent,
    snapshot: EffectiveFeishuBotConfig,
) -> None:
    client = FeishuBotClient(snapshot.config)
    status_value = "failed"
    binding: FeishuBinding | None = None
    try:
        if not _config_store.is_current(snapshot):
            return
        code = _binding_code(event.text)
        if code is not None:
            try:
                async with _config_gate():
                    if not _config_store.is_current(snapshot):
                        return
                    created_binding = _binding_store.consume_code(
                        code,
                        sender_open_id=event.sender_open_id,
                        app_id=event.app_id,
                        chat_id=event.chat_id,
                        owner_is_authorized=_owner_is_authorized,
                        expected_app_id=snapshot.config.app_id,
                        expected_binding_generation=snapshot.binding_generation,
                    )
            except BindingOwnerUnauthorizedError:
                return
            except BindingCodeError as exc:
                await _reply_if_current(client, event, snapshot, f"绑定失败：{exc}")
                return
            binding = created_binding
            try:
                _validate_bind_target(created_binding.tab_id, created_binding.workspace_id)
            except HTTPException:
                try:
                    await _reply_if_current(
                        client,
                        event,
                        snapshot,
                        "绑定失败：Claude Hub 目标当前不可用。",
                        binding=created_binding,
                    )
                finally:
                    try:
                        async with _config_gate():
                            if _config_store.is_current(snapshot) and _binding_record_is_current(
                                created_binding
                            ):
                                _binding_store.delete_owner_binding(created_binding.owner_open_id)
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Feishu Bot binding cleanup failed: %s",
                            type(cleanup_exc).__name__,
                        )
                return
            if not _binding_is_current(created_binding, snapshot):
                return
            if await _reply_if_current(
                client,
                event,
                snapshot,
                f"已连接 Claude Hub Chat：{created_binding.tab_id}",
                binding=created_binding,
            ):
                status_value = "completed"
            return

        binding = _binding_store.get_sender_binding(
            event.sender_open_id, event.app_id, event.chat_id
        )
        if binding is None:
            await _reply_if_current(
                client,
                event,
                snapshot,
                "当前单聊尚未连接 Claude Hub，请先在 Hub 网页生成绑定码。",
            )
            return
        if not _config_store.is_current(snapshot):
            return
        if not _binding_is_current(binding, snapshot):
            async with _config_gate():
                if not _config_store.is_current(snapshot):
                    return
                if _binding_record_is_current(binding):
                    _binding_store.delete_owner_binding(binding.owner_open_id)
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
        if await _reply_if_current(client, event, snapshot, assistant_text, binding=binding):
            status_value = "completed"
    except HTTPException as exc:
        logger.warning("Feishu Bot target rejected: status=%s", exc.status_code)
        reason = _chat_error_reason(exc)
        if reason == "chat_busy":
            message = "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"
        elif exc.status_code == 409 and reason != "binding_target_missing":
            message = (
                "Claude Hub Chat 当前不可用，本条消息尚未执行。" "请在网页检查 Chat 状态后重试。"
            )
        else:
            message = "Claude Hub 目标当前不可用，请在网页重新绑定。"
        await _send_failure(client, event, snapshot, message, binding=binding)
    except Exception:
        logger.exception("Feishu Bot message dispatch failed")
        await _send_failure(
            client,
            event,
            snapshot,
            "Claude Hub 处理消息失败，请稍后重试。",
            binding=binding,
        )
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

    raw_body = await _read_bounded_event_body(request)
    snapshot = _bot_config()
    try:
        event_kind, value = parse_feishu_callback(raw_body, request.headers, snapshot.config)
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
    background_tasks.add_task(_handle_message_event, value, snapshot)
    return {"ok": True}


__all__ = ["require_real_feishu_user", "router"]
