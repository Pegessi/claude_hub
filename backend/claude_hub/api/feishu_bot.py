"""Feishu Bot pool administration, pairing, and long-connection routing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from claude_hub.api.agent_stream import (
    CHAT_ERROR_REASON_HEADER,
    ExternalDispatchRetired,
    dispatch_tab_chat_and_wait,
)
from claude_hub.auth.dependencies import get_current_user
from claude_hub.config import settings
from claude_hub.models import ExecutionTarget, SessionKind, User
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V2,
    format_feishu_provider_text_v2,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
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
_MAX_GATES = 256
_MAX_TAB_DISPATCH_QUEUES = 256
_MAX_TAB_QUEUE_WAITERS = 8


@dataclass
class _BotGate:
    lock: asyncio.Lock
    users: int = 0


_gate_registry_lock = threading.Lock()
_bot_gates: dict[str, _BotGate] = {}
# Creation has no bot_id yet, so it uses one dedicated lock instead of mixing
# app_ids into the per-Bot registry key space.
_create_gate = asyncio.Lock()


class _TabDispatchQueueFull(RuntimeError):
    """The bounded Feishu-only queue cannot retain another message."""


@dataclass
class _TabDispatchQueue:
    waiters: list[asyncio.Future[None]]


_tab_dispatch_queues: dict[str, _TabDispatchQueue] = {}


def _release_tab_dispatch(tab_id: str, queue: _TabDispatchQueue) -> None:
    """Transfer ownership FIFO, or evict an idle registry entry."""

    while queue.waiters:
        next_waiter = queue.waiters.pop(0)
        if next_waiter.done():
            continue
        next_waiter.set_result(None)
        return
    if _tab_dispatch_queues.get(tab_id) is queue:
        del _tab_dispatch_queues[tab_id]


class _TabDispatchReservation:
    """One pre-await position in a tab's Feishu FIFO.

    Reservation happens before the reaction HTTP call, so transport latency
    cannot reorder messages that the WebSocket route tasks delivered in order.
    ``cancel`` also covers cancellation before ``__aenter__`` is reached.
    """

    def __init__(
        self,
        tab_id: str,
        queue: _TabDispatchQueue,
        waiter: asyncio.Future[None] | None,
    ) -> None:
        self._tab_id = tab_id
        self._queue = queue
        self._waiter = waiter
        self._closed = False

    async def __aenter__(self) -> None:
        if self._closed:
            raise RuntimeError("Feishu tab dispatch reservation is closed")
        if self._waiter is not None:
            try:
                await self._waiter
            except BaseException:
                self.cancel()
                raise
        return None

    async def __aexit__(self, *_exc: Any) -> None:
        self.cancel()

    def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        waiter = self._waiter
        if waiter is None or (waiter.done() and not waiter.cancelled()):
            # Immediate reservations and successfully awakened waiters own the
            # active slot, so cancellation must hand it to the next waiter.
            _release_tab_dispatch(self._tab_id, self._queue)
            return
        try:
            self._queue.waiters.remove(waiter)
        except ValueError:
            pass


def _reserve_tab_dispatch(tab_id: str) -> _TabDispatchReservation:
    queue = _tab_dispatch_queues.get(tab_id)
    if queue is None:
        if len(_tab_dispatch_queues) >= _MAX_TAB_DISPATCH_QUEUES:
            raise _TabDispatchQueueFull("Feishu tab dispatch registry is at capacity")
        queue = _TabDispatchQueue(waiters=[])
        _tab_dispatch_queues[tab_id] = queue
        return _TabDispatchReservation(tab_id, queue, None)
    if len(queue.waiters) >= _MAX_TAB_QUEUE_WAITERS:
        raise _TabDispatchQueueFull("Feishu tab dispatch queue is at capacity")
    waiter = asyncio.get_running_loop().create_future()
    queue.waiters.append(waiter)
    return _TabDispatchReservation(tab_id, queue, waiter)


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


class FeishuPairingView(BaseModel):
    pairing_id: str
    state: str
    bot_id: str
    app_id: str
    tab_id: str
    workspace_id: str | None
    chat_id: str | None
    owner_kind: str
    is_mine: bool
    created_at: datetime
    expires_at: datetime | None


class FeishuBotSummary(BaseModel):
    bot_id: str
    name: str
    app_id: str
    source: str
    enabled: bool
    credentials_editable: bool
    deletable: bool
    revision: int
    generation: int
    configured: bool
    app_secret_configured: bool
    connection_status: str
    updated_at: datetime | None
    binding: FeishuPairingView | None
    my_claims: list[FeishuPairingView]


class FeishuBotPoolResponse(BaseModel):
    pool_revision: int
    bots: list[FeishuBotSummary]
    pool_editable: bool
    deprecated_env: list[str]
    focus_bot_id: str | None


class FeishuPairStartResponse(BaseModel):
    bot_id: str
    code: str
    expires_at: datetime
    revision: int


# ----------------------------------------------------------------- plumbing


def _utc(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=timezone.utc)


def _pool_http_error(exc: FeishuBotPoolError) -> HTTPException:
    mapping: list[tuple[type[FeishuBotPoolError], int, str]] = [
        (FeishuBotNotFound, 404, "bot_not_found"),
        (FeishuBotRevoked, 409, "bot_disabled"),
        (FeishuBotUnavailable, 503, "bot_pool_unavailable"),
        (FeishuBotReadOnly, 409, "bot_credentials_read_only"),
        (FeishuBotRevisionConflict, 409, "bot_revision_conflict"),
        (FeishuBotAppIdConflict, 409, "app_id_already_in_pool"),
        (FeishuBotOccupied, 409, "bot_already_bound"),
        (FeishuChatOccupied, 409, "chat_already_bound"),
        (FeishuPairingNotFound, 409, "pairing_not_claimed"),
        (FeishuPairingNotOwned, 403, "pairing_not_owned"),
        (FeishuPairingMismatch, 409, "pairing_confirmation_mismatch"),
        (FeishuBindingCodeError, 409, "pairing_expired"),
        (FeishuBotRateLimited, 429, "pair_code_rate_limited"),
        (FeishuPoolFull, 409, "pool_capacity_reached"),
        (FeishuBotPoolStateError, 503, "bot_pool_unavailable"),
    ]
    for error_type, code, detail in mapping:
        if isinstance(exc, error_type):
            return HTTPException(status_code=code, detail=detail)
    return HTTPException(status_code=503, detail="bot_pool_unavailable")


def _snapshot() -> PoolSnapshot:
    try:
        return _pool.snapshot()
    except FeishuBotPoolError as exc:
        raise _pool_http_error(exc) from exc


def _pairing_view(
    entry: BotEntry,
    pairing: BotBinding | PairingClaim,
    *,
    state: str,
    is_mine: bool,
    expires_at: float | None = None,
) -> FeishuPairingView:
    created = _utc(pairing.created_at)
    assert created is not None
    return FeishuPairingView(
        pairing_id=pairing.pairing_id,
        state=state,
        bot_id=entry.bot_id,
        app_id=entry.app_id,
        tab_id=pairing.tab_id,
        workspace_id=pairing.workspace_id,
        # Another person's Feishu conversation id is not needed to understand
        # occupancy, so it is only returned to its own owner.
        chat_id=pairing.chat_id if is_mine else None,
        owner_kind=pairing.owner.kind,
        is_mine=is_mine,
        created_at=created,
        expires_at=_utc(expires_at),
    )


def _is_mine(owner: OwnerIdentity, actor: OwnerIdentity) -> bool:
    return owner.kind == actor.kind and owner.open_id == actor.open_id


def _summary(entry: BotEntry, actor: OwnerIdentity) -> FeishuBotSummary:
    config = entry.config
    binding = None
    if entry.binding is not None:
        binding = _pairing_view(
            entry,
            entry.binding,
            state="active",
            is_mine=_is_mine(entry.binding.owner, actor),
        )
    claims = [
        _pairing_view(entry, claim, state="claimed", is_mine=True, expires_at=claim.expires_at)
        for claim in entry.claims
        if _is_mine(claim.owner, actor)
    ]
    return FeishuBotSummary(
        bot_id=entry.bot_id,
        name=entry.name,
        app_id=entry.app_id,
        source=entry.source,
        enabled=entry.enabled,
        credentials_editable=entry.credentials_editable,
        deletable=entry.deletable,
        revision=entry.revision,
        generation=entry.generation,
        configured=entry.configured,
        app_secret_configured=bool(config and config.app_secret),
        connection_status=_ws_supervisor.status(entry.bot_id),
        updated_at=_utc(entry.updated_at),
        binding=binding,
        my_claims=claims,
    )


def _pool_response(user: User, *, focus_bot_id: str | None = None) -> FeishuBotPoolResponse:
    actor = _owner_identity(user)
    snapshot = _snapshot()
    return FeishuBotPoolResponse(
        pool_revision=snapshot.pool_revision,
        bots=[_summary(entry, actor) for entry in snapshot.bots],
        pool_editable=True,
        deprecated_env=list(snapshot.deprecated_env),
        focus_bot_id=focus_bot_id,
    )


async def _read_json(request: Request) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _BODY_BYTES:
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_bot_request") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise HTTPException(status_code=400, detail="invalid_bot_request")


def _secret(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw.strip()


def _name(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 64:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw.strip()


def _revision(value: dict[str, Any]) -> int:
    raw = value.get("expected_revision")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw


def _validate_bind_target(tab_id: str, requested_workspace_id: str | None) -> str | None:
    tab = ttyd_manager.get_tab(tab_id)
    process = ttyd_manager.processes.get(tab_id)
    if tab is None or process is None or getattr(process, "archived", False):
        raise HTTPException(status_code=404, detail="chat_tab_not_found")
    if tab.session_kind != SessionKind.CHAT or tab.target != ExecutionTarget.LOCAL:
        raise HTTPException(status_code=400, detail="chat_tab_not_eligible")
    if tab.workspace_role is not None:
        raise HTTPException(status_code=400, detail="chat_tab_not_eligible")
    actual = str(tab.workspace_id) if tab.workspace_id is not None else None
    if requested_workspace_id is not None and requested_workspace_id != actual:
        raise HTTPException(status_code=403, detail="chat_tab_workspace_mismatch")
    if actual is not None and actual not in workspace_manager.workspaces:
        raise HTTPException(
            status_code=409,
            detail="chat_tab_workspace_missing",
            headers={CHAT_ERROR_REASON_HEADER: "binding_target_missing"},
        )
    return actual


def _validate_saved_target(pairing: BotBinding | PairingClaim) -> None:
    actual = _validate_bind_target(pairing.tab_id, pairing.workspace_id)
    # None in a saved pairing means an originally unscoped Chat, not permission
    # to follow that tab into an arbitrary later workspace.
    if actual != pairing.workspace_id:
        raise HTTPException(
            status_code=409,
            detail="chat_tab_workspace_changed",
            headers={CHAT_ERROR_REASON_HEADER: "binding_target_missing"},
        )


async def _in_pool(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except FeishuBotPoolError as exc:
        raise _pool_http_error(exc) from exc


async def _reconcile_ws() -> None:
    """Apply pool mutations immediately; the monitor remains the retry path."""

    try:
        await _ws_supervisor.reconcile()
    except Exception:
        logger.exception("Feishu WebSocket reconciliation failed after a pool mutation")


# ------------------------------------------------------------ pool routes


@router.get("/bots", response_model=FeishuBotPoolResponse)
async def list_feishu_bots(user: User = Depends(require_hub_user)) -> FeishuBotPoolResponse:
    return _pool_response(user)


@router.post("/bots", response_model=FeishuBotPoolResponse, status_code=201)
async def create_feishu_bot(
    request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    value = await _read_json(request)
    _exact_keys(value, {"name", "app_id", "app_secret"})
    candidate = FeishuBotConfig(
        app_id=_secret(value, "app_id"),
        app_secret=_secret(value, "app_secret"),
    )
    name = _name(value, "name")
    snapshot = _snapshot()
    if len(snapshot.bots) >= MAX_BOTS:
        raise HTTPException(status_code=409, detail="pool_capacity_reached")
    if any(entry.app_id == candidate.app_id for entry in snapshot.bots):
        raise HTTPException(status_code=409, detail="app_id_already_in_pool")
    # Credential validation reaches Feishu and must never hold a gate.
    try:
        await FeishuBotClient(candidate).validate_credentials()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="feishu_credentials_rejected") from exc
    async with _create_gate:
        bot_id = await _in_pool(lambda: _pool.create_bot(name=name, config=candidate))
    await _reconcile_ws()
    return _pool_response(user, focus_bot_id=bot_id)


@router.put("/bots/{bot_id}/secrets", response_model=FeishuBotPoolResponse)
async def rotate_feishu_bot_secrets(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    value = await _read_json(request)
    _exact_keys(value, {"app_secret", "expected_revision"})
    expected_revision = _revision(value)
    snapshot = _snapshot()
    entry = snapshot.get(bot_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="bot_not_found")
    if not entry.credentials_editable:
        raise HTTPException(status_code=409, detail="bot_credentials_read_only")
    candidate = FeishuBotConfig(
        app_id=entry.app_id,
        app_secret=_secret(value, "app_secret"),
    )
    try:
        await FeishuBotClient(candidate).validate_credentials()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="feishu_credentials_rejected") from exc
    async with _gate(bot_id):
        await _in_pool(
            lambda: _pool.rotate_secrets(
                bot_id,
                app_secret=candidate.app_secret,
                expected_revision=expected_revision,
            )
        )
    await _reconcile_ws()
    return _pool_response(user, focus_bot_id=bot_id)


@router.patch("/bots/{bot_id}", response_model=FeishuBotPoolResponse)
async def update_feishu_bot(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    value = await _read_json(request)
    if (
        not set(value) <= {"name", "enabled", "expected_revision"}
        or "expected_revision" not in value
    ):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    expected_revision = _revision(value)
    name = _name(value, "name") if "name" in value else None
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    if _snapshot().get(bot_id) is None:
        raise HTTPException(status_code=404, detail="bot_not_found")
    async with _gate(bot_id):
        await _in_pool(
            lambda: _pool.update_bot(
                bot_id, expected_revision=expected_revision, name=name, enabled=enabled
            )
        )
    await _reconcile_ws()
    return _pool_response(user, focus_bot_id=bot_id)


@router.delete("/bots/{bot_id}", response_model=FeishuBotPoolResponse)
async def delete_feishu_bot(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    value = await _read_json(request)
    _exact_keys(value, {"expected_revision"})
    expected_revision = _revision(value)
    if _snapshot().get(bot_id) is None:
        raise HTTPException(status_code=404, detail="bot_not_found")
    async with _gate(bot_id):
        await _in_pool(lambda: _pool.delete_bot(bot_id, expected_revision=expected_revision))
    await _reconcile_ws()
    return _pool_response(user, focus_bot_id=None)


# --------------------------------------------------------- pairing routes


@router.post("/bots/{bot_id}/pair/start", response_model=FeishuPairStartResponse, status_code=201)
async def start_feishu_pairing(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuPairStartResponse:
    value = await _read_json(request)
    if not set(value) <= {"tab_id", "workspace_id", "expected_revision"} or not {
        "tab_id",
        "expected_revision",
    } <= set(value):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    expected_revision = _revision(value)
    tab_id = _secret(value, "tab_id")
    requested_workspace = value.get("workspace_id")
    if requested_workspace is not None and not isinstance(requested_workspace, str):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    owner = _owner_identity(user)
    async with _gate(bot_id):
        entry = _snapshot().get(bot_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="bot_not_found")
        if not entry.configured:
            raise HTTPException(status_code=503, detail="bot_pool_unavailable")
        if not _owner_is_authorized(owner):
            raise HTTPException(status_code=403, detail="hub_identity_revoked")
        workspace_id = _validate_bind_target(tab_id, requested_workspace)
        code, expires_at, revision = await _in_pool(
            lambda: _pool.issue_code(
                bot_id,
                owner=owner,
                tab_id=tab_id,
                workspace_id=workspace_id,
                expected_revision=expected_revision,
            )
        )
    issued = _utc(expires_at)
    assert issued is not None
    return FeishuPairStartResponse(bot_id=bot_id, code=code, expires_at=issued, revision=revision)


@router.post("/bots/{bot_id}/pair/activate", response_model=FeishuBotPoolResponse)
async def activate_feishu_pairing(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    value = await _read_json(request)
    _exact_keys(value, {"pairing_id", "confirm_word", "expected_revision"})
    expected_revision = _revision(value)
    pairing_id = _secret(value, "pairing_id")
    confirm_word = _secret(value, "confirm_word")
    actor = _owner_identity(user)
    async with _gate(bot_id):
        if not _owner_is_authorized(actor):
            raise HTTPException(status_code=403, detail="hub_identity_revoked")
        entry = _snapshot().get(bot_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="bot_not_found")
        claim = next((item for item in entry.claims if item.pairing_id == pairing_id), None)
        if claim is not None:
            _validate_saved_target(claim)
        await _in_pool(
            lambda: _pool.activate_claim(
                bot_id,
                pairing_id=pairing_id,
                confirm_word=confirm_word,
                actor=actor,
                expected_revision=expected_revision,
            )
        )
    return _pool_response(user, focus_bot_id=bot_id)


@router.delete("/bots/{bot_id}/pairing", response_model=FeishuBotPoolResponse)
async def release_feishu_pairing(
    bot_id: str, request: Request, user: User = Depends(require_hub_user)
) -> FeishuBotPoolResponse:
    """Unbind explicitly.

    Any authorized Hub user may do this: the pool is instance-shared, and
    restricting it to the original owner would let one departed owner occupy a
    Bot forever. It is not preemption - unbinding and pairing are two separate
    deliberate actions, and occupancy is visible in the pool the whole time.
    """

    value = await _read_json(request)
    _exact_keys(value, {"expected_revision"})
    expected_revision = _revision(value)
    async with _gate(bot_id):
        await _in_pool(lambda: _pool.release_binding(bot_id, expected_revision=expected_revision))
    return _pool_response(user, focus_bot_id=bot_id)


# ----------------------------------------------------- replaced interfaces


def _gone() -> HTTPException:
    return HTTPException(status_code=410, detail="interface_replaced_by_bot_pool")


@router.api_route("/config/status", methods=["GET"])
@router.api_route("/config", methods=["GET", "PUT", "DELETE"])
@router.api_route("/bind/start", methods=["POST"])
@router.api_route("/binding", methods=["GET", "DELETE"])
async def removed_single_bot_interface() -> None:
    raise _gone()


# ------------------------------------------------------------ event intake


def _binding_code(text: str) -> str | None:
    match = _BIND_CODE_RE.fullmatch(text.strip())
    return match.group(1).upper() if match is not None else None


def _turn_metadata(bot_id: str, event: FeishuMessageEvent) -> dict[str, Any]:
    """Additive only: ``bot_id`` joins the existing ``feishu`` sub-object so no
    Task schema or frontend reader has to change."""

    return {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V2,
        "feishu": {
            "bot_id": bot_id,
            "app_id": event.app_id,
            "chat_id": event.chat_id,
            "message_id": event.message_id,
            "sender_open_id": event.sender_open_id,
        },
    }


def _binding_still_routes(effective: EffectiveBot, binding: BotBinding) -> bool:
    """Compare the pairing id, not just the fields.

    Re-pairing the same Chat produces a new pairing id, so a reply computed for
    the previous binding can never be delivered against the new one.
    """

    current = _pool.routed_binding(
        effective.bot_id,
        sender_open_id=binding.sender_open_id,
        chat_id=binding.chat_id,
    )
    if current is None or current.pairing_id != binding.pairing_id:
        return False
    return _owner_is_authorized(current.owner)


def _message_matches_binding_time(event: FeishuMessageEvent, binding: BotBinding) -> bool:
    return feishu_message_time_is_valid(
        event.message_created_at_ms, activated_at=binding.created_at, now=_now()
    )


@asynccontextmanager
async def _admit_binding_dispatch(
    effective: EffectiveBot, binding: BotBinding, event: FeishuMessageEvent
) -> AsyncIterator[None]:
    async with _gate(effective.bot_id):
        if not _pool.is_current(effective) or not _binding_still_routes(effective, binding):
            raise HTTPException(status_code=409, detail="bot_binding_revoked")
        if not _message_matches_binding_time(event, binding):
            raise HTTPException(
                status_code=409,
                detail="message_outside_binding_period",
                headers={CHAT_ERROR_REASON_HEADER: "message_outside_binding_period"},
            )
        _validate_saved_target(binding)
        yield


def _claim_still_live(bot_id: str, claim: PairingClaim) -> bool:
    try:
        entry = _pool.snapshot().get(bot_id)
    except FeishuBotPoolError:
        return False
    if entry is None or not entry.enabled:
        return False
    current = next((item for item in entry.claims if item.pairing_id == claim.pairing_id), None)
    if current is None:
        return False
    return _owner_is_authorized(current.owner)


async def _reply_if_current(
    client: FeishuBotClient,
    event: FeishuMessageEvent,
    effective: EffectiveBot,
    text: str,
    *,
    still_valid: Callable[[], bool] | None = None,
    formatted: bool = False,
) -> bool:
    try:
        # Token acquisition can block on Feishu and must stay outside the gate.
        token = await client.get_tenant_token()
        async with _gate(effective.bot_id):
            if not _pool.is_current(effective):
                return False
            if still_valid is not None and not still_valid():
                return False
            # An already-sent request cannot be recalled, so a timeout is never retried.
            if formatted:
                payload = build_feishu_message_payload(text)
                outbound = client.reply_message(
                    event.message_id,
                    payload.msg_type,
                    payload.content,
                    access_token=token,
                )
            else:
                outbound = client.reply_text(event.message_id, text, access_token=token)
            await asyncio.wait_for(outbound, timeout=_OUTBOUND_POST_TIMEOUT_SECONDS)
            return True
    except Exception as exc:
        logger.warning("Feishu Bot reply failed: %s", type(exc).__name__)
        return False


async def _add_processing_reaction(
    client: FeishuBotClient, event: FeishuMessageEvent
) -> str | None:
    """Best-effort hint; legacy Bots may not have the reaction scope."""

    try:
        # ``wait_for`` creates a child task and then waits for its cancellation
        # handshake when the route is cancelled. Keeping the HTTP await in this
        # task lets shutdown cancellation release its FIFO reservation promptly.
        async with asyncio.timeout(_REACTION_POST_TIMEOUT_SECONDS):
            return await client.add_reaction(event.message_id, "Typing")
    except Exception as exc:
        logger.info("Feishu Bot processing reaction unavailable: %s", type(exc).__name__)
        return None


async def _delete_processing_reaction(
    client: FeishuBotClient, event: FeishuMessageEvent, reaction_id: str | None
) -> None:
    if reaction_id is None:
        return
    try:
        async with asyncio.timeout(_REACTION_POST_TIMEOUT_SECONDS):
            await client.delete_reaction(event.message_id, reaction_id)
    except Exception as exc:
        logger.info("Feishu Bot processing reaction cleanup unavailable: %s", type(exc).__name__)


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
                bot_id, code=code, sender_open_id=event.sender_open_id, chat_id=event.chat_id
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
    reservation: _TabDispatchReservation | None = None
    reaction_id: str | None = None
    dispatch_started = False
    try:
        if not _pool.is_current(effective):
            return
        code = _binding_code(event.text)
        if code is not None:
            if await _handle_pairing_code(bot_id, event, effective, client, code):
                status_value = "completed"
            return
        binding = _pool.routed_binding(
            bot_id, sender_open_id=event.sender_open_id, chat_id=event.chat_id
        )
        if binding is None:
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
            reservation = _reserve_tab_dispatch(binding.tab_id)
        except _TabDispatchQueueFull:
            reaction_id = await _add_processing_reaction(client, event)
            await _reply_if_current(
                client,
                event,
                effective,
                "Claude Hub 消息队列已满，本条消息未执行，请稍后重试。",
                still_valid=lambda: binding is not None
                and _binding_still_routes(effective, binding),
            )
            return
        reaction_id = await _add_processing_reaction(client, event)
        await reservation.__aenter__()
        dispatch_binding = binding
        dispatch_started = True
        assistant_text = await dispatch_tab_chat_and_wait(
            binding.tab_id,
            format_feishu_provider_text_v2(
                event.text,
                app_id=event.app_id,
                chat_id=event.chat_id,
                message_id=event.message_id,
                sender_open_id=event.sender_open_id,
            ),
            turn_id_for(bot_id, event.message_id),
            visible_text=event.text,
            turn_metadata=_turn_metadata(bot_id, event),
            admission_guard=lambda: _admit_binding_dispatch(effective, dispatch_binding, event),
        )
        if await _reply_if_current(
            client,
            event,
            effective,
            assistant_text,
            still_valid=(
                (lambda: binding is not None and _binding_still_routes(effective, binding))
                if binding is not None
                else None
            ),
            formatted=True,
        ):
            status_value = "completed"
    except asyncio.CancelledError:
        if not dispatch_started:
            # A queued message cancelled during shutdown never entered the Chat
            # bridge, so its durable at-most-once claim must remain retryable.
            # The outer WebSocket intake releases only this explicit retirement
            # signal after the reservation/reaction cleanup below has completed.
            raise ExternalDispatchRetired(turn_id_for(bot_id, event.message_id)) from None
        raise
    except HTTPException as exc:
        logger.warning("Feishu Bot target rejected: status=%s", exc.status_code)
        reason = (
            (exc.headers or {}).get(CHAT_ERROR_REASON_HEADER) if exc.status_code == 409 else None
        )
        if reason == "message_outside_binding_period":
            return
        if reason == "chat_busy":
            message = "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"
        elif exc.status_code == 409 and reason != "binding_target_missing":
            message = "Claude Hub Chat 当前不可用，本条消息尚未执行。请在网页检查 Chat 状态后重试。"
        else:
            message = "Claude Hub 目标当前不可用，请在网页重新配对。"
        await _reply_if_current(
            client,
            event,
            effective,
            message,
            still_valid=(
                (lambda: binding is not None and _binding_still_routes(effective, binding))
                if binding is not None
                else None
            ),
        )
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
        if reservation is not None:
            reservation.cancel()
        try:
            await _delete_processing_reaction(client, event, reaction_id)
        finally:
            _dedup.finish(dedup_key(bot_id, event.message_id), status_value)


async def _handle_sdk_event(bot_id: str, data: Any) -> None:
    try:
        effective = _pool.effective(bot_id)
        event = message_event_from_sdk(data, expected_app_id=effective.app_id)
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
