"""Feishu Bot pool administration, pairing, and long-connection routing."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from claude_hub.api.agent_stream import (
    CHAT_ERROR_REASON_HEADER,
    AgentStreamAttachment,
    ExternalDispatchRetired,
    ExternalInteractionRequired,
    dispatch_tab_chat_and_wait,
)
from claude_hub.api.routes.feishu_bot import pool as pool_routes
from claude_hub.api.routes.feishu_bot.message_dispatch import FeishuMessageDispatcher
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchItem as _TabDispatchItem
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchQueue as _TabDispatchQueue
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchQueueFull as _TabDispatchQueueFull
from claude_hub.auth.dependencies import get_current_user
from claude_hub.config import settings
from claude_hub.models import ExecutionTarget, SessionKind, User
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.agent_stream.attachments import preview_within_cache_bounds
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V2,
    format_feishu_provider_text_v2,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotProfile,
    FeishuBotRemoteError,
    FeishuMessageDedupStore,
    FeishuMessageEvent,
    feishu_message_time_is_valid,
)
from claude_hub.services.feishu_bot_pool import (
    MAX_BOTS,
    OWNER_KIND_LOCAL,
    OWNER_KIND_OAUTH,
    BotBinding,
    BotEntry,
    EffectiveBot,
    FeishuBindingCodeError,
    FeishuBotAppIdConflict,
    FeishuBotNotFound,
    FeishuBotOccupied,
    FeishuBotPoolError,
    FeishuBotPoolStateError,
    FeishuBotPoolStore,
    FeishuBotRateLimited,
    FeishuBotReadOnly,
    FeishuBotRevisionConflict,
    FeishuBotRevoked,
    FeishuBotUnavailable,
    FeishuChatOccupied,
    FeishuPairingMismatch,
    FeishuPairingNotFound,
    FeishuPairingNotOwned,
    FeishuPoolFull,
    OwnerIdentity,
    PairingClaim,
    PoolSnapshot,
    dedup_key,
    turn_id_for,
)
from claude_hub.services.feishu_bot_websocket import (
    FeishuBotWebSocketSupervisor,
    message_event_from_sdk,
)
from claude_hub.services.feishu_message_format import build_feishu_message_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feishu/bot", tags=["feishu-bot"])

_BIND_CODE_RE = re.compile(r"^(?:bind\s+)?(CH-[A-Z0-9]{10})$", re.IGNORECASE)
_pool = FeishuBotPoolStore()
_dedup = FeishuMessageDedupStore()
_now: Callable[[], float] = time.time

_BODY_BYTES = 16 * 1024
_GATE_TIMEOUT_SECONDS = 20.0
_OUTBOUND_POST_TIMEOUT_SECONDS = 20.0
_REACTION_POST_TIMEOUT_SECONDS = 2.0
_BATCH_RETIREMENT_WAIT_SECONDS = 12.0
_MAX_GATES = 256
_MAX_TAB_DISPATCH_QUEUES = 256
_MAX_TAB_QUEUE_WAITERS = 8
_MAX_TAB_DISPATCH_CONTEXTS = 64
_MESSAGE_COALESCE_QUIET_SECONDS = 1.0
_MESSAGE_COALESCE_MAX_WAIT_SECONDS = 5.0
_MAX_BATCH_IMAGE_ATTACHMENTS = 10
_MAX_BATCH_IMAGE_BYTES = 40 * 1024 * 1024


@dataclass
class _BotGate:
    lock: asyncio.Lock
    users: int = 0


_gate_registry_lock = threading.Lock()
_bot_gates: dict[str, _BotGate] = {}
# Creation has no bot_id yet, so it uses one dedicated lock instead of mixing
# app_ids into the per-Bot registry key space.
_create_gate = asyncio.Lock()


_tab_dispatch_queues: dict[str, _TabDispatchQueue] = {}


def _promote_tab_dispatch(tab_id: str, queue: _TabDispatchQueue) -> None:
    if queue.processor is not None or queue.active:
        return
    queue.pending[:] = [item for item in queue.pending if not item.cancelled]
    if queue.pending:
        queue.processor = queue.pending[0]
        if not queue.processor.start.done():
            queue.processor.start.set_result(None)
        queue.changed.set()
    elif not queue.active and _tab_dispatch_queues.get(tab_id) is queue:
        del _tab_dispatch_queues[tab_id]


def _reserve_tab_dispatch(
    tab_id: str,
    *,
    bot_id: str,
    event: FeishuMessageEvent,
    effective: EffectiveBot,
    binding: BotBinding,
    client: FeishuBotClient,
) -> _TabDispatchItem:
    queue = _tab_dispatch_queues.get(tab_id)
    if queue is None:
        if len(_tab_dispatch_queues) >= _MAX_TAB_DISPATCH_QUEUES:
            raise _TabDispatchQueueFull("Feishu tab dispatch registry is at capacity")
        queue = _TabDispatchQueue(
            pending=[],
            active=[],
            processor=None,
            changed=asyncio.Event(),
        )
        _tab_dispatch_queues[tab_id] = queue
    # One active/coalescing batch plus at most eight later inbound messages.
    waiting_limit = _MAX_TAB_QUEUE_WAITERS if queue.active else _MAX_TAB_QUEUE_WAITERS + 1
    if len(queue.pending) >= waiting_limit:
        raise _TabDispatchQueueFull("Feishu tab dispatch queue is at capacity")
    item = _TabDispatchItem(
        bot_id=bot_id,
        event=event,
        effective=effective,
        binding=binding,
        client=client,
        arrived_at=asyncio.get_running_loop().time(),
        start=asyncio.get_running_loop().create_future(),
        outcome=asyncio.get_running_loop().create_future(),
    )
    queue.pending.append(item)
    queue.changed.set()
    _promote_tab_dispatch(tab_id, queue)
    return item


def _cancel_tab_dispatch(tab_id: str, item: _TabDispatchItem) -> None:
    queue = _tab_dispatch_queues.get(tab_id)
    if queue is None or item.cancelled:
        return
    item.cancelled = True
    item.reaction_finished.set()
    if not item.dispatch_started:
        try:
            queue.pending.remove(item)
        except ValueError:
            try:
                queue.active.remove(item)
            except ValueError:
                pass
    if queue.processor is item:
        queue.processor = None
    queue.changed.set()
    _promote_tab_dispatch(tab_id, queue)


def _enter_gate_registry(bot_id: str) -> _BotGate:
    """Reference this Bot's gate before any await.

    The count covers holders and waiters alike. Evicting on ``lock.locked()``
    is unsafe: after ``release()`` wakes a waiter there is a window where
    nobody holds the lock but a waiter is pending, and replacing the entry
    there would hand two coroutines two different locks for the same Bot.
    """

    with _gate_registry_lock:
        entry = _bot_gates.get(bot_id)
        if entry is None:
            if len(_bot_gates) >= _MAX_GATES:
                # Refuse a new entry rather than evict one that is still
                # referenced; capacity is never a reason to break serialization.
                raise HTTPException(status_code=503, detail="bot_operation_busy")
            entry = _BotGate(lock=asyncio.Lock())
            _bot_gates[bot_id] = entry
        entry.users += 1
        return entry


def _leave_gate_registry(bot_id: str, entry: _BotGate) -> None:
    with _gate_registry_lock:
        entry.users -= 1
        if entry.users <= 0 and _bot_gates.get(bot_id) is entry:
            del _bot_gates[bot_id]


@asynccontextmanager
async def _gate(bot_id: str) -> AsyncIterator[None]:
    entry = _enter_gate_registry(bot_id)
    try:
        try:
            await asyncio.wait_for(entry.lock.acquire(), timeout=_GATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=503, detail="bot_operation_busy") from exc
        try:
            yield
        finally:
            entry.lock.release()
    finally:
        _leave_gate_registry(bot_id, entry)


# ------------------------------------------------------------------ identity


def require_hub_user(current_user: User = Depends(get_current_user)) -> User:
    """Reuse Hub's own access control, including its local-network identity.

    ``get_current_user`` returns the shared ``local`` identity whenever
    ``is_local_network_request`` holds, which is unconditional while auth is
    unconfigured. Demanding a real OAuth session here would add a gate that the
    rest of Hub does not have.
    """

    return current_user


def _owner_identity(user: User) -> OwnerIdentity:
    kind = OWNER_KIND_LOCAL if user.open_id == "local" else OWNER_KIND_OAUTH
    return OwnerIdentity(open_id=user.open_id, email=user.email or "", kind=kind)


def _owner_is_authorized(owner: OwnerIdentity) -> bool:
    """Re-evaluate a stored owner against the current configuration.

    The whitelist rule is kept in step with ``get_current_user_from_cookie``:
    an open_id list takes priority, only an empty one falls through to emails,
    and with neither configured every identity is allowed. A parity test pins
    this against Hub's own session check.

    A ``local`` owner is instance-wide shared trust, not a person. There is no
    honest per-person revocation for it, so none is invented here: such a
    pairing is removed by unbinding, disabling, or deleting the Bot.
    """

    if owner.kind == OWNER_KIND_LOCAL:
        return True
    if not settings.auth_enabled:
        return True
    allowed_open_ids = settings.allowed_open_ids_list
    if allowed_open_ids:
        return owner.open_id in allowed_open_ids
    allowed_emails = [value.lower() for value in settings.allowed_emails_list]
    if allowed_emails:
        return bool(owner.email) and owner.email.lower() in allowed_emails
    return True


# --------------------------------------------------------------------- DTOs


FeishuPairingView = pool_routes.FeishuPairingView
FeishuBotSummary = pool_routes.FeishuBotSummary
FeishuBotPoolResponse = pool_routes.FeishuBotPoolResponse
FeishuPairStartResponse = pool_routes.FeishuPairStartResponse
_utc = pool_routes.utc
_pool_http_error = pool_routes.pool_http_error
_pairing_view = pool_routes.pairing_view
_is_mine = pool_routes.is_mine
_validated_profile = pool_routes.validated_profile
_read_json = pool_routes.read_json
_exact_keys = pool_routes.exact_keys
_secret = pool_routes.parse_secret
_name = pool_routes.parse_name
_revision = pool_routes.parse_revision
_in_pool = pool_routes.in_pool
_credentials_http_error = pool_routes.credentials_http_error
_gone = pool_routes.gone


class _FeishuPoolBindings:
    def pool(self) -> FeishuBotPoolStore:
        return _pool

    def dedup(self) -> FeishuMessageDedupStore:
        return _dedup

    def now(self) -> float:
        return _now()

    def gate(self, bot_id: str) -> Any:
        return _gate(bot_id)

    def owner_is_authorized(self, owner: OwnerIdentity) -> bool:
        return _owner_is_authorized(owner)

    def validate_saved_target(self, binding: BotBinding) -> None:
        _validate_saved_target(binding)

    def websocket_supervisor(self) -> FeishuBotWebSocketSupervisor:
        return _ws_supervisor

    def owner_identity(self, user: User) -> OwnerIdentity:
        return _owner_identity(user)

    def require_hub_user(self, user: User) -> User:
        return require_hub_user(user)

    def bot_client_class(self) -> type[FeishuBotClient]:
        return FeishuBotClient

    def create_gate(self) -> Any:
        return _create_gate

    def validate_bind_target(self, tab_id: str, workspace_id: Any) -> Any:
        return _validate_bind_target(tab_id, workspace_id)

    def ttyd_manager(self) -> Any:
        return ttyd_manager

    def workspace_manager(self) -> Any:
        return workspace_manager


def _snapshot() -> PoolSnapshot:
    return pool_routes.get_pool_snapshot(_FeishuPoolBindings())


def _summary(entry: BotEntry, actor: OwnerIdentity) -> FeishuBotSummary:
    return pool_routes.summary(entry, actor, _FeishuPoolBindings())


def _pool_response(user: User, *, focus_bot_id: str | None = None) -> FeishuBotPoolResponse:
    return pool_routes.pool_response(user, _FeishuPoolBindings(), focus_bot_id=focus_bot_id)


def _validate_bind_target(tab_id: str, requested_workspace_id: str | None) -> str | None:
    return pool_routes.validate_bind_target(tab_id, requested_workspace_id, _FeishuPoolBindings())


def _validate_saved_target(pairing: BotBinding | PairingClaim) -> None:
    pool_routes.validate_saved_target(pairing, _FeishuPoolBindings())


async def _reconcile_ws() -> None:
    await pool_routes.reconcile_ws(_FeishuPoolBindings())


_pool_endpoints = pool_routes.register_pool_routes(router, _FeishuPoolBindings(), require_hub_user)
list_feishu_bots = _pool_endpoints.list_feishu_bots
create_feishu_bot = _pool_endpoints.create_feishu_bot
rotate_feishu_bot_secrets = _pool_endpoints.rotate_feishu_bot_secrets
update_feishu_bot = _pool_endpoints.update_feishu_bot
delete_feishu_bot = _pool_endpoints.delete_feishu_bot
start_feishu_pairing = _pool_endpoints.start_feishu_pairing
activate_feishu_pairing = _pool_endpoints.activate_feishu_pairing
release_feishu_pairing = _pool_endpoints.release_feishu_pairing
removed_single_bot_config_status = _pool_endpoints.removed_single_bot_config_status
removed_single_bot_get_config = _pool_endpoints.removed_single_bot_get_config
removed_single_bot_put_config = _pool_endpoints.removed_single_bot_put_config
removed_single_bot_delete_config = _pool_endpoints.removed_single_bot_delete_config
removed_single_bot_bind_start = _pool_endpoints.removed_single_bot_bind_start
removed_single_bot_get_binding = _pool_endpoints.removed_single_bot_get_binding
removed_single_bot_delete_binding = _pool_endpoints.removed_single_bot_delete_binding


class _FeishuMessageBindings(_FeishuPoolBindings):
    def promote_tab_dispatch(self, tab_id: str, queue: _TabDispatchQueue) -> None:
        _promote_tab_dispatch(tab_id, queue)

    async def dispatch_tab_chat_and_wait(self, *args: Any, **kwargs: Any) -> str:
        return await dispatch_tab_chat_and_wait(*args, **kwargs)

    async def event_attachments(
        self, *args: Any, **kwargs: Any
    ) -> tuple[list[AgentStreamAttachment], int, list[int]]:
        return await _event_attachments(*args, **kwargs)

    async def refresh_missing_profile(self, *args: Any, **kwargs: Any) -> EffectiveBot:
        return await _refresh_missing_profile(*args, **kwargs)

    async def reply_if_current(self, *args: Any, **kwargs: Any) -> bool:
        return await _reply_if_current(*args, **kwargs)

    async def add_processing_reaction(self, *args: Any, **kwargs: Any) -> str | None:
        return await _add_processing_reaction(*args, **kwargs)

    async def delete_processing_reaction(self, *args: Any, **kwargs: Any) -> None:
        await _delete_processing_reaction(*args, **kwargs)

    def outbound_post_timeout_seconds(self) -> float:
        return _OUTBOUND_POST_TIMEOUT_SECONDS

    def reaction_post_timeout_seconds(self) -> float:
        return _REACTION_POST_TIMEOUT_SECONDS

    def max_tab_dispatch_contexts(self) -> int:
        return _MAX_TAB_DISPATCH_CONTEXTS

    def message_coalesce_quiet_seconds(self) -> float:
        return _MESSAGE_COALESCE_QUIET_SECONDS

    def message_coalesce_max_wait_seconds(self) -> float:
        return _MESSAGE_COALESCE_MAX_WAIT_SECONDS

    def max_batch_image_attachments(self) -> int:
        return _MAX_BATCH_IMAGE_ATTACHMENTS

    def max_batch_image_bytes(self) -> int:
        return _MAX_BATCH_IMAGE_BYTES


_message_dispatcher = FeishuMessageDispatcher(_FeishuMessageBindings())
_binding_code = _message_dispatcher._binding_code
_turn_metadata = _message_dispatcher._turn_metadata
_image_mime = _message_dispatcher._image_mime
_event_attachments = _message_dispatcher._event_attachments
_refresh_missing_profile = _message_dispatcher._refresh_missing_profile
_binding_still_routes = _message_dispatcher._binding_still_routes
_message_matches_binding_time = _message_dispatcher._message_matches_binding_time
_admit_binding_dispatch = _message_dispatcher._admit_binding_dispatch
_admit_batch_dispatch = _message_dispatcher._admit_batch_dispatch
_claim_still_live = _message_dispatcher._claim_still_live
_reply_if_current = _message_dispatcher._reply_if_current
_add_processing_reaction = _message_dispatcher._add_processing_reaction
_delete_processing_reaction = _message_dispatcher._delete_processing_reaction
_wait_for_reaction_dispatch = _message_dispatcher._wait_for_reaction_dispatch
_maintain_processing_reaction = _message_dispatcher._maintain_processing_reaction
_finish_processing_reaction = _message_dispatcher._finish_processing_reaction
_batch_visible_text = _message_dispatcher._batch_visible_text
_batch_matches = _message_dispatcher._batch_matches
_collect_tab_dispatch_batch = _message_dispatcher._collect_tab_dispatch_batch
_claim_tab_dispatch_batch = _message_dispatcher._claim_tab_dispatch_batch
_finish_tab_dispatch_batch = _message_dispatcher._finish_tab_dispatch_batch
_dispatch_tab_batch = _message_dispatcher._dispatch_tab_batch


_PAIRING_FAILURE_TEXT = {
    "unknown": "配对失败：配对码无效。",
    "expired": "配对失败：配对码已过期，请在 Claude Hub 重新生成。",
    "superseded": "配对失败：该 Bot 的配置已变更，请在 Claude Hub 重新生成配对码。",
}


async def _handle_pairing_code(
    bot_id: str,
    event: FeishuMessageEvent,
    effective: EffectiveBot,
    client: FeishuBotClient,
    code: str,
) -> bool:
    try:
        async with _gate(bot_id):
            if not _pool.is_current(effective):
                return False
            claim, word = _pool.claim_code(
                bot_id,
                code=code,
                sender_open_id=event.sender_open_id,
                chat_id=event.chat_id,
            )
            # Consuming the code advances this Bot's revision, so the pre-claim
            # snapshot is now deliberately stale. Re-read it instead of
            # weakening the staleness check that protects every other reply.
            refreshed = _pool.effective(bot_id)
    except FeishuBindingCodeError as exc:
        reason = str(exc) if str(exc) in _PAIRING_FAILURE_TEXT else "unknown"
        await _reply_if_current(client, event, effective, _PAIRING_FAILURE_TEXT[reason])
        return False
    except FeishuBotPoolError:
        await _reply_if_current(client, event, effective, "配对失败：请稍后重试。")
        return False
    return await _reply_if_current(
        client,
        event,
        refreshed,
        f"已收到配对请求。确认码：{word}\n"
        "请回到 Claude Hub，在该 Bot 的配对卡片中输入这个确认码完成激活，10 分钟内有效。"
        "激活之前，本会话的消息不会转发给 Claude Hub。",
        still_valid=lambda: _claim_still_live(bot_id, claim),
    )


async def _handle_message_event(
    bot_id: str, event: FeishuMessageEvent, effective: EffectiveBot
) -> None:
    client = FeishuBotClient(effective.config)
    status_value = "failed"
    binding: BotBinding | None = None
    item: _TabDispatchItem | None = None
    try:
        effective = await _refresh_missing_profile(effective, client)
        if not _pool.is_current(effective):
            return
        code = _binding_code(event.text) if event.chat_type == "p2p" else None
        if code is not None:
            if await _handle_pairing_code(bot_id, event, effective, client, code):
                status_value = "completed"
            return
        binding = (
            _pool.routed_binding(bot_id, sender_open_id=event.sender_open_id, chat_id=event.chat_id)
            if event.chat_type == "p2p"
            else _pool.routed_binding_for_sender(bot_id, sender_open_id=event.sender_open_id)
        )
        if binding is None:
            if event.chat_type == "group":
                return
            await _reply_if_current(
                client,
                event,
                effective,
                "本会话尚未连接 Claude Hub。请在 Hub 网页选择一个 Bot 和 Chat 生成配对码。",
            )
            return
        if not _message_matches_binding_time(event, binding):
            return
        if not _owner_is_authorized(binding.owner):
            async with _gate(bot_id):
                if not _owner_is_authorized(binding.owner):
                    _pool.drop_binding(bot_id, binding.pairing_id)
            return
        _validate_saved_target(binding)
        try:
            item = _reserve_tab_dispatch(
                binding.tab_id,
                bot_id=bot_id,
                event=event,
                effective=effective,
                binding=binding,
                client=client,
            )
        except _TabDispatchQueueFull:
            reaction_id = await _add_processing_reaction(client, event, "OneSecond")
            await _reply_if_current(
                client,
                event,
                effective,
                "Claude Hub 消息队列已满，本条消息未执行，请稍后重试。",
                still_valid=lambda: binding is not None
                and _binding_still_routes(effective, binding),
            )
            await _delete_processing_reaction(client, event, reaction_id)
            return
        item.reaction_task = asyncio.create_task(_maintain_processing_reaction(item))
        await item.start
        queue = _tab_dispatch_queues.get(binding.tab_id)
        if queue is None:
            # The batch leader may complete and retire the queue before a
            # coalesced follower resumes from ``item.start``. Its per-item
            # outcome remains the authoritative result channel.
            status_value = await asyncio.shield(item.outcome)
        elif queue.processor is item:
            status_value = await _dispatch_tab_batch(binding.tab_id, queue, item)
        else:
            status_value = await asyncio.shield(item.outcome)
        if status_value == "retired":
            raise ExternalDispatchRetired(turn_id_for(bot_id, event.message_id))
    except asyncio.CancelledError:
        if item is None or not item.dispatch_started:
            # A queued message cancelled during shutdown never entered the Chat
            # bridge, so its durable at-most-once claim must remain retryable.
            # The outer WebSocket intake releases only this explicit retirement
            # signal after the reservation/reaction cleanup below has completed.
            raise ExternalDispatchRetired(turn_id_for(bot_id, event.message_id)) from None
        if not item.outcome.done():
            # A coalesced follower does not own the shared native turn. During
            # shutdown its route task may be cancelled before the batch leader
            # has proved whether that turn stopped safely. Shielding ``outcome``
            # keeps the leader's result channel alive; wait slightly longer
            # than the bridge's bounded retirement so every constituent claim
            # follows the same retry decision.
            try:
                status_value = await asyncio.wait_for(
                    asyncio.shield(item.outcome),
                    timeout=_BATCH_RETIREMENT_WAIT_SECONDS,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if item.outcome.done() and not item.outcome.cancelled():
            status_value = item.outcome.result()
        if status_value == "retired":
            raise ExternalDispatchRetired(turn_id_for(bot_id, event.message_id)) from None
        raise
    except Exception:
        logger.exception("Feishu Bot message dispatch failed")
        await _reply_if_current(
            client,
            event,
            effective,
            "Claude Hub 处理消息失败，请稍后重试。",
            still_valid=(
                (lambda: binding is not None and _binding_still_routes(effective, binding))
                if binding is not None
                else None
            ),
        )
    finally:
        if binding is not None and item is not None:
            _cancel_tab_dispatch(binding.tab_id, item)
        try:
            if item is not None:
                await _finish_processing_reaction(item)
        finally:
            _dedup.finish(dedup_key(bot_id, event.message_id), status_value)


async def _handle_sdk_event(bot_id: str, data: Any) -> None:
    try:
        effective = _pool.effective(bot_id)
        message = getattr(getattr(data, "event", None), "message", None)
        if getattr(message, "chat_type", None) == "group":
            effective = await _refresh_missing_profile(
                effective, FeishuBotClient(effective.config), require_open_id=True
            )
        event = message_event_from_sdk(
            data,
            expected_app_id=effective.app_id,
            expected_bot_open_id=effective.feishu_open_id,
        )
    except FeishuBotPoolError:
        return
    except ValueError as exc:
        logger.info("Ignored unsupported Feishu WebSocket event for Bot %s: %s", bot_id, exc)
        return
    if not _dedup.claim(dedup_key(bot_id, event.message_id)):
        return
    key = dedup_key(bot_id, event.message_id)
    try:
        await _handle_message_event(bot_id, event, effective)
    except ExternalDispatchRetired:
        # Final shutdown cancels only after its bounded drain. The Chat bridge
        # proved the matching turn was stopped or never started, so releasing
        # the claim lets a same-ID replay recover without duplicating a
        # completed or superseded turn.
        _dedup.release(key)
        raise


_ws_supervisor = FeishuBotWebSocketSupervisor(_pool, _handle_sdk_event)


async def start_feishu_websockets() -> None:
    await _ws_supervisor.start()


async def stop_feishu_websockets() -> None:
    await _ws_supervisor.stop()


__all__ = [
    "require_hub_user",
    "router",
    "start_feishu_websockets",
    "stop_feishu_websockets",
]
