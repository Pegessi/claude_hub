"""Feishu Bot pool administration and pairing routes."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from claude_hub.api.agent_stream import CHAT_ERROR_REASON_HEADER
from claude_hub.api.routes.feishu_bot.bindings import FeishuPoolRouteBindings
from claude_hub.models import ExecutionTarget, SessionKind, User
from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuBotProfile, FeishuBotRemoteError
from claude_hub.services.feishu_bot_pool import (
    MAX_BOTS,
    BotBinding,
    BotEntry,
    FeishuBindingCodeError,
    FeishuBotAppIdConflict,
    FeishuBotNotFound,
    FeishuBotOccupied,
    FeishuBotPoolError,
    FeishuBotPoolStateError,
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
)

logger = logging.getLogger(__name__)
BODY_BYTES = 16 * 1024


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
    feishu_name: str | None
    feishu_avatar_url: str | None
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


def utc(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=timezone.utc)


def pool_http_error(exc: FeishuBotPoolError) -> HTTPException:
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


def get_pool_snapshot(bindings: FeishuPoolRouteBindings) -> PoolSnapshot:
    try:
        return bindings.pool().snapshot()
    except FeishuBotPoolError as exc:
        raise pool_http_error(exc) from exc


def pairing_view(
    entry: BotEntry,
    pairing: BotBinding | PairingClaim,
    *,
    state: str,
    is_mine: bool,
    expires_at: float | None = None,
) -> FeishuPairingView:
    created = utc(pairing.created_at)
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
        expires_at=utc(expires_at),
    )


def is_mine(owner: OwnerIdentity, actor: OwnerIdentity) -> bool:
    return owner.kind == actor.kind and owner.open_id == actor.open_id


def summary(
    entry: BotEntry, actor: OwnerIdentity, bindings: FeishuPoolRouteBindings
) -> FeishuBotSummary:
    config = entry.config
    binding = None
    if entry.binding is not None:
        binding = pairing_view(
            entry,
            entry.binding,
            state="active",
            is_mine=is_mine(entry.binding.owner, actor),
        )
    claims = [
        pairing_view(entry, claim, state="claimed", is_mine=True, expires_at=claim.expires_at)
        for claim in entry.claims
        if is_mine(claim.owner, actor)
    ]
    return FeishuBotSummary(
        bot_id=entry.bot_id,
        name=entry.name,
        feishu_name=entry.feishu_name,
        feishu_avatar_url=entry.feishu_avatar_url,
        app_id=entry.app_id,
        source=entry.source,
        enabled=entry.enabled,
        credentials_editable=entry.credentials_editable,
        deletable=entry.deletable,
        revision=entry.revision,
        generation=entry.generation,
        configured=entry.configured,
        app_secret_configured=bool(config and config.app_secret),
        connection_status=bindings.websocket_supervisor().status(entry.bot_id),
        updated_at=utc(entry.updated_at),
        binding=binding,
        my_claims=claims,
    )


def validated_profile(value: object, fallback_name: str) -> FeishuBotProfile:
    """Keep simple test adapters compatible while production returns a profile."""

    if isinstance(value, FeishuBotProfile):
        return value
    return FeishuBotProfile(name=fallback_name, avatar_url=None, open_id=None)


def pool_response(
    user: User, bindings: FeishuPoolRouteBindings, *, focus_bot_id: str | None = None
) -> FeishuBotPoolResponse:
    actor = bindings.owner_identity(user)
    pool_snapshot = get_pool_snapshot(bindings)
    return FeishuBotPoolResponse(
        pool_revision=pool_snapshot.pool_revision,
        bots=[summary(entry, actor, bindings) for entry in pool_snapshot.bots],
        pool_editable=True,
        deprecated_env=list(pool_snapshot.deprecated_env),
        focus_bot_id=focus_bot_id,
    )


async def read_json(request: Request) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_BYTES:
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_bot_request") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return value


def exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise HTTPException(status_code=400, detail="invalid_bot_request")


def parse_secret(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw.strip()


def parse_name(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 64:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw.strip()


def parse_revision(value: dict[str, Any]) -> int:
    raw = value.get("expected_revision")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise HTTPException(status_code=400, detail="invalid_bot_request")
    return raw


def validate_bind_target(
    tab_id: str, requested_workspace_id: str | None, bindings: FeishuPoolRouteBindings
) -> str | None:
    tab = bindings.ttyd_manager().get_tab(tab_id)
    process = bindings.ttyd_manager().processes.get(tab_id)
    if tab is None or process is None or getattr(process, "archived", False):
        raise HTTPException(status_code=404, detail="chat_tab_not_found")
    if tab.session_kind != SessionKind.CHAT or tab.target != ExecutionTarget.LOCAL:
        raise HTTPException(status_code=400, detail="chat_tab_not_eligible")
    if tab.workspace_role is not None:
        raise HTTPException(status_code=400, detail="chat_tab_not_eligible")
    actual = str(tab.workspace_id) if tab.workspace_id is not None else None
    if requested_workspace_id is not None and requested_workspace_id != actual:
        raise HTTPException(status_code=403, detail="chat_tab_workspace_mismatch")
    if actual is not None and actual not in bindings.workspace_manager().workspaces:
        raise HTTPException(
            status_code=409,
            detail="chat_tab_workspace_missing",
            headers={CHAT_ERROR_REASON_HEADER: "binding_target_missing"},
        )
    return actual


def validate_saved_target(
    pairing: BotBinding | PairingClaim, bindings: FeishuPoolRouteBindings
) -> None:
    actual = bindings.validate_bind_target(pairing.tab_id, pairing.workspace_id)
    # None in a saved pairing means an originally unscoped Chat, not permission
    # to follow that tab into an arbitrary later workspace.
    if actual != pairing.workspace_id:
        raise HTTPException(
            status_code=409,
            detail="chat_tab_workspace_changed",
            headers={CHAT_ERROR_REASON_HEADER: "binding_target_missing"},
        )


async def in_pool(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except FeishuBotPoolError as exc:
        raise pool_http_error(exc) from exc


async def reconcile_ws(bindings: FeishuPoolRouteBindings) -> None:
    """Apply pool mutations immediately; the monitor remains the retry path."""

    try:
        await bindings.websocket_supervisor().reconcile()
    except Exception:
        logger.exception("Feishu WebSocket reconciliation failed after a pool mutation")


def credentials_http_error(exc: Exception) -> HTTPException:
    """Name the Feishu-side policy that refused the credentials, when known."""
    if not isinstance(exc, FeishuBotRemoteError):
        return HTTPException(status_code=502, detail="feishu_credentials_rejected")
    logger.warning(
        "Feishu Bot credential validation failed: remote_code=%s log_id=%s",
        exc.code,
        exc.log_id or "unavailable",
    )
    if str(exc.code) != "21001":
        return HTTPException(status_code=502, detail="feishu_credentials_rejected")
    # 21001 is an app access-control decision, not a wrong secret, so the
    # operator has to involve the app administrator instead of retyping it.
    detail = {"code": "feishu_access_control_denied", "upstream_code": "21001"}
    if exc.log_id is not None:
        detail["request_id"] = exc.log_id
    return HTTPException(status_code=502, detail=detail)


def gone() -> HTTPException:
    return HTTPException(status_code=410, detail="interface_replaced_by_bot_pool")


@dataclass(frozen=True)
class FeishuPoolEndpoints:
    list_feishu_bots: Callable[..., Any]
    create_feishu_bot: Callable[..., Any]
    rotate_feishu_bot_secrets: Callable[..., Any]
    update_feishu_bot: Callable[..., Any]
    delete_feishu_bot: Callable[..., Any]
    start_feishu_pairing: Callable[..., Any]
    activate_feishu_pairing: Callable[..., Any]
    release_feishu_pairing: Callable[..., Any]
    removed_single_bot_config_status: Callable[..., Any]
    removed_single_bot_get_config: Callable[..., Any]
    removed_single_bot_put_config: Callable[..., Any]
    removed_single_bot_delete_config: Callable[..., Any]
    removed_single_bot_bind_start: Callable[..., Any]
    removed_single_bot_get_binding: Callable[..., Any]
    removed_single_bot_delete_binding: Callable[..., Any]


def register_pool_routes(
    router: APIRouter,
    bindings: FeishuPoolRouteBindings,
    require_hub_user: Callable[..., User],
) -> FeishuPoolEndpoints:
    """Register pool routes in their historical order."""

    @router.get("/bots", response_model=FeishuBotPoolResponse)
    async def list_feishu_bots(
        user: User = Depends(require_hub_user),
    ) -> FeishuBotPoolResponse:
        return pool_response(user, bindings)

    @router.post("/bots", response_model=FeishuBotPoolResponse, status_code=201)
    async def create_feishu_bot(
        request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuBotPoolResponse:
        value = await read_json(request)
        exact_keys(value, {"name", "app_id", "app_secret"})
        candidate = FeishuBotConfig(
            app_id=parse_secret(value, "app_id"),
            app_secret=parse_secret(value, "app_secret"),
        )
        name = parse_name(value, "name")
        snapshot = get_pool_snapshot(bindings)
        if len(snapshot.bots) >= MAX_BOTS:
            raise HTTPException(status_code=409, detail="pool_capacity_reached")
        if any(entry.app_id == candidate.app_id for entry in snapshot.bots):
            raise HTTPException(status_code=409, detail="app_id_already_in_pool")
        # Credential validation reaches Feishu and must never hold a gate.
        try:
            profile = validated_profile(
                await bindings.bot_client_class()(candidate).validate_credentials(), name
            )
        except Exception as exc:
            raise credentials_http_error(exc) from exc
        async with bindings.create_gate():
            bot_id = await in_pool(
                lambda: bindings.pool().create_bot(
                    name=name,
                    config=candidate,
                    feishu_name=profile.name,
                    feishu_avatar_url=profile.avatar_url,
                    feishu_open_id=profile.open_id,
                )
            )
        await reconcile_ws(bindings)
        return pool_response(user, bindings, focus_bot_id=bot_id)

    @router.put("/bots/{bot_id}/secrets", response_model=FeishuBotPoolResponse)
    async def rotate_feishu_bot_secrets(
        bot_id: str, request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuBotPoolResponse:
        value = await read_json(request)
        exact_keys(value, {"app_secret", "expected_revision"})
        expected_revision = parse_revision(value)
        snapshot = get_pool_snapshot(bindings)
        entry = snapshot.get(bot_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="bot_not_found")
        if not entry.credentials_editable:
            raise HTTPException(status_code=409, detail="bot_credentials_read_only")
        candidate = FeishuBotConfig(
            app_id=entry.app_id,
            app_secret=parse_secret(value, "app_secret"),
        )
        try:
            profile = validated_profile(
                await bindings.bot_client_class()(candidate).validate_credentials(), entry.name
            )
        except Exception as exc:
            raise credentials_http_error(exc) from exc
        async with bindings.gate(bot_id):
            await in_pool(
                lambda: bindings.pool().rotate_secrets(
                    bot_id,
                    app_secret=candidate.app_secret,
                    feishu_name=profile.name,
                    feishu_avatar_url=profile.avatar_url,
                    feishu_open_id=profile.open_id,
                    expected_revision=expected_revision,
                )
            )
        await reconcile_ws(bindings)
        return pool_response(user, bindings, focus_bot_id=bot_id)

    @router.patch("/bots/{bot_id}", response_model=FeishuBotPoolResponse)
    async def update_feishu_bot(
        bot_id: str, request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuBotPoolResponse:
        value = await read_json(request)
        if (
            not set(value) <= {"name", "enabled", "expected_revision"}
            or "expected_revision" not in value
        ):
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        expected_revision = parse_revision(value)
        name = parse_name(value, "name") if "name" in value else None
        enabled = value.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        if get_pool_snapshot(bindings).get(bot_id) is None:
            raise HTTPException(status_code=404, detail="bot_not_found")
        async with bindings.gate(bot_id):
            await in_pool(
                lambda: bindings.pool().update_bot(
                    bot_id, expected_revision=expected_revision, name=name, enabled=enabled
                )
            )
        await reconcile_ws(bindings)
        return pool_response(user, bindings, focus_bot_id=bot_id)

    @router.delete("/bots/{bot_id}", response_model=FeishuBotPoolResponse)
    async def delete_feishu_bot(
        bot_id: str, request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuBotPoolResponse:
        value = await read_json(request)
        exact_keys(value, {"expected_revision"})
        expected_revision = parse_revision(value)
        if get_pool_snapshot(bindings).get(bot_id) is None:
            raise HTTPException(status_code=404, detail="bot_not_found")
        async with bindings.gate(bot_id):
            await in_pool(
                lambda: bindings.pool().delete_bot(bot_id, expected_revision=expected_revision)
            )
        await reconcile_ws(bindings)
        return pool_response(user, bindings, focus_bot_id=None)

    @router.post(
        "/bots/{bot_id}/pair/start", response_model=FeishuPairStartResponse, status_code=201
    )
    async def start_feishu_pairing(
        bot_id: str, request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuPairStartResponse:
        value = await read_json(request)
        if not set(value) <= {"tab_id", "workspace_id", "expected_revision"} or not {
            "tab_id",
            "expected_revision",
        } <= set(value):
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        expected_revision = parse_revision(value)
        tab_id = parse_secret(value, "tab_id")
        requested_workspace = value.get("workspace_id")
        if requested_workspace is not None and not isinstance(requested_workspace, str):
            raise HTTPException(status_code=400, detail="invalid_bot_request")
        owner = bindings.owner_identity(user)
        async with bindings.gate(bot_id):
            entry = get_pool_snapshot(bindings).get(bot_id)
            if entry is None:
                raise HTTPException(status_code=404, detail="bot_not_found")
            if not entry.configured:
                raise HTTPException(status_code=503, detail="bot_pool_unavailable")
            if not bindings.owner_is_authorized(owner):
                raise HTTPException(status_code=403, detail="hub_identity_revoked")
            workspace_id = bindings.validate_bind_target(tab_id, requested_workspace)
            code, expires_at, revision = await in_pool(
                lambda: bindings.pool().issue_code(
                    bot_id,
                    owner=owner,
                    tab_id=tab_id,
                    workspace_id=workspace_id,
                    expected_revision=expected_revision,
                )
            )
        issued = utc(expires_at)
        assert issued is not None
        return FeishuPairStartResponse(
            bot_id=bot_id, code=code, expires_at=issued, revision=revision
        )

    @router.post("/bots/{bot_id}/pair/activate", response_model=FeishuBotPoolResponse)
    async def activate_feishu_pairing(
        bot_id: str, request: Request, user: User = Depends(require_hub_user)
    ) -> FeishuBotPoolResponse:
        value = await read_json(request)
        exact_keys(value, {"pairing_id", "confirm_word", "expected_revision"})
        expected_revision = parse_revision(value)
        pairing_id = parse_secret(value, "pairing_id")
        confirm_word = parse_secret(value, "confirm_word")
        actor = bindings.owner_identity(user)
        async with bindings.gate(bot_id):
            if not bindings.owner_is_authorized(actor):
                raise HTTPException(status_code=403, detail="hub_identity_revoked")
            entry = get_pool_snapshot(bindings).get(bot_id)
            if entry is None:
                raise HTTPException(status_code=404, detail="bot_not_found")
            claim = next((item for item in entry.claims if item.pairing_id == pairing_id), None)
            if claim is not None:
                bindings.validate_saved_target(claim)
            await in_pool(
                lambda: bindings.pool().activate_claim(
                    bot_id,
                    pairing_id=pairing_id,
                    confirm_word=confirm_word,
                    actor=actor,
                    expected_revision=expected_revision,
                )
            )
        return pool_response(user, bindings, focus_bot_id=bot_id)

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

        value = await read_json(request)
        exact_keys(value, {"expected_revision"})
        expected_revision = parse_revision(value)
        async with bindings.gate(bot_id):
            await in_pool(
                lambda: bindings.pool().release_binding(bot_id, expected_revision=expected_revision)
            )
        return pool_response(user, bindings, focus_bot_id=bot_id)

    @router.api_route("/config/status", methods=["GET"])
    async def removed_single_bot_config_status() -> None:
        raise gone()

    @router.get("/config")
    async def removed_single_bot_get_config() -> None:
        raise gone()

    @router.put("/config")
    async def removed_single_bot_put_config() -> None:
        raise gone()

    @router.delete("/config")
    async def removed_single_bot_delete_config() -> None:
        raise gone()

    @router.api_route("/bind/start", methods=["POST"])
    async def removed_single_bot_bind_start() -> None:
        raise gone()

    @router.get("/binding")
    async def removed_single_bot_get_binding() -> None:
        raise gone()

    @router.delete("/binding")
    async def removed_single_bot_delete_binding() -> None:
        raise gone()

    return FeishuPoolEndpoints(
        list_feishu_bots=list_feishu_bots,
        create_feishu_bot=create_feishu_bot,
        rotate_feishu_bot_secrets=rotate_feishu_bot_secrets,
        update_feishu_bot=update_feishu_bot,
        delete_feishu_bot=delete_feishu_bot,
        start_feishu_pairing=start_feishu_pairing,
        activate_feishu_pairing=activate_feishu_pairing,
        release_feishu_pairing=release_feishu_pairing,
        removed_single_bot_config_status=removed_single_bot_config_status,
        removed_single_bot_get_config=removed_single_bot_get_config,
        removed_single_bot_put_config=removed_single_bot_put_config,
        removed_single_bot_delete_config=removed_single_bot_delete_config,
        removed_single_bot_bind_start=removed_single_bot_bind_start,
        removed_single_bot_get_binding=removed_single_bot_get_binding,
        removed_single_bot_delete_binding=removed_single_bot_delete_binding,
    )
