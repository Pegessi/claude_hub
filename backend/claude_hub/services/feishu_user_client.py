"""Feishu user-identity OpenAPI client for the personal ``/ai`` assistant workflow.

Reuses ``FeishuBotClient``, ``create_feishu_http_client``, ``FeishuBotRemoteError``,
and message parsing helpers from ``feishu_bot.py`` so there is one HTTP and
reaction/reply implementation across Bot and User identities.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence, TypeVar
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
    create_feishu_http_client,
)
from claude_hub.services.feishu_user_client_models import (
    RECOMMENDED_AI_COMMAND_SCOPES as RECOMMENDED_AI_COMMAND_SCOPES,
)
from claude_hub.services.feishu_user_client_models import (
    REQUIRED_IM_MESSAGE_SCOPE as REQUIRED_IM_MESSAGE_SCOPE,
)
from claude_hub.services.feishu_user_client_models import (
    REQUIRED_SEND_AS_USER_SCOPE as REQUIRED_SEND_AS_USER_SCOPE,
)
from claude_hub.services.feishu_user_client_models import FeishuMessagePage as FeishuMessagePage
from claude_hub.services.feishu_user_client_models import (
    FeishuMessageSearchPage as FeishuMessageSearchPage,
)
from claude_hub.services.feishu_user_client_models import (
    FeishuScopePreflightResult as FeishuScopePreflightResult,
)
from claude_hub.services.feishu_user_client_models import (
    FeishuUserScopeError as FeishuUserScopeError,
)
from claude_hub.services.feishu_user_client_models import _extract_item_text as _extract_item_text
from claude_hub.services.feishu_user_client_models import _parse_user_message as _parse_user_message
from claude_hub.services.feishu_user_client_models import (
    build_auth_login_command as build_auth_login_command,
)
from claude_hub.services.feishu_user_client_models import (
    evaluate_scope_preflight as evaluate_scope_preflight,
)
from claude_hub.services.feishu_user_message import FeishuUserMessage
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenRecord,
    FeishuUserTokenStore,
    get_feishu_user_token_store,
)

_T = TypeVar("_T")
logger = logging.getLogger(__name__)

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
        web_session_store: Any | None = None,
        send_mode: str = "auto",
        web_bot_chat_ids: Mapping[str, str] | None = None,
    ) -> None:
        self._store = token_store or get_feishu_user_token_store()
        self._open_id = (open_id or "").strip() or None
        self._app_id = (app_id or "").strip() or None
        self._api_base_url = api_base_url.rstrip("/")
        self._client = client
        self._token_refresher = token_refresher or refresh_user_access_token_bundle
        self._web_session_store = web_session_store
        self._send_mode = (send_mode or "auto").strip().lower()
        self._web_transport: Any | None = None
        self._web_bot_chat_ids = dict(web_bot_chat_ids or {})
        self._bot_client = FeishuBotClient(
            FeishuBotConfig(
                app_id=self._app_id or "user-client",
                app_secret="unused-for-user-token",
                api_base_url=self._api_base_url,
            ),
            client=client,
        )
        self._fallback_bot_clients: dict[str, FeishuBotClient] = {}

    def _raw_web_session(self) -> Any | None:
        if self._send_mode == "openapi" or self._web_session_store is None:
            return None
        try:
            sess = self._web_session_store.load(auto_import_larkx=False)
            if sess is not None and sess.is_valid_structure():
                return sess
        except Exception:
            pass
        return None

    def _active_web_session(self, record: FeishuUserTokenRecord | None = None) -> Any | None:
        sess = self._raw_web_session()
        if sess is None:
            return None
        from claude_hub.services.feishu_web_session.store import (
            is_web_session_authorized_for_owner,
        )

        auth_ok, _ = is_web_session_authorized_for_owner(
            sess,
            self._open_id,
            token_record=record,
            api_base_url=self._api_base_url,
        )
        return sess if auth_ok else None

    def _get_web_transport(self) -> Any | None:
        sess = self._active_web_session()
        if sess is None:
            return None
        if self._web_transport is None:
            from claude_hub.services.feishu_web_session import (
                FeishuWebClient,
                FeishuWebUserTransport,
            )

            web_client = FeishuWebClient(
                self._web_session_store,
                http_client=self._client,
            )
            self._web_transport = FeishuWebUserTransport(
                web_client,
                owner_open_id=self._open_id or "",
                api_base_url=self._api_base_url,
            )
        return self._web_transport

    def _should_use_web_session_for_send(self, record: FeishuUserTokenRecord | None) -> bool:
        if self._send_mode == "openapi" or self._active_web_session(record) is None:
            return False
        if self._send_mode == "web_session" or record is None or record.source == "web_session":
            return True
        return REQUIRED_SEND_AS_USER_SCOPE not in record.scopes_set()

    def should_use_web_session(self, record: FeishuUserTokenRecord | None = None) -> bool:
        rec = record or self._store.resolve_token(open_id=self._open_id, app_id=self._app_id)
        return self._should_use_web_session_for_send(rec)

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

    def _synthetic_web_record(self, sess: Any) -> FeishuUserTokenRecord:
        eff_open = (sess.open_id or sess.user_id or self._open_id or "web_user").strip()
        return FeishuUserTokenRecord(
            open_id=eff_open,
            app_id=self._app_id or "web_session",
            access_token="web-session",
            refresh_token="",
            expires_at=0.0,
            scope=" ".join(RECOMMENDED_AI_COMMAND_SCOPES),
            user_name=sess.name or "",
            source="web_session",
            updated_at=sess.saved_at,
        )

    def resolve_record(self, *, auto_import_lark_cli: bool = True) -> FeishuUserTokenRecord | None:
        rec = self._store.resolve_token(
            open_id=self._open_id,
            app_id=self._app_id,
            auto_import_lark_cli=auto_import_lark_cli,
        )
        if rec is not None or self._send_mode == "openapi":
            return rec
        sess = self._active_web_session()
        return self._synthetic_web_record(sess) if sess is not None else None

    async def aresolve_record(
        self, *, auto_import_lark_cli: bool = True
    ) -> FeishuUserTokenRecord | None:
        if hasattr(self._store, "aresolve_token"):
            rec = await self._store.aresolve_token(
                open_id=self._open_id,
                app_id=self._app_id,
                auto_import_lark_cli=auto_import_lark_cli,
            )
        else:
            async with self._store.async_file_lock(
                app_id=self._app_id or "", open_id=self._open_id or ""
            ):
                rec = self._store.resolve_token(
                    open_id=self._open_id,
                    app_id=self._app_id,
                    auto_import_lark_cli=auto_import_lark_cli,
                )
        if rec is not None or self._send_mode == "openapi":
            return rec
        sess = self._active_web_session()
        return self._synthetic_web_record(sess) if sess is not None else None

    def _eval_preflight(self, target: FeishuUserTokenRecord | None) -> FeishuScopePreflightResult:
        fb_configs = self._resolve_fallback_bot_configs(target)
        return evaluate_scope_preflight(
            target,
            fallback_bot_app_id=fb_configs[0][0] if fb_configs else "",
            web_session=self._raw_web_session(),
            configured_send_mode=self._send_mode,
            configured_owner_open_id=self._open_id,
            api_base_url=self._api_base_url,
        )

    def preflight_scopes(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
    ) -> FeishuScopePreflightResult:
        target = (
            self.resolve_record(auto_import_lark_cli=auto_import_lark_cli)
            if record is _RECORD_OMITTED
            else (record if isinstance(record, FeishuUserTokenRecord) else None)
        )
        return self._eval_preflight(target)

    async def apreflight_scopes(
        self,
        record: FeishuUserTokenRecord | None | object = _RECORD_OMITTED,
        *,
        auto_import_lark_cli: bool = True,
    ) -> FeishuScopePreflightResult:
        target = (
            await self.aresolve_record(auto_import_lark_cli=auto_import_lark_cli)
            if record is _RECORD_OMITTED
            else (record if isinstance(record, FeishuUserTokenRecord) else None)
        )
        return self._eval_preflight(target)

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
        preflight = evaluate_scope_preflight(
            target,
            fallback_bot_app_id=fb_app_id,
            web_session=self._active_web_session(),
            configured_send_mode=self._send_mode,
        )
        if self._send_mode == "web_session" and not preflight.has_web_session:
            raise FeishuUserScopeError(
                "Feishu web session is required when send_mode='web_session'. "
                "Run `claude-hub feishu ai-command web-login`.",
                missing_scopes=preflight.missing_required_scopes,
                auth_login_command="claude-hub feishu ai-command web-login",
            )
        if target is None:
            raise FeishuUserScopeError(
                "No Feishu user access token is available. "
                f"Run `{preflight.auth_login_command}` or sign in via Hub Feishu OAuth.",
                missing_scopes=preflight.missing_required_scopes,
                auth_login_command=preflight.auth_login_command,
            )
        can_proceed = preflight.can_send if allow_bot_fallback else preflight.can_send_as_user
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

    def _resolve_chat_bound_pool_bot_configs(
        self, chat_id: str, *, exclude_app_id: str = ""
    ) -> list[tuple[str, str]]:
        """Return ``(app_id, app_secret)`` for enabled non-tombstoned pool bots bound to ``chat_id``."""
        clean_chat = (chat_id or "").strip()
        if not clean_chat:
            return []
        try:
            from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore

            pool = FeishuBotPoolStore()
            snapshot = pool.snapshot()
            with pool._lock:
                raw_bots = pool._load().get("bots", {})
            revoked_apps = {
                str(raw.get("app_id") or "").strip()
                for raw in raw_bots.values()
                if isinstance(raw, Mapping)
                and (raw.get("revoked_at") is not None or not raw.get("enabled"))
            }
        except Exception:
            return []

        matched: list[tuple[str, str]] = []
        seen_apps: set[str] = {exclude_app_id.strip()} if exclude_app_id else set()
        for bot in snapshot.bots:
            app_id = (bot.app_id or "").strip()
            if (
                not app_id
                or app_id in seen_apps
                or app_id in revoked_apps
                or not bot.enabled
                or bot.config is None
                or not bot.config.app_secret.strip()
            ):
                continue
            if any((b.chat_id or "").strip() == clean_chat for b in bot.bindings):
                seen_apps.add(app_id)
                matched.append((app_id, bot.config.app_secret.strip()))
        return matched

    async def _send_via_bot_fallback(
        self,
        record: FeishuUserTokenRecord,
        chat_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        uuid: str | None = None,
        auto_invite_bot_to_chat: bool = False,
        allow_bound_or_dm_fallback_on_not_in_chat: bool = False,
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
            if exc.code in _BOT_NOT_IN_CHAT_CODES:
                if (
                    auto_invite_bot_to_chat
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
                if allow_bound_or_dm_fallback_on_not_in_chat:
                    for (
                        bound_app_id,
                        bound_secret,
                    ) in self._resolve_chat_bound_pool_bot_configs(
                        chat_id, exclude_app_id=bot_app_id
                    ):
                        bound_client = self._get_fallback_bot_client(bound_app_id, bound_secret)
                        try:
                            msg_id = await bound_client.send_message_with_identity(
                                chat_id, msg_type, content, uuid=uuid
                            )
                            return {
                                "message_id": msg_id,
                                "chat_id": chat_id,
                                "msg_type": msg_type,
                                "send_mode": "bot_fallback",
                                "bot_app_id": bound_app_id,
                            }
                        except FeishuBotRemoteError as bound_exc:
                            if bound_exc.code in _BOT_NOT_IN_CHAT_CODES:
                                continue
                            raise
                    owner_open_id = record.open_id.strip()
                    if owner_open_id.startswith("ou_") and record.app_id.strip() == bot_app_id:
                        dm_msg_id = await bot_client.send_message_with_identity(
                            owner_open_id,
                            msg_type,
                            content,
                            uuid=uuid,
                            receive_id_type="open_id",
                        )
                        return {
                            "message_id": dm_msg_id,
                            "chat_id": chat_id,
                            "msg_type": msg_type,
                            "send_mode": "bot_fallback_dm",
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
        allow_bound_or_dm_fallback_on_not_in_chat: bool = False,
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
            if exc.code in _BOT_NOT_IN_CHAT_CODES:
                resolved_chat_id = (chat_id or "").strip()
                if not resolved_chat_id and (
                    auto_invite_bot_to_chat or allow_bound_or_dm_fallback_on_not_in_chat
                ):
                    try:
                        msg_info = await self.get_message(message_id)
                        if msg_info is not None and msg_info.chat_id:
                            resolved_chat_id = msg_info.chat_id
                    except Exception:
                        pass
                if (
                    auto_invite_bot_to_chat
                    and resolved_chat_id
                    and await self._ensure_bot_in_chat(resolved_chat_id, bot_app_id)
                ):
                    receipt = await bot_client._reply_message_payload(
                        message_id,
                        msg_type,
                        content,
                        reply_in_thread=reply_in_thread,
                        uuid=uuid,
                    )
                    return self._require_reply_receipt_identity(receipt)
                if allow_bound_or_dm_fallback_on_not_in_chat:
                    if resolved_chat_id:
                        for (
                            bound_app_id,
                            bound_secret,
                        ) in self._resolve_chat_bound_pool_bot_configs(
                            resolved_chat_id, exclude_app_id=bot_app_id
                        ):
                            bound_client = self._get_fallback_bot_client(bound_app_id, bound_secret)
                            try:
                                receipt = await bound_client._reply_message_payload(
                                    message_id,
                                    msg_type,
                                    content,
                                    reply_in_thread=reply_in_thread,
                                    uuid=uuid,
                                )
                                return self._require_reply_receipt_identity(receipt)
                            except FeishuBotRemoteError as bound_exc:
                                if bound_exc.code in _BOT_NOT_IN_CHAT_CODES:
                                    continue
                                raise
                    owner_open_id = record.open_id.strip()
                    if owner_open_id.startswith("ou_") and record.app_id.strip() == bot_app_id:
                        dm_msg_id = await bot_client.send_message_with_identity(
                            owner_open_id,
                            msg_type,
                            content,
                            uuid=uuid,
                            receive_id_type="open_id",
                        )
                        return FeishuReplyReceipt(message_id=dm_msg_id, thread_id=None)
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
        allow_bound_or_dm_fallback_on_not_in_chat: bool = False,
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
        if self._should_use_web_session_for_send(record):
            wt = self._get_web_transport()
            if wt is not None:
                sent_res: dict[str, Any] = await wt.send_message(
                    chat_id, msg_type, content, uuid=uuid
                )
                return sent_res
        if REQUIRED_SEND_AS_USER_SCOPE not in record.scopes_set():
            return await self._send_via_bot_fallback(
                record,
                chat_id,
                msg_type,
                content,
                uuid=uuid,
                auto_invite_bot_to_chat=auto_invite_bot_to_chat,
                allow_bound_or_dm_fallback_on_not_in_chat=allow_bound_or_dm_fallback_on_not_in_chat,
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
                    allow_bound_or_dm_fallback_on_not_in_chat=allow_bound_or_dm_fallback_on_not_in_chat,
                )
            raise

    async def _reply_after_web_rejection(
        self,
        record: FeishuUserTokenRecord,
        message_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        chat_id: str | None,
        reply_in_thread: bool,
        uuid: str | None,
        allow_bound_or_dm: bool,
    ) -> FeishuReplyReceipt:
        wt = self._get_web_transport()
        open_chat_id = chat_id if chat_id and chat_id.startswith("oc_") else None
        if allow_bound_or_dm and chat_id and wt is not None and not open_chat_id:
            try:
                open_chat_id = await wt.resolve_open_chat_id(chat_id)
            except FeishuBotError:
                pass
        mapped_bot: FeishuBotClient | None = None
        if allow_bound_or_dm and not open_chat_id and chat_id:
            mapped = self._web_bot_chat_ids.get(chat_id, "")
            if mapped.startswith("oc_"):
                # Only use operator-declared equivalence, confirmed visible to
                # a configured Bot. Names and numeric IDs cannot prove identity.
                candidates = self._resolve_fallback_bot_configs(record)
                candidates += self._resolve_chat_bound_pool_bot_configs(mapped)
                for app_id, secret in dict(candidates).items():
                    bot = self._get_fallback_bot_client(app_id, secret)
                    try:
                        visible = await bot.list_chats()
                    except (FeishuBotError, httpx.HTTPError):
                        continue
                    if any(chat.chat_id == mapped for chat in visible):
                        mapped_bot = bot
                        open_chat_id = mapped
                        break
        alias = wt.get_message_alias(message_id) if wt is not None else None
        open_message_id = message_id if message_id.startswith("om_") else alias
        if open_message_id and open_message_id.startswith("om_"):
            if mapped_bot is not None:
                return self._require_reply_receipt_identity(
                    await mapped_bot._reply_message_payload(
                        open_message_id,
                        msg_type,
                        content,
                        reply_in_thread=reply_in_thread,
                        uuid=uuid,
                    )
                )
            return await self._reply_via_bot_fallback(
                record,
                open_message_id,
                msg_type,
                content,
                chat_id=open_chat_id,
                reply_in_thread=reply_in_thread,
                uuid=uuid,
                allow_bound_or_dm_fallback_on_not_in_chat=allow_bound_or_dm,
            )
        if not allow_bound_or_dm:
            raise FeishuBotConfigurationError(
                "Web reply rejected; no OpenAPI message alias for Bot reply"
            )
        # Numeric Web IDs are not OpenAPI IDs. An explicit chat mapping permits
        # the existing configured/bound Bot path; otherwise only the owner DM is safe.
        if open_chat_id:
            if mapped_bot is not None:
                sent_id = await mapped_bot.send_message_with_identity(
                    open_chat_id, msg_type, content, uuid=uuid
                )
                return self._require_reply_receipt_identity(
                    FeishuReplyReceipt(message_id=sent_id, thread_id=None)
                )
            sent = await self._send_via_bot_fallback(
                record,
                open_chat_id,
                msg_type,
                content,
                uuid=uuid,
                allow_bound_or_dm_fallback_on_not_in_chat=True,
            )
            return self._require_reply_receipt_identity(
                FeishuReplyReceipt(message_id=sent["message_id"], thread_id=None)
            )
        configs = self._resolve_fallback_bot_configs(record)
        if configs and record.open_id.startswith("ou_") and record.app_id == configs[0][0]:
            bot = self._get_fallback_bot_client(*configs[0])
            sent_id = await bot.send_message_with_identity(
                record.open_id, msg_type, content, uuid=uuid, receive_id_type="open_id"
            )
            return self._require_reply_receipt_identity(
                FeishuReplyReceipt(message_id=sent_id, thread_id=None)
            )
        raise FeishuBotConfigurationError(
            "Web reply rejected; no mapped chat or matching owner Bot DM"
        )

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
        allow_bound_or_dm_fallback_on_not_in_chat: bool = False,
    ) -> FeishuReplyReceipt:
        """Reply to ``message_id`` as the authenticated user (or bot fallback when configured)."""
        from claude_hub.services.feishu_web_session.client import FeishuWebGatewayError

        if dry_run:
            return FeishuReplyReceipt(
                message_id=f"dry-run:{uuid or message_id}",
                thread_id="dry-run-thread" if reply_in_thread else None,
            )
        record = await self.arequire_send_as_user_scope(allow_bot_fallback=allow_bot_fallback)
        if self._should_use_web_session_for_send(record):
            wt = self._get_web_transport()
            if wt is not None:
                try:
                    reply_res: FeishuReplyReceipt = await wt.reply_message(
                        message_id,
                        msg_type,
                        content,
                        chat_id=chat_id,
                        reply_in_thread=reply_in_thread,
                        uuid=uuid,
                    )
                    return reply_res
                except FeishuDeliveryUncertainError:
                    raise
                except FeishuWebGatewayError as exc:
                    # Only explicit request rejection can authorize another send.
                    # 5xx, timeouts and ambiguous statuses must never duplicate delivery.
                    if (
                        self._send_mode != "auto"
                        or not allow_bot_fallback
                        or exc.status_code not in {400, 404, 405, 413, 415, 422}
                    ):
                        raise
                    try:
                        return await self._reply_after_web_rejection(
                            record,
                            message_id,
                            msg_type,
                            content,
                            chat_id=chat_id,
                            reply_in_thread=reply_in_thread,
                            uuid=uuid,
                            allow_bound_or_dm=allow_bound_or_dm_fallback_on_not_in_chat,
                        )
                    except (FeishuBotConfigurationError, FeishuBotRemoteError):
                        # Keep the original sanitized gateway evidence if the
                        # configured fallback is unavailable or explicitly rejected.
                        raise exc from None
                except FeishuBotError:
                    if (
                        self._send_mode == "web_session"
                        or record.source == "web_session"
                        or not (message_id or "").startswith("om_")
                    ):
                        raise
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
                allow_bound_or_dm_fallback_on_not_in_chat=allow_bound_or_dm_fallback_on_not_in_chat,
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
                    allow_bound_or_dm_fallback_on_not_in_chat=allow_bound_or_dm_fallback_on_not_in_chat,
                )
            raise

    def _has_oauth_token(self) -> bool:
        return self._store.resolve_token(open_id=self._open_id, app_id=self._app_id) is not None

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
        wt = self._get_web_transport()
        has_oauth = self._has_oauth_token()
        if wt is not None and (
            self.should_use_web_session()
            or not (message_id or "").startswith("om_")
            or not has_oauth
        ):
            try:
                return str(await wt.add_reaction(message_id, emoji_type))
            except FeishuBotError:
                if (
                    self._send_mode == "web_session"
                    or not has_oauth
                    or not (message_id or "").startswith("om_")
                ):
                    raise

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
        wt = self._get_web_transport()
        has_oauth = self._has_oauth_token()
        if wt is not None and (
            self.should_use_web_session()
            or (reaction_id or "").startswith("web-reaction:")
            or not (message_id or "").startswith("om_")
            or not has_oauth
        ):
            try:
                await wt.delete_reaction(message_id, reaction_id)
                return
            except FeishuBotError:
                if (
                    self._send_mode == "web_session"
                    or not has_oauth
                    or not (message_id or "").startswith("om_")
                ):
                    raise

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
        types: str = "p2p,group",
    ) -> list[FeishuChatInfo]:
        """List recent chats (both P2P and group by default) for the authenticated user."""
        wt = self._get_web_transport()
        if wt is not None and self.should_use_web_session():
            chats_res: list[FeishuChatInfo] = await wt.list_chats(page_size=page_size)
            return chats_res

        async def _call(token: str) -> list[FeishuChatInfo]:
            params: dict[str, Any] = {
                "sort_type": sort_type,
                "page_size": max(1, min(page_size, 100)),
                "user_id_type": "open_id",
            }
            if types and types.strip():
                params["types"] = types.strip()
            response = await self._request(
                "GET",
                "/open-apis/im/v1/chats",
                params=params,
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
        wt = self._get_web_transport()
        if wt is not None:
            rec = self._store.resolve_token(open_id=self._open_id, app_id=self._app_id)
            if self._should_use_web_session_for_send(rec) and (
                self._send_mode == "web_session"
                or rec is None
                or not (container_id or "").startswith(("oc_", "omt_"))
            ):
                page_res: FeishuMessagePage = await wt.list_messages_page(
                    container_id_type=container_id_type,
                    container_id=container_id,
                    sort_type=sort_type,
                    page_size=page_size,
                    page_token=page_token,
                )
                return page_res

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

    async def download_message_image(self, message: FeishuUserMessage, image_key: str) -> bytes:
        """Download only under this user's verified identity; never fall back to a Bot."""
        from claude_hub.services.feishu_user_media import download_user_image

        return await download_user_image(self, message, image_key)

    async def get_message(self, message_id: str) -> FeishuUserMessage | None:
        """Fetch one message by ``message_id`` using the user token."""
        wt = self._get_web_transport()
        has_oauth = self._has_oauth_token()
        if wt is not None and (
            self.should_use_web_session()
            or not (message_id or "").startswith("om_")
            or not has_oauth
        ):
            try:
                msg_res: FeishuUserMessage | None = await wt.get_message(message_id)
                return msg_res
            except FeishuBotError:
                if (
                    self._send_mode == "web_session"
                    or not has_oauth
                    or not (message_id or "").startswith("om_")
                ):
                    raise

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

    async def search_messages_page(
        self,
        query: str,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        from_ids: Sequence[str] | None = None,
    ) -> FeishuMessageSearchPage:
        """Search messages via ``/open-apis/search/v2/message`` with pagination metadata."""
        wt = self._get_web_transport()
        if wt is not None and self.should_use_web_session():
            search_res: FeishuMessageSearchPage = await wt.search_messages_page(
                query, page_size=page_size
            )
            return search_res

        async def _call(token: str) -> FeishuMessageSearchPage:
            body: dict[str, Any] = {"query": query}
            if start_time:
                body["start_time"] = start_time
            if end_time:
                body["end_time"] = end_time
            if from_ids:
                cleaned_from = [s.strip() for s in from_ids if isinstance(s, str) and s.strip()]
                if cleaned_from:
                    body["from_ids"] = cleaned_from
            params: dict[str, Any] = {
                "page_size": max(1, min(page_size, 50)),
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            response = await self._request(
                "POST",
                "/open-apis/search/v2/message",
                params=params,
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
                return FeishuMessageSearchPage(items=(), has_more=False, page_token=None)
            data = payload.get("data")
            if not isinstance(data, Mapping):
                return FeishuMessageSearchPage(items=(), has_more=False, page_token=None)
            raw_items = data.get("items")
            items = (
                tuple(
                    str(item).strip()
                    for item in raw_items
                    if isinstance(item, str) and item.strip()
                )
                if isinstance(raw_items, list)
                else ()
            )
            next_token = data.get("page_token")
            clean_token = (
                str(next_token).strip()
                if isinstance(next_token, str) and str(next_token).strip()
                else None
            )
            return FeishuMessageSearchPage(
                items=items,
                has_more=bool(data.get("has_more")) and bool(clean_token),
                page_token=clean_token,
            )

        return await self._with_user_token_retry(_call)

    async def search_messages(
        self,
        query: str,
        *,
        page_size: int = 20,
        page_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        from_ids: Sequence[str] | None = None,
    ) -> list[str]:
        """Search messages via ``/open-apis/search/v2/message`` and return message IDs."""
        page = await self.search_messages_page(
            query,
            page_size=page_size,
            page_token=page_token,
            start_time=start_time,
            end_time=end_time,
            from_ids=from_ids,
        )
        return list(page.items)
