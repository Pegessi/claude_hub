"""Feishu user-identity OpenAPI client for the personal ``/ai`` assistant workflow.

Reuses ``FeishuBotClient``, ``create_feishu_http_client``, ``FeishuBotRemoteError``,
and message parsing helpers from ``feishu_bot.py`` so there is one HTTP and
reaction/reply implementation across Bot and User identities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, TypeVar
from urllib.parse import quote

import httpx

from claude_hub.auth.feishu import refresh_user_access_token_bundle
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotConfigurationError,
    FeishuBotError,
    FeishuBotRemoteError,
    FeishuChatInfo,
    FeishuDeliveryUncertainError,
    FeishuReplyReceipt,
    _chat_info_from_payload,
    _forward_body,
    _forward_content,
    create_feishu_http_client,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenRecord,
    FeishuUserTokenStore,
    get_feishu_user_token_store,
)

_T = TypeVar("_T")
logger = logging.getLogger(__name__)

REQUIRED_SEND_AS_USER_SCOPE = "im:message.send_as_user"
REQUIRED_IM_MESSAGE_SCOPE = "im:message"
RECOMMENDED_AI_COMMAND_SCOPES: tuple[str, ...] = (
    "im:message",
    "im:message.reactions:write_only",
    "im:chat:read",
    "im:message.group_msg:get_as_user",
    "im:message.p2p_msg:get_as_user",
    "search:message",
    "im:message:readonly",
)
_EXPIRED_TOKEN_CODES = {
    99991677,
    99991663,
    99991668,
    "99991677",
    "99991663",
    "99991668",
}
_MISSING_SEND_AS_USER_CODES = {
    230027,
    "230027",
}
_BOT_NOT_IN_CHAT_CODES = {
    230002,
    "230002",
}


class FeishuUserScopeError(FeishuBotConfigurationError):
    """Raised when the user token lacks required scopes and no fallback bot can send."""

    def __init__(
        self,
        message: str,
        *,
        missing_scopes: tuple[str, ...] = (REQUIRED_SEND_AS_USER_SCOPE,),
        auth_login_command: str = "",
    ) -> None:
        super().__init__(message)
        self.missing_scopes = missing_scopes
        self.auth_login_command = auth_login_command


@dataclass(frozen=True)
class FeishuScopePreflightResult:
    """Scope readiness and remediation guidance for ``/ai`` user-identity actions."""

    has_token: bool
    open_id: str | None
    user_name: str | None
    app_id: str | None
    source: str | None
    expires_at: float
    token_expired: bool
    granted_scopes: tuple[str, ...]
    required_send_scope: str
    can_send_as_user: bool
    can_react_as_user: bool
    can_read_messages: bool
    missing_required_scopes: tuple[str, ...]
    missing_recommended_scopes: tuple[str, ...]
    auth_login_command: str
    remediation_steps: tuple[str, ...]
    can_send: bool = False
    send_mode: str = "none"
    has_send_as_user_scope: bool = False
    can_send_via_bot: bool = False
    fallback_bot_app_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_token": self.has_token,
            "open_id": self.open_id,
            "user_name": self.user_name,
            "app_id": self.app_id,
            "source": self.source,
            "expires_at": self.expires_at,
            "token_expired": self.token_expired,
            "granted_scopes": list(self.granted_scopes),
            "required_send_scope": self.required_send_scope,
            "can_send": self.can_send,
            "can_send_as_user": self.can_send_as_user,
            "has_send_as_user_scope": self.has_send_as_user_scope,
            "can_send_via_bot": self.can_send_via_bot,
            "fallback_bot_app_id": self.fallback_bot_app_id,
            "send_mode": self.send_mode,
            "can_react_as_user": self.can_react_as_user,
            "can_read_messages": self.can_read_messages,
            "missing_required_scopes": list(self.missing_required_scopes),
            "missing_recommended_scopes": list(self.missing_recommended_scopes),
            "auth_login_command": self.auth_login_command,
            "remediation_steps": list(self.remediation_steps),
        }


@dataclass(frozen=True)
class FeishuUserMessage:
    """Normalized message record returned by user-identity IM read APIs."""

    message_id: str
    chat_id: str
    sender_open_id: str
    sender_type: str
    msg_type: str
    text: str
    create_time_ms: int
    thread_id: str | None = None
    parent_id: str | None = None
    root_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "sender_open_id": self.sender_open_id,
            "sender_type": self.sender_type,
            "msg_type": self.msg_type,
            "text": self.text,
            "create_time_ms": self.create_time_ms,
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "root_id": self.root_id,
        }


@dataclass(frozen=True)
class FeishuMessagePage:
    """One paginated page of messages from ``GET /open-apis/im/v1/messages``."""

    items: tuple[FeishuUserMessage, ...]
    has_more: bool = False
    page_token: str = ""


def build_auth_login_command(
    scopes: tuple[str, ...] = RECOMMENDED_AI_COMMAND_SCOPES,
) -> str:
    scope_arg = " ".join(scopes)
    return f'lark-cli auth login --scope "{scope_arg}" --no-wait --json'


def evaluate_scope_preflight(
    record: FeishuUserTokenRecord | None,
    *,
    now: float | None = None,
    fallback_bot_app_id: str | None = None,
) -> FeishuScopePreflightResult:
    """Inspect a user token record and produce clear scope & auth guidance."""
    cmd = build_auth_login_command()
    if record is None:
        steps = (
            f"1. Authenticate via lark-cli: `{cmd}` (or sign in via Hub Feishu OAuth).",
            "2. Run `claude-hub feishu ai-command status` or click 'Import ~/.lark-cli Token' in Settings.",
        )
        return FeishuScopePreflightResult(
            has_token=False,
            open_id=None,
            user_name=None,
            app_id=None,
            source=None,
            expires_at=0.0,
            token_expired=True,
            granted_scopes=(),
            required_send_scope=REQUIRED_SEND_AS_USER_SCOPE,
            can_send_as_user=False,
            can_react_as_user=False,
            can_read_messages=False,
            missing_required_scopes=(
                REQUIRED_SEND_AS_USER_SCOPE,
                REQUIRED_IM_MESSAGE_SCOPE,
            ),
            missing_recommended_scopes=RECOMMENDED_AI_COMMAND_SCOPES,
            auth_login_command=cmd,
            remediation_steps=steps,
            can_send=False,
            send_mode="none",
            has_send_as_user_scope=False,
            can_send_via_bot=False,
            fallback_bot_app_id=None,
        )

    granted = record.scopes_set()
    has_send_as_user = REQUIRED_SEND_AS_USER_SCOPE in granted
    can_react = bool(granted & {"im:message.reactions:write_only", "im:message"})
    can_read = bool(
        granted
        & {
            "im:message:readonly",
            "im:message",
            "im:message.group_msg:get_as_user",
            "im:message.p2p_msg:get_as_user",
        }
    )
    resolved_bot_app = (
        record.app_id
        if (fallback_bot_app_id or "").strip() == record.app_id
        else (record.app_id if (fallback_bot_app_id is None and record.app_secret.strip()) else "")
    )
    can_send_via_bot = bool(resolved_bot_app and can_read)
    can_send = has_send_as_user or can_send_via_bot
    send_mode = "user" if has_send_as_user else ("bot_fallback" if can_send_via_bot else "none")
    missing_req: list[str] = []
    if not has_send_as_user:
        missing_req.append(REQUIRED_SEND_AS_USER_SCOPE)
    if not can_read and REQUIRED_IM_MESSAGE_SCOPE not in granted:
        missing_req.append(REQUIRED_IM_MESSAGE_SCOPE)
    missing_rec = [s for s in RECOMMENDED_AI_COMMAND_SCOPES if s not in granted]

    steps_list: list[str] = []
    if has_send_as_user:
        steps_list.append(
            "User token includes `im:message.send_as_user` and is ready to send as user."
        )
    elif can_send_via_bot:
        steps_list.append(
            f"Ready via bot fallback (`{resolved_bot_app}`): user token reads context and reacts "
            f"as {record.open_id}, while replies are sent by app `{resolved_bot_app}` "
            f"(`{REQUIRED_SEND_AS_USER_SCOPE}` is not granted on `{record.app_id}`)."
        )
    else:
        steps_list.append(
            f"1. Configure app secret for `{record.app_id}` (or enable `{REQUIRED_SEND_AS_USER_SCOPE}` "
            f"in Feishu Developer Console for `{record.app_id}`)."
        )
        steps_list.append(
            f"2. Re-authorize your user token with: `{cmd}` and complete device verification."
        )
        steps_list.append(
            "3. Reload the token with `claude-hub feishu ai-command configure --import-lark-cli` "
            "or via the Hub Feishu Settings UI."
        )

    return FeishuScopePreflightResult(
        has_token=True,
        open_id=record.open_id,
        user_name=record.user_name or None,
        app_id=record.app_id,
        source=record.source,
        expires_at=record.expires_at,
        token_expired=record.is_expired(now=now, skew_seconds=0.0),
        granted_scopes=tuple(sorted(granted)),
        required_send_scope=REQUIRED_SEND_AS_USER_SCOPE,
        can_send_as_user=has_send_as_user,
        can_react_as_user=can_react,
        can_read_messages=can_read,
        missing_required_scopes=tuple(missing_req),
        missing_recommended_scopes=tuple(missing_rec),
        auth_login_command=cmd,
        remediation_steps=tuple(steps_list),
        can_send=can_send,
        send_mode=send_mode,
        has_send_as_user_scope=has_send_as_user,
        can_send_via_bot=can_send_via_bot,
        fallback_bot_app_id=resolved_bot_app or None,
    )


def _extract_item_text(item: Mapping[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or item.get("message_type") or "text")
    body = _forward_body(item)
    if msg_type == "text":
        if isinstance(body, Mapping):
            raw = body.get("text")
            if isinstance(raw, str):
                text = raw
            else:
                text = ""
        elif isinstance(body, str):
            text = body
        else:
            text = ""
    else:
        text, _ = _forward_content(body)
    # Substitute mention placeholders (@_user_1) when mentions metadata is present
    mentions = item.get("mentions")
    if isinstance(mentions, list) and text:
        for mention in mentions:
            if not isinstance(mention, Mapping):
                continue
            key = mention.get("key")
            name = mention.get("name")
            if isinstance(key, str) and key and isinstance(name, str) and name:
                text = text.replace(key, f"@{name}")
    return text.strip()


def _parse_user_message(
    item: Mapping[str, Any], default_chat_id: str = ""
) -> FeishuUserMessage | None:
    if item.get("deleted") is True:
        return None
    message_id = str(item.get("message_id") or item.get("id") or "").strip()
    if not message_id:
        return None
    chat_id = str(item.get("chat_id") or default_chat_id or "").strip()
    sender = item.get("sender")
    if isinstance(sender, Mapping):
        sender_open_id = str(
            sender.get("id") or sender.get("open_id") or sender.get("sender_id") or ""
        ).strip()
        sender_type = str(sender.get("sender_type") or "user").strip()
    else:
        sender_open_id = str(item.get("sender_open_id") or "").strip()
        sender_type = str(item.get("sender_type") or "user").strip()
    msg_type = str(item.get("msg_type") or item.get("message_type") or "text").strip()
    raw_ctime = item.get("create_time")
    try:
        ctime_val = int(str(raw_ctime).strip()) if raw_ctime is not None else 0
        if 0 < ctime_val < 10_000_000_000:
            ctime_val *= 1000
    except (ValueError, TypeError):
        ctime_val = 0
    thread_id = str(item.get("thread_id") or "").strip() or None
    parent_id = str(item.get("parent_id") or "").strip() or None
    root_id = str(item.get("root_id") or "").strip() or None
    text = _extract_item_text(item)
    return FeishuUserMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_open_id=sender_open_id,
        sender_type=sender_type,
        msg_type=msg_type,
        text=text,
        create_time_ms=ctime_val,
        thread_id=thread_id,
        parent_id=parent_id,
        root_id=root_id,
    )


_RECORD_OMITTED = object()


class FeishuUserClient:
    """User-identity Feishu client reusing ``FeishuBotClient`` HTTP/reaction/reply primitives."""

    def __init__(
        self,
        token_store: FeishuUserTokenStore | None = None,
        *,
        open_id: str | None = None,
        app_id: str | None = None,
        api_base_url: str = "https://open.feishu.cn",
        client: httpx.AsyncClient | None = None,
        token_refresher: Callable[..., Any] | None = None,
    ) -> None:
        self._store = token_store or get_feishu_user_token_store()
        self._open_id = (open_id or "").strip() or None
        self._app_id = (app_id or "").strip() or None
        self._api_base_url = api_base_url.rstrip("/")
        self._client = client
        self._token_refresher = token_refresher or refresh_user_access_token_bundle
        self._bot_client = FeishuBotClient(
            FeishuBotConfig(
                app_id=self._app_id or "user-client",
                app_secret="unused-for-user-token",
                api_base_url=self._api_base_url,
            ),
            client=client,
        )
        self._fallback_bot_clients: dict[str, FeishuBotClient] = {}

    def _resolve_fallback_bot_configs(
        self, record: FeishuUserTokenRecord | None = None
    ) -> list[tuple[str, str]]:
        if hasattr(self._store, "resolve_fallback_bot_configs"):
            return self._store.resolve_fallback_bot_configs(
                preferred_app_id=self._app_id, record=record
            )
        if record is not None and record.app_id and record.app_secret.strip():
            return [(record.app_id, record.app_secret.strip())]
        return []

    def _get_fallback_bot_client(self, app_id: str, app_secret: str) -> FeishuBotClient:
        key = f"{app_id}:{app_secret}"
        bot_client = self._fallback_bot_clients.get(key)
        if bot_client is None:
            bot_client = FeishuBotClient(
                FeishuBotConfig(
                    app_id=app_id,
                    app_secret=app_secret,
                    api_base_url=self._api_base_url,
                ),
                client=self._client,
            )
            self._fallback_bot_clients[key] = bot_client
        return bot_client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, path, **kwargs)
        async with create_feishu_http_client(self._api_base_url, timeout=15.0) as client:
            return await client.request(method, path, **kwargs)

    def resolve_record(self, *, auto_import_lark_cli: bool = True) -> FeishuUserTokenRecord | None:
        return self._store.resolve_token(
            open_id=self._open_id,
            app_id=self._app_id,
            auto_import_lark_cli=auto_import_lark_cli,
        )

    async def aresolve_record(
        self, *, auto_import_lark_cli: bool = True
    ) -> FeishuUserTokenRecord | None:
        if hasattr(self._store, "aresolve_token"):
            return await self._store.aresolve_token(
                open_id=self._open_id,
                app_id=self._app_id,
                auto_import_lark_cli=auto_import_lark_cli,
            )
        async with self._store.async_file_lock(
            app_id=self._app_id or "", open_id=self._open_id or ""
        ):
            return self.resolve_record(auto_import_lark_cli=auto_import_lark_cli)

    def preflight_scopes(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
    ) -> FeishuScopePreflightResult:
        if record is _RECORD_OMITTED:
            target = self.resolve_record(auto_import_lark_cli=auto_import_lark_cli)
        else:
            target = record if isinstance(record, FeishuUserTokenRecord) else None
        fb_configs = self._resolve_fallback_bot_configs(target)
        fb_app_id = fb_configs[0][0] if fb_configs else ""
        return evaluate_scope_preflight(target, fallback_bot_app_id=fb_app_id)

    async def apreflight_scopes(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
    ) -> FeishuScopePreflightResult:
        if record is _RECORD_OMITTED:
            target = await self.aresolve_record(auto_import_lark_cli=auto_import_lark_cli)
        else:
            target = record if isinstance(record, FeishuUserTokenRecord) else None
        fb_configs = self._resolve_fallback_bot_configs(target)
        fb_app_id = fb_configs[0][0] if fb_configs else ""
        return evaluate_scope_preflight(target, fallback_bot_app_id=fb_app_id)

    def require_send_as_user_scope(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
        allow_bot_fallback: bool = False,
    ) -> FeishuUserTokenRecord:
        if record is _RECORD_OMITTED:
            target = self.resolve_record(auto_import_lark_cli=auto_import_lark_cli)
        else:
            target = record if isinstance(record, FeishuUserTokenRecord) else None
        fb_configs = self._resolve_fallback_bot_configs(target) if allow_bot_fallback else []
        fb_app_id = fb_configs[0][0] if fb_configs else ""
        preflight = evaluate_scope_preflight(target, fallback_bot_app_id=fb_app_id)
        if target is None:
            raise FeishuUserScopeError(
                "No Feishu user access token is available. "
                f"Run `{preflight.auth_login_command}` or sign in via Hub Feishu OAuth.",
                missing_scopes=preflight.missing_required_scopes,
                auth_login_command=preflight.auth_login_command,
            )
        can_proceed = preflight.can_send if allow_bot_fallback else preflight.has_send_as_user_scope
        if not can_proceed:
            missing = preflight.missing_required_scopes or (REQUIRED_SEND_AS_USER_SCOPE,)
            raise FeishuUserScopeError(
                f"Feishu user token for {target.open_id} is missing required scope "
                f"`{REQUIRED_SEND_AS_USER_SCOPE}` and no bot fallback is configured for "
                f"`{target.app_id}`. "
                f"Run `{preflight.auth_login_command}` after enabling `{REQUIRED_SEND_AS_USER_SCOPE}` "
                f"on app `{target.app_id}` or configure app secret for `{target.app_id}`.",
                missing_scopes=missing,
                auth_login_command=preflight.auth_login_command,
            )
        return target

    async def arequire_send_as_user_scope(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
        allow_bot_fallback: bool = False,
    ) -> FeishuUserTokenRecord:
        if record is _RECORD_OMITTED:
            target = await self.aresolve_record(auto_import_lark_cli=auto_import_lark_cli)
        else:
            target = record if isinstance(record, FeishuUserTokenRecord) else None
        return self.require_send_as_user_scope(target, allow_bot_fallback=allow_bot_fallback)

    async def refresh_token(self, record: FeishuUserTokenRecord) -> FeishuUserTokenRecord:
        async with self._store.async_refresh_lock(app_id=record.app_id, open_id=record.open_id):
            async with self._store.async_file_lock(
                app_id=record.app_id, open_id=record.open_id, create_cli_lock_dir=False
            ):
                now_ts = float(self._store._now())
                # Re-read store + lark-cli keychain under the per-account lock in case
                # another consumer already rotated the single-use refresh token.
                latest = self._store.resolve_token(
                    open_id=record.open_id,
                    app_id=record.app_id,
                    auto_import_lark_cli=True,
                )
                active = latest if latest is not None else record
                if (
                    active.access_token != record.access_token
                    or active.generation > record.generation
                ) and not active.is_expired(now=now_ts, skew_seconds=300.0):
                    return active

                if not active.refresh_token:
                    raise FeishuBotConfigurationError(
                        f"Feishu user token for {active.open_id} expired and has no refresh_token"
                    )

            refreshed = await self._token_refresher(
                active.refresh_token,
                app_id=active.app_id,
                app_secret=active.app_secret or None,
            )
            if not isinstance(refreshed, Mapping) or not refreshed.get("access_token"):
                raise FeishuBotError("Feishu user access token refresh failed")
            return await self._store.asave_refreshed_token(active, refreshed, now=now_ts)

    async def get_user_token(
        self, *, force_refresh: bool = False
    ) -> tuple[str, FeishuUserTokenRecord]:
        record = await self.aresolve_record()
        if record is None:
            preflight = evaluate_scope_preflight(None)
            raise FeishuUserScopeError(
                "No Feishu user access token is configured. "
                f"Run `{preflight.auth_login_command}`.",
                missing_scopes=preflight.missing_required_scopes,
                auth_login_command=preflight.auth_login_command,
            )
        now_ts = float(self._store._now())
        if (
            force_refresh or record.is_expired(now=now_ts, skew_seconds=300.0)
        ) and record.refresh_token:
            try:
                record = await self.refresh_token(record)
            except Exception:
                if force_refresh:
                    raise
                logger.warning("Proactive Feishu user token refresh failed; trying current token")
        return record.access_token, record

    async def _with_user_token_retry(self, op: Callable[[str], Awaitable[_T]]) -> _T:
        token, record = await self.get_user_token(force_refresh=False)
        try:
            return await op(token)
        except FeishuBotRemoteError as exc:
            if exc.code in _EXPIRED_TOKEN_CODES and record.refresh_token:
                refreshed_token, _ = await self.get_user_token(force_refresh=True)
                return await op(refreshed_token)
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and record.refresh_token:
                refreshed_token, _ = await self.get_user_token(force_refresh=True)
                return await op(refreshed_token)
            raise

    async def _ensure_bot_in_chat(self, chat_id: str, bot_app_id: str) -> bool:
        """Invite ``bot_app_id`` into ``chat_id`` using the user's ``im:chat.members:write_only`` scope."""
        clean_chat = (chat_id or "").strip()
        clean_bot = (bot_app_id or "").strip()
        if not clean_chat or not clean_bot:
            return False

        async def _call(token: str) -> bool:
            response = await self._request(
                "POST",
                f"/open-apis/im/v1/chats/{quote(clean_chat, safe='')}/members",
                params={"member_id_type": "app_id", "succeed_type": 0},
                headers={"Authorization": f"Bearer {token}"},
                json={"id_list": [clean_bot]},
            )
            try:
                payload = response.json()
            except ValueError:
                return False
            if (
                response.is_error
                and isinstance(payload, dict)
                and payload.get("code") in _EXPIRED_TOKEN_CODES
            ):
                raise FeishuBotClient._remote_error(response, payload, "the user chat member add")
            return bool(isinstance(payload, Mapping) and payload.get("code") == 0)

        try:
            return await self._with_user_token_retry(_call)
        except Exception:
            logger.debug(
                "Could not auto-add bot %s to chat %s via user token",
                clean_bot,
                clean_chat,
                exc_info=True,
            )
            return False

    @staticmethod
    def _require_reply_receipt_identity(
        receipt: FeishuReplyReceipt,
    ) -> FeishuReplyReceipt:
        if not isinstance(receipt.message_id, str) or not receipt.message_id.strip():
            raise FeishuDeliveryUncertainError(
                "Feishu reply message identity is unavailable; delivery may have occurred"
            )
        return receipt

    async def _send_via_bot_fallback(
        self,
        record: FeishuUserTokenRecord,
        chat_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        uuid: str | None = None,
        auto_invite_bot_to_chat: bool = False,
    ) -> dict[str, Any]:
        bot_configs = self._resolve_fallback_bot_configs(record)
        if not bot_configs:
            preflight = evaluate_scope_preflight(record, fallback_bot_app_id="")
            raise FeishuUserScopeError(
                f"Feishu user token for {record.open_id} is missing required scope "
                f"`{REQUIRED_SEND_AS_USER_SCOPE}` and no bot fallback is configured for "
                f"`{record.app_id}`. "
                f"Run `{preflight.auth_login_command}` after enabling `{REQUIRED_SEND_AS_USER_SCOPE}` "
                f"on app `{record.app_id}`.",
                missing_scopes=(REQUIRED_SEND_AS_USER_SCOPE,),
                auth_login_command=preflight.auth_login_command,
            )
        bot_app_id, bot_secret = bot_configs[0]
        bot_client = self._get_fallback_bot_client(bot_app_id, bot_secret)
        try:
            msg_id = await bot_client.send_message_with_identity(
                chat_id, msg_type, content, uuid=uuid
            )
            return {
                "message_id": msg_id,
                "chat_id": chat_id,
                "msg_type": msg_type,
                "send_mode": "bot_fallback",
                "bot_app_id": bot_app_id,
            }
        except FeishuBotRemoteError as exc:
            if (
                auto_invite_bot_to_chat
                and exc.code in _BOT_NOT_IN_CHAT_CODES
                and chat_id
                and await self._ensure_bot_in_chat(chat_id, bot_app_id)
            ):
                msg_id = await bot_client.send_message_with_identity(
                    chat_id, msg_type, content, uuid=uuid
                )
                return {
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "msg_type": msg_type,
                    "send_mode": "bot_fallback",
                    "bot_app_id": bot_app_id,
                }
            raise

    async def _reply_via_bot_fallback(
        self,
        record: FeishuUserTokenRecord,
        message_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        chat_id: str | None = None,
        reply_in_thread: bool = False,
        uuid: str | None = None,
        auto_invite_bot_to_chat: bool = False,
    ) -> FeishuReplyReceipt:
        bot_configs = self._resolve_fallback_bot_configs(record)
        if not bot_configs:
            preflight = evaluate_scope_preflight(record, fallback_bot_app_id="")
            raise FeishuUserScopeError(
                f"Feishu user token for {record.open_id} is missing required scope "
                f"`{REQUIRED_SEND_AS_USER_SCOPE}` and no bot fallback is configured for "
                f"`{record.app_id}`. "
                f"Run `{preflight.auth_login_command}` after enabling `{REQUIRED_SEND_AS_USER_SCOPE}` "
                f"on app `{record.app_id}`.",
                missing_scopes=(REQUIRED_SEND_AS_USER_SCOPE,),
                auth_login_command=preflight.auth_login_command,
            )
        bot_app_id, bot_secret = bot_configs[0]
        bot_client = self._get_fallback_bot_client(bot_app_id, bot_secret)
        try:
            receipt = await bot_client._reply_message_payload(
                message_id,
                msg_type,
                content,
                reply_in_thread=reply_in_thread,
                uuid=uuid,
            )
            return self._require_reply_receipt_identity(receipt)
        except FeishuBotRemoteError as exc:
            if auto_invite_bot_to_chat and exc.code in _BOT_NOT_IN_CHAT_CODES:
                resolved_chat_id = (chat_id or "").strip()
                if not resolved_chat_id:
                    try:
                        msg_info = await self.get_message(message_id)
                        if msg_info is not None and msg_info.chat_id:
                            resolved_chat_id = msg_info.chat_id
                    except Exception:
                        pass
                if resolved_chat_id and await self._ensure_bot_in_chat(
                    resolved_chat_id, bot_app_id
                ):
                    receipt = await bot_client._reply_message_payload(
                        message_id,
                        msg_type,
                        content,
                        reply_in_thread=reply_in_thread,
                        uuid=uuid,
                    )
                    return self._require_reply_receipt_identity(receipt)
            raise

    async def send_as_user(
        self,
        chat_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        uuid: str | None = None,
        dry_run: bool = False,
        allow_bot_fallback: bool = True,
        auto_invite_bot_to_chat: bool = False,
    ) -> dict[str, Any]:
        """Send a message to ``chat_id`` as the authenticated user (or bot fallback when configured)."""
        if dry_run:
            preflight = await self.apreflight_scopes()
            return {
                "dry_run": True,
                "action": "send_as_user",
                "chat_id": chat_id,
                "msg_type": msg_type,
                "content": dict(content),
                "uuid": uuid,
                "can_send": preflight.can_send,
                "can_send_as_user": preflight.can_send_as_user,
                "send_mode": preflight.send_mode,
                "missing_required_scopes": list(preflight.missing_required_scopes),
            }
        record = await self.arequire_send_as_user_scope(allow_bot_fallback=allow_bot_fallback)
        if REQUIRED_SEND_AS_USER_SCOPE not in record.scopes_set():
            return await self._send_via_bot_fallback(
                record,
                chat_id,
                msg_type,
                content,
                uuid=uuid,
                auto_invite_bot_to_chat=auto_invite_bot_to_chat,
            )

        async def _call(token: str) -> dict[str, Any]:
            message_id = await self._bot_client.send_message_with_identity(
                chat_id,
                msg_type,
                content,
                uuid=uuid,
                access_token=token,
            )
            return {
                "message_id": message_id,
                "chat_id": chat_id,
                "msg_type": msg_type,
                "send_mode": "user",
            }

        try:
            return await self._with_user_token_retry(_call)
        except FeishuBotRemoteError as exc:
            if allow_bot_fallback and exc.code in _MISSING_SEND_AS_USER_CODES:
                return await self._send_via_bot_fallback(
                    record,
                    chat_id,
                    msg_type,
                    content,
                    uuid=uuid,
                    auto_invite_bot_to_chat=auto_invite_bot_to_chat,
                )
            raise

    async def reply_as_user(
        self,
        message_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        chat_id: str | None = None,
        reply_in_thread: bool = False,
        uuid: str | None = None,
        dry_run: bool = False,
        allow_bot_fallback: bool = True,
        auto_invite_bot_to_chat: bool = False,
    ) -> FeishuReplyReceipt:
        """Reply to ``message_id`` as the authenticated user (or bot fallback when configured)."""
        if dry_run:
            return FeishuReplyReceipt(
                message_id=f"dry-run:{uuid or message_id}",
                thread_id="dry-run-thread" if reply_in_thread else None,
            )
        record = await self.arequire_send_as_user_scope(allow_bot_fallback=allow_bot_fallback)
        if REQUIRED_SEND_AS_USER_SCOPE not in record.scopes_set():
            return await self._reply_via_bot_fallback(
                record,
                message_id,
                msg_type,
                content,
                chat_id=chat_id,
                reply_in_thread=reply_in_thread,
                uuid=uuid,
                auto_invite_bot_to_chat=auto_invite_bot_to_chat,
            )

        async def _call(token: str) -> FeishuReplyReceipt:
            receipt = await self._bot_client._reply_message_payload(
                message_id,
                msg_type,
                content,
                access_token=token,
                reply_in_thread=reply_in_thread,
                uuid=uuid,
            )
            return self._require_reply_receipt_identity(receipt)

        try:
            return await self._with_user_token_retry(_call)
        except FeishuBotRemoteError as exc:
            if allow_bot_fallback and exc.code in _MISSING_SEND_AS_USER_CODES:
                return await self._reply_via_bot_fallback(
                    record,
                    message_id,
                    msg_type,
                    content,
                    chat_id=chat_id,
                    reply_in_thread=reply_in_thread,
                    uuid=uuid,
                    auto_invite_bot_to_chat=auto_invite_bot_to_chat,
                )
            raise

    async def add_reaction_as_user(
        self,
        message_id: str,
        emoji_type: str,
        *,
        dry_run: bool = False,
    ) -> str:
        """Add an emoji reaction (e.g. ``OnIt`` or ``DONE``) as the user."""
        if dry_run:
            return f"dry-run-reaction:{emoji_type}"

        async def _call(token: str) -> str:
            return await self._bot_client.add_reaction(
                message_id,
                emoji_type,
                access_token=token,
            )

        return await self._with_user_token_retry(_call)

    async def delete_reaction_as_user(
        self,
        message_id: str,
        reaction_id: str,
        *,
        dry_run: bool = False,
    ) -> None:
        """Delete a reaction previously added by the user."""
        if dry_run:
            return

        async def _call(token: str) -> None:
            await self._bot_client.delete_reaction(
                message_id,
                reaction_id,
                access_token=token,
            )

        await self._with_user_token_retry(_call)

    async def list_chats(
        self,
        *,
        sort_type: str = "ByActiveTimeDesc",
        page_size: int = 20,
    ) -> list[FeishuChatInfo]:
        """List recent chats for the authenticated user."""

        async def _call(token: str) -> list[FeishuChatInfo]:
            response = await self._request(
                "GET",
                "/open-apis/im/v1/chats",
                params={
                    "sort_type": sort_type,
                    "page_size": max(1, min(page_size, 100)),
                    "user_id_type": "open_id",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json()
            if (
                response.is_error
                and isinstance(payload, dict)
                and payload.get("code") not in (None, 0)
            ):
                raise FeishuBotClient._remote_error(response, payload, "the user chat list")
            response.raise_for_status()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                if isinstance(payload, dict) and payload.get("code") is not None:
                    raise FeishuBotClient._remote_error(response, payload, "the user chat list")
                raise FeishuBotError("Feishu refused the user chat list")
            data = payload.get("data")
            items = data.get("items") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                return []
            chats: list[FeishuChatInfo] = []
            for item in items:
                info = _chat_info_from_payload(item) if isinstance(item, Mapping) else None
                if info is not None:
                    chats.append(info)
            return chats

        return await self._with_user_token_retry(_call)

    async def list_messages_page(
        self,
        *,
        container_id_type: str = "chat",
        container_id: str,
        sort_type: str = "ByCreateTimeDesc",
        page_size: int = 50,
        page_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> FeishuMessagePage:
        """Fetch one page of messages in a ``chat`` or ``thread`` container."""
        if container_id_type not in {"chat", "thread"}:
            raise ValueError("container_id_type must be 'chat' or 'thread'")

        async def _call(token: str) -> FeishuMessagePage:
            params: dict[str, Any] = {
                "container_id_type": container_id_type,
                "container_id": container_id,
                "sort_type": sort_type,
                "page_size": max(1, min(page_size, 50)),
            }
            if page_token:
                params["page_token"] = page_token
            if start_time:
                params["start_time"] = start_time
            if end_time:
                params["end_time"] = end_time
            response = await self._request(
                "GET",
                "/open-apis/im/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json()
            if (
                response.is_error
                and isinstance(payload, dict)
                and payload.get("code") not in (None, 0)
            ):
                raise FeishuBotClient._remote_error(response, payload, "the user message list")
            response.raise_for_status()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                if isinstance(payload, dict) and payload.get("code") is not None:
                    raise FeishuBotClient._remote_error(response, payload, "the user message list")
                raise FeishuBotError("Feishu refused the user message list")
            data = payload.get("data")
            items = data.get("items") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                return FeishuMessagePage(items=())
            messages: list[FeishuUserMessage] = []
            default_chat_id = container_id if container_id_type == "chat" else ""
            for raw_item in items:
                if not isinstance(raw_item, Mapping):
                    continue
                parsed = _parse_user_message(raw_item, default_chat_id=default_chat_id)
                if parsed is not None:
                    messages.append(parsed)
            has_more = bool(data.get("has_more")) if isinstance(data, Mapping) else False
            next_token = (
                str(data.get("page_token") or "").strip() if isinstance(data, Mapping) else ""
            )
            return FeishuMessagePage(
                items=tuple(messages),
                has_more=has_more,
                page_token=next_token,
            )

        return await self._with_user_token_retry(_call)

    async def list_messages(
        self,
        *,
        container_id_type: str = "chat",
        container_id: str,
        sort_type: str = "ByCreateTimeDesc",
        page_size: int = 50,
        page_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        min_create_time_ms: int | None = None,
        max_pages: int = 1,
    ) -> list[FeishuUserMessage]:
        """List messages in a ``chat`` or ``thread`` container using the user token.

        When ``max_pages > 1`` or ``min_create_time_ms`` is supplied, follows
        ``page_token`` across pages until reaching ``min_create_time_ms`` or ``max_pages``.
        """
        effective_max_pages = max(1, max_pages)
        if min_create_time_ms is not None and max_pages == 1:
            effective_max_pages = 25

        collected: list[FeishuUserMessage] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        cursor = page_token

        for _ in range(effective_max_pages):
            page = await self.list_messages_page(
                container_id_type=container_id_type,
                container_id=container_id,
                sort_type=sort_type,
                page_size=page_size,
                page_token=cursor,
                start_time=start_time,
                end_time=end_time,
            )
            for item in page.items:
                if item.message_id not in seen_ids:
                    seen_ids.add(item.message_id)
                    collected.append(item)
            if not page.items or not page.has_more or not page.page_token:
                break
            if page.page_token in seen_tokens:
                break
            if (
                min_create_time_ms is not None
                and sort_type == "ByCreateTimeDesc"
                and min(m.create_time_ms for m in page.items) <= min_create_time_ms
            ):
                break
            seen_tokens.add(page.page_token)
            cursor = page.page_token

        return collected

    async def get_message(self, message_id: str) -> FeishuUserMessage | None:
        """Fetch one message by ``message_id`` using the user token."""

        async def _call(token: str) -> FeishuUserMessage | None:
            response = await self._request(
                "GET",
                f"/open-apis/im/v1/messages/{quote(message_id, safe='')}",
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json()
            if (
                response.is_error
                and isinstance(payload, dict)
                and payload.get("code") not in (None, 0)
            ):
                raise FeishuBotClient._remote_error(response, payload, "the user message lookup")
            response.raise_for_status()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return None
            data = payload.get("data")
            items = data.get("items") if isinstance(data, Mapping) else None
            if isinstance(items, list) and items and isinstance(items[0], Mapping):
                return _parse_user_message(items[0])
            if isinstance(data, Mapping):
                return _parse_user_message(data)
            return None

        return await self._with_user_token_retry(_call)

    async def search_messages(
        self,
        query: str,
        *,
        page_size: int = 20,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[str]:
        """Search messages via ``/open-apis/search/v2/message`` and return message IDs."""

        async def _call(token: str) -> list[str]:
            body: dict[str, Any] = {"query": query}
            if start_time:
                body["start_time"] = start_time
            if end_time:
                body["end_time"] = end_time
            response = await self._request(
                "POST",
                "/open-apis/search/v2/message",
                params={
                    "page_size": max(1, min(page_size, 50)),
                    "user_id_type": "open_id",
                },
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            payload = response.json()
            if (
                response.is_error
                and isinstance(payload, dict)
                and payload.get("code") not in (None, 0)
            ):
                raise FeishuBotClient._remote_error(response, payload, "the user message search")
            response.raise_for_status()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return []
            data = payload.get("data")
            items = data.get("items") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                return []
            return [str(item).strip() for item in items if isinstance(item, str) and item.strip()]

        return await self._with_user_token_retry(_call)
