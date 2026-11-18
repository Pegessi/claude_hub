"""Transport adapter and login lifecycle manager bridging ``FeishuWebClient`` to Feishu ``/ai``."""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping

from claude_hub.services.feishu_bot import (
    FeishuChatInfo,
    FeishuDeliveryUncertainError,
    FeishuReplyReceipt,
)
from claude_hub.services.feishu_user_client import (
    RECOMMENDED_AI_COMMAND_SCOPES,
    REQUIRED_IM_MESSAGE_SCOPE,
    REQUIRED_SEND_AS_USER_SCOPE,
    FeishuMessagePage,
    FeishuMessageSearchPage,
    FeishuScopePreflightResult,
    FeishuUserMessage,
    build_auth_login_command,
)
from claude_hub.services.feishu_user_token_store import FeishuUserTokenRecord
from claude_hub.services.feishu_web_session.client import (
    FeishuWebClient,
    FeishuWebFrontierListener,
    FeishuWebQRLoginExpiredError,
    FeishuWebQRLoginSession,
    FeishuWebSessionError,
    PushedMessage,
    is_web_entity_id,
)
from claude_hub.services.feishu_web_session.rich import (
    CARD_CHAT,
    CHAT_P2P,
    FEED_INBOX,
    WebChat,
    WebMessage,
    WebOut,
    describe,
    extract_text_and_title_from_openapi_content,
    formatted_out,
)
from claude_hub.services.feishu_web_session.store import (
    FeishuWebSession,
    FeishuWebSessionStore,
    get_feishu_web_session_store,
    is_web_session_authorized_for_owner,
)

logger = logging.getLogger(__name__)

_WEB_REACTION_PREFIX = "web-reaction:"
_MAX_CACHED_MESSAGES = 1024


def is_in_topic(msg: WebMessage) -> bool:
    """Return True if ``msg`` was written inside a thread/topic (`agent.go:inTopic`)."""
    return bool(msg.root_id) or bool(msg.thread_id and msg.thread_id != msg.id)


def matches_authorized_owner_sender(
    sender_id: str,
    *,
    owner_open_id: str | None,
    web_session: FeishuWebSession | None,
    send_mode: str = "auto",
    token_record: Any | None = None,
    api_base_url: str | None = None,
) -> bool:
    """Return True iff ``sender_id`` matches the configured owner or a verified same-owner web session."""
    clean_sender = (sender_id or "").strip()
    if not clean_sender:
        return False
    target_owner = (owner_open_id or "").strip()
    if target_owner and hmac.compare_digest(clean_sender, target_owner):
        return True
    if (send_mode or "auto").strip().lower() != "openapi":
        auth_ok, _ = is_web_session_authorized_for_owner(
            web_session,
            target_owner or None,
            token_record=token_record,
            api_base_url=api_base_url,
        )
        if auth_ok and web_session is not None:
            for candidate in (web_session.user_id, web_session.open_id):
                c = (candidate or "").strip()
                if c and hmac.compare_digest(clean_sender, c):
                    return True
    return False


async def normalize_and_claim_ai_command_message(
    *,
    message: FeishuUserMessage,
    parsed_instruction: str,
    owner_open_id: str,
    user_client: Any,
    dedup_store: Any,
    cross_claims: dict[tuple[str, str, str], tuple[int, str, str]],
    lock: threading.RLock,
    dry_run: bool = False,
    resuming_completed: bool = False,
) -> tuple[FeishuUserMessage, str, list[str], bool]:
    """Normalize an incoming OpenAPI/web message and claim deduplication across both ID namespaces.

    Returns ``(effective_message, primary_dedup_key, alias_dedup_keys, claimed_ok)``.
    """
    orig_mid = (message.message_id or "").strip()
    orig_ns = "web" if is_web_entity_id(orig_mid) else "openapi"
    wt = user_client._get_web_transport() if hasattr(user_client, "_get_web_transport") else None
    effective_msg = message
    alias_mids: list[str] = []

    if not dry_run and not resuming_completed and wt is not None and orig_ns == "openapi":
        try:
            resolved_web = await wt.resolve_web_message(
                orig_mid,
                chat_id=message.chat_id,
                text=message.text,
                create_time_ms=message.create_time_ms,
            )
        except Exception:
            resolved_web = None
        if resolved_web is not None:
            converted = wt.to_user_message(resolved_web)
            if converted is not None:
                wt.bind_message_alias(orig_mid, converted.message_id)
                alias_mids.append(orig_mid)
                effective_msg = converted

    if wt is not None:
        mapped_alias = wt.get_message_alias(effective_msg.message_id)
        if (
            mapped_alias
            and mapped_alias not in alias_mids
            and mapped_alias != effective_msg.message_id
        ):
            alias_mids.append(mapped_alias)

    owner_key = (owner_open_id or "owner").strip()
    primary_key = f"ai_cmd:{owner_key}:{effective_msg.message_id}"
    extra_keys = [f"ai_cmd:{owner_key}:{amid}" for amid in alias_mids]

    if dry_run:
        return effective_msg, primary_key, extra_keys, True

    if resuming_completed:
        dedup_store.claim(primary_key)
        for ek in extra_keys:
            dedup_store.claim(ek)
        return effective_msg, primary_key, extra_keys, True

    canonical_chat = (
        wt.canonical_chat_id(effective_msg.chat_id)
        if wt is not None
        else (effective_msg.chat_id or "").strip()
    )
    norm_instr = " ".join((parsed_instruction or effective_msg.text or "").strip().split()).lower()
    cross_key = (owner_key, canonical_chat, norm_instr)

    with lock:
        prev = cross_claims.get(cross_key)
        if prev is not None and canonical_chat and norm_instr:
            prev_ms, prev_ns, prev_mid = prev
            if prev_ns != orig_ns and (
                prev_ms == 0
                or effective_msg.create_time_ms == 0
                or abs(effective_msg.create_time_ms - prev_ms) <= 30_000
            ):
                if wt is not None:
                    if orig_ns == "openapi":
                        wt.bind_message_alias(orig_mid, prev_mid)
                    else:
                        wt.bind_message_alias(prev_mid, orig_mid)
                return effective_msg, primary_key, extra_keys, False

        if not dedup_store.claim(primary_key):
            return effective_msg, primary_key, extra_keys, False
        for ek in extra_keys:
            if not dedup_store.claim(ek):
                dedup_store.release(primary_key)
                return effective_msg, primary_key, extra_keys, False

        if canonical_chat and norm_instr:
            cross_claims[cross_key] = (
                effective_msg.create_time_ms,
                orig_ns,
                effective_msg.message_id,
            )
            if len(cross_claims) > 512:
                oldest_key = next(iter(cross_claims))
                cross_claims.pop(oldest_key, None)

    return effective_msg, primary_key, extra_keys, True


def evaluate_scope_preflight_with_web_session(
    record: FeishuUserTokenRecord | None,
    *,
    now: float | None = None,
    fallback_bot_app_id: str | None = None,
    web_session: Any | None = None,
    configured_send_mode: str = "auto",
    configured_owner_open_id: str | None = None,
    api_base_url: str | None = None,
) -> FeishuScopePreflightResult:
    """Inspect a user token record (and optional web session) and produce scope & auth guidance."""
    cmd = build_auth_login_command()
    mode_pref = (configured_send_mode or "auto").strip().lower()
    raw_has_web = bool(
        mode_pref != "openapi"
        and web_session is not None
        and getattr(web_session, "is_valid_structure", lambda: False)()
    )
    target_owner = (configured_owner_open_id or "").strip() or (
        record.open_id if (record is not None and record.source != "web_session") else ""
    )
    web_auth_ok, web_mismatch_reason = (
        is_web_session_authorized_for_owner(
            web_session if isinstance(web_session, FeishuWebSession) else None,
            target_owner or None,
            token_record=record,
            api_base_url=api_base_url,
        )
        if raw_has_web
        else (False, "no_web_session")
    )
    has_web = raw_has_web and web_auth_ok
    web_uid = (str(getattr(web_session, "user_id", "") or "").strip() or None) if has_web else None
    web_uname = (str(getattr(web_session, "name", "") or "").strip() or None) if has_web else None
    web_open_id = (
        (str(getattr(web_session, "open_id", "") or "").strip() or None) if has_web else None
    )
    mismatch_note = (
        f"Stored Feishu web session ({getattr(web_session, 'open_id', None) or getattr(web_session, 'user_id', None) or 'unverified'}) "
        f"does not match configured owner `{target_owner}` ({web_mismatch_reason}). "
        "Re-login with the owner account or run `claude-hub feishu ai-command web-login --switch-owner`."
        if (raw_has_web and not web_auth_ok and target_owner)
        else None
    )

    if mode_pref == "web_session" and not has_web:
        steps: tuple[str, ...] = (
            (
                mismatch_note,
                "1. Run `claude-hub feishu ai-command web-login --switch-owner` to switch owner to the scanned web session.",
            )
            if mismatch_note
            else (
                "1. Run `claude-hub feishu ai-command web-login` and scan the Feishu QR code on your phone.",
                "2. Or import an existing `~/.larkx/credentials.json` with `claude-hub feishu ai-command web-login --import-larkx`.",
            )
        )
        return FeishuScopePreflightResult(
            has_token=record is not None,
            open_id=(record.open_id if record is not None else None) or (target_owner or None),
            user_name=(record.user_name or None) if record is not None else None,
            app_id=record.app_id if record is not None else None,
            source=record.source if record is not None else None,
            expires_at=record.expires_at if record is not None else 0.0,
            token_expired=True if record is None else record.is_expired(now=now, skew_seconds=0.0),
            granted_scopes=tuple(sorted(record.scopes_set())) if record is not None else (),
            required_send_scope=REQUIRED_SEND_AS_USER_SCOPE,
            can_send_as_user=False,
            can_react_as_user=False,
            can_read_messages=False,
            missing_required_scopes=(REQUIRED_SEND_AS_USER_SCOPE,),
            missing_recommended_scopes=RECOMMENDED_AI_COMMAND_SCOPES,
            auth_login_command="claude-hub feishu ai-command web-login",
            remediation_steps=steps,
            can_send=False,
            send_mode="none",
            has_send_as_user_scope=False,
            can_send_via_bot=False,
            fallback_bot_app_id=None,
            has_web_session=False,
            web_user_id=None,
            web_user_name=None,
        )

    if (record is None or record.source == "web_session") and has_web:
        eff_open = web_open_id or web_uid or (record.open_id if record is not None else None)
        eff_name = (record.user_name if record is not None else None) or web_uname
        return FeishuScopePreflightResult(
            has_token=True,
            open_id=eff_open,
            user_name=eff_name,
            app_id=(record.app_id if record is not None else None) or "web_session",
            source="web_session",
            expires_at=0.0,
            token_expired=False,
            granted_scopes=tuple(RECOMMENDED_AI_COMMAND_SCOPES),
            required_send_scope=REQUIRED_SEND_AS_USER_SCOPE,
            can_send_as_user=True,
            can_react_as_user=True,
            can_read_messages=True,
            missing_required_scopes=(),
            missing_recommended_scopes=(),
            auth_login_command="claude-hub feishu ai-command web-login",
            remediation_steps=(
                f"Ready via Feishu web session ({eff_name or eff_open or 'authenticated'}): "
                "reads context, reacts, and replies in-place as user in 1v1 DMs and groups.",
            ),
            can_send=True,
            send_mode="web_session",
            has_send_as_user_scope=False,
            can_send_via_bot=False,
            fallback_bot_app_id=None,
            has_web_session=True,
            web_user_id=web_uid,
            web_user_name=web_uname,
        )

    if record is None or record.source == "web_session":
        base_steps: list[str] = []
        if mismatch_note:
            base_steps.append(mismatch_note)
        base_steps.extend(
            [
                "1. Scan Feishu web login QR: `claude-hub feishu ai-command web-login` (recommended for 1v1 DM & group user-identity replies).",
                f"2. Or authenticate via lark-cli: `{cmd}` (or sign in via Hub Feishu OAuth).",
                "3. Run `claude-hub feishu ai-command status` or click 'Import ~/.lark-cli Token' in Settings.",
            ]
        )
        return FeishuScopePreflightResult(
            has_token=False,
            open_id=target_owner or None,
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
            remediation_steps=tuple(base_steps),
            can_send=False,
            send_mode="none",
            has_send_as_user_scope=False,
            can_send_via_bot=False,
            fallback_bot_app_id=None,
            has_web_session=False,
            web_user_id=None,
            web_user_name=None,
        )

    granted = record.scopes_set()
    has_send_as_user = REQUIRED_SEND_AS_USER_SCOPE in granted
    can_react = bool(granted & {"im:message.reactions:write_only", "im:message"}) or has_web
    can_read = (
        bool(
            granted
            & {
                "im:message:readonly",
                "im:message",
                "im:message.group_msg:get_as_user",
                "im:message.p2p_msg:get_as_user",
            }
        )
        or has_web
    )
    resolved_bot_app = (
        record.app_id
        if (fallback_bot_app_id or "").strip() == record.app_id
        else (record.app_id if (fallback_bot_app_id is None and record.app_secret.strip()) else "")
    )
    can_send_via_bot = bool(resolved_bot_app and can_read)
    use_web = has_web and (mode_pref == "web_session" or not has_send_as_user)
    can_send = use_web or has_send_as_user or can_send_via_bot
    if use_web:
        send_mode = "web_session"
    elif has_send_as_user:
        send_mode = "user"
    elif can_send_via_bot:
        send_mode = "bot_fallback"
    else:
        send_mode = "none"

    missing_req: list[str] = []
    if not has_send_as_user and not use_web:
        missing_req.append(REQUIRED_SEND_AS_USER_SCOPE)
    if not can_read and REQUIRED_IM_MESSAGE_SCOPE not in granted:
        missing_req.append(REQUIRED_IM_MESSAGE_SCOPE)
    missing_rec = [s for s in RECOMMENDED_AI_COMMAND_SCOPES if s not in granted]

    steps_list: list[str] = []
    if mismatch_note:
        steps_list.append(mismatch_note)
    if use_web:
        steps_list.append(
            f"Ready via Feishu web session ({web_uname or web_uid or record.open_id}): "
            "reads context, reacts, and replies in-place as user in 1v1 DMs and groups."
        )
    elif has_send_as_user:
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
        user_name=record.user_name or web_uname or None,
        app_id=record.app_id,
        source=record.source,
        expires_at=record.expires_at,
        token_expired=record.is_expired(now=now, skew_seconds=0.0) and not use_web,
        granted_scopes=tuple(sorted(granted)),
        required_send_scope=REQUIRED_SEND_AS_USER_SCOPE,
        can_send_as_user=has_send_as_user or use_web,
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
        has_web_session=has_web,
        web_user_id=web_uid,
        web_user_name=web_uname,
    )


class FeishuWebUserTransport:
    """Adapts ``FeishuWebClient`` to the user-identity read/react/send contract used by ``/ai``."""

    def __init__(
        self,
        web_client: FeishuWebClient,
        *,
        owner_open_id: str = "",
        api_base_url: str | None = None,
    ) -> None:
        self._web = web_client
        self._owner_open_id = (owner_open_id or "").strip()
        self._api_base_url = api_base_url
        self._chat_cache: dict[str, WebChat] = {}
        self._open_to_web_chat: dict[str, str] = {}
        self._web_to_open_chat: dict[str, str] = {}
        self._open_to_web_msg: dict[str, str] = {}
        self._web_to_open_msg: dict[str, str] = {}
        self._last_msg_cache: dict[str, WebMessage] = {}
        self._msg_cache: OrderedDict[str, WebMessage] = OrderedDict()

    @property
    def web_client(self) -> FeishuWebClient:
        return self._web

    def _remember_msg(self, msg: WebMessage) -> None:
        if not msg.id:
            return
        self._msg_cache[msg.id] = msg
        self._msg_cache.move_to_end(msg.id)
        while len(self._msg_cache) > _MAX_CACHED_MESSAGES:
            self._msg_cache.popitem(last=False)

    def _remember_chat(self, chat: WebChat) -> None:
        if not chat.id:
            return
        self._chat_cache[chat.id] = chat
        if chat.open_chat_id:
            self._open_to_web_chat[chat.open_chat_id] = chat.id
            self._web_to_open_chat[chat.id] = chat.open_chat_id

    def canonical_chat_id(self, chat_id: str) -> str:
        clean = (chat_id or "").strip()
        if not clean:
            return ""
        if clean.startswith("oc_"):
            return self._open_to_web_chat.get(clean, clean)
        return clean

    def bind_message_alias(self, openapi_message_id: str, web_message_id: str) -> None:
        om = (openapi_message_id or "").strip()
        wm = (web_message_id or "").strip()
        if om and wm and om != wm:
            self._open_to_web_msg[om] = wm
            self._web_to_open_msg[wm] = om

    def get_message_alias(self, message_id: str) -> str | None:
        clean = (message_id or "").strip()
        if not clean:
            return None
        return self._open_to_web_msg.get(clean) or self._web_to_open_msg.get(clean)

    def resolve_owner_identity(self, session: FeishuWebSession | None = None) -> str:
        s = session or self._web.session()
        if s is None:
            return ""
        auth_ok, _ = is_web_session_authorized_for_owner(
            s,
            self._owner_open_id or None,
            api_base_url=self._api_base_url,
        )
        if not auth_ok:
            return ""
        return (s.open_id or s.user_id).strip()

    def to_user_message(
        self,
        msg: WebMessage,
        *,
        session: FeishuWebSession | None = None,
    ) -> FeishuUserMessage | None:
        if msg.recalled or not msg.id:
            return None
        self._remember_msg(msg)
        s = session or self._web.session()
        web_uid = s.user_id.strip() if s is not None else ""
        auth_ok, _ = is_web_session_authorized_for_owner(
            s,
            self._owner_open_id or None,
            api_base_url=self._api_base_url,
        )
        if web_uid and msg.from_id == web_uid and auth_ok and s is not None:
            # Only use s.open_id if present on the verified session itself; never fabricate self._owner_open_id
            sender_id = (s.open_id or s.user_id).strip()
        else:
            sender_id = msg.from_id
        text = describe(msg.type, msg.content, web_uid).strip()
        in_topic = is_in_topic(msg)
        thread_id = (msg.thread_id or msg.root_id) if in_topic else None
        return FeishuUserMessage(
            message_id=msg.id,
            chat_id=msg.chat_id,
            sender_open_id=sender_id,
            sender_type="user",
            msg_type="post" if msg.type == 2 else "text",
            text=text,
            create_time_ms=msg.create_time_ms,
            thread_id=thread_id,
            parent_id=msg.parent_id or None,
            root_id=msg.root_id or None,
        )

    async def resolve_web_chat_id(self, chat_id: str) -> str:
        clean = (chat_id or "").strip()
        if not clean:
            return ""
        if is_web_entity_id(clean):
            return clean
        if clean in self._open_to_web_chat:
            return self._open_to_web_chat[clean]
        try:
            page = await self._web.feed_cards(feed_type=FEED_INBOX, count=50)
            for c in page.chats.values():
                self._remember_chat(c)
            for cid, m in page.last.items():
                self._last_msg_cache[cid] = m
                self._remember_msg(m)
        except Exception:
            pass
        if clean in self._open_to_web_chat:
            return self._open_to_web_chat[clean]
        raise FeishuWebSessionError(
            f"Cannot resolve OpenAPI chat_id {clean!r} to a numeric Feishu web chat ID"
        )

    async def resolve_web_message(
        self,
        message_id: str,
        *,
        chat_id: str | None = None,
        text: str | None = None,
        create_time_ms: int | None = None,
    ) -> WebMessage | None:
        clean_mid = (message_id or "").strip()
        if not clean_mid:
            return None
        if not is_web_entity_id(clean_mid):
            mapped_id = self._open_to_web_msg.get(clean_mid)
            if mapped_id and mapped_id in self._msg_cache:
                return self._msg_cache[mapped_id]
            raw_cid = (chat_id or "").strip()
            if not raw_cid:
                return None
            try:
                web_cid = await self.resolve_web_chat_id(raw_cid)
            except FeishuWebSessionError:
                return None
            if not web_cid:
                return None
            try:
                await self.list_messages_page(
                    container_id_type="chat",
                    container_id=web_cid,
                    page_size=20,
                )
            except Exception:
                pass
            s = self._web.session()
            web_uid = s.user_id.strip() if s is not None else ""
            want_text = (text or "").strip()
            candidates: list[WebMessage] = []
            for m in reversed(list(self._msg_cache.values())):
                if m.chat_id != web_cid or not is_web_entity_id(m.id):
                    continue
                if web_uid and m.from_id != web_uid:
                    continue
                m_text = describe(m.type, m.content, web_uid).strip()
                if want_text and m_text != want_text:
                    continue
                if (
                    create_time_ms
                    and m.create_time_ms
                    and abs(m.create_time_ms - create_time_ms) > 60_000
                ):
                    continue
                candidates.append(m)
            if candidates:
                chosen = candidates[0]
                self.bind_message_alias(clean_mid, chosen.id)
                return chosen
            return None

        if clean_mid in self._msg_cache:
            return self._msg_cache[clean_mid]
        try:
            fetched = await self._web.messages_by_id([clean_mid])
            if fetched:
                self._remember_msg(fetched[0])
                return fetched[0]
        except Exception:
            pass
        return None

    async def list_chats(self, *, page_size: int = 20) -> list[FeishuChatInfo]:
        page = await self._web.feed_cards(
            feed_type=FEED_INBOX,
            count=max(20, min(100, page_size)),
        )
        for c in page.chats.values():
            self._remember_chat(c)
        for cid, m in page.last.items():
            self._last_msg_cache[cid] = m
            self._remember_msg(m)

        ordered_ids: list[str] = []
        for card in page.cards:
            if card.type == CARD_CHAT and card.id and card.id not in ordered_ids:
                ordered_ids.append(card.id)
        for cid in page.chats:
            if cid not in ordered_ids:
                ordered_ids.append(cid)

        infos: list[FeishuChatInfo] = []
        for cid in ordered_ids[: max(1, page_size)]:
            chat_item = page.chats.get(cid)
            if chat_item is None or chat_item.dissolved:
                continue
            mode = (
                "p2p" if chat_item.type == CHAT_P2P else ("topic" if chat_item.thread else "group")
            )
            infos.append(
                FeishuChatInfo(
                    chat_id=chat_item.id,
                    name=chat_item.name
                    or (
                        f"DM {chat_item.id}"
                        if chat_item.type == CHAT_P2P
                        else f"Chat {chat_item.id}"
                    ),
                    chat_mode=mode,
                    user_count=chat_item.member_count or None,
                    bot_count=None,
                )
            )
        return infos

    async def list_messages_page(
        self,
        *,
        container_id_type: str = "chat",
        container_id: str,
        sort_type: str = "ByCreateTimeDesc",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> FeishuMessagePage:
        s = self._web._require_session()
        if container_id_type == "thread":
            clean_tid = (container_id or "").strip()
            if not is_web_entity_id(clean_tid):
                mapped_tid = self._open_to_web_msg.get(clean_tid)
                if not mapped_tid:
                    raise FeishuWebSessionError(
                        f"Cannot read Feishu web thread with OpenAPI thread_id {clean_tid!r}"
                    )
                clean_tid = mapped_tid
            topics = await self._web.topics([clean_tid])
            t = topics.get(clean_tid)
            if t is None or (t.last < 0 and t.replies <= 0):
                return FeishuMessagePage(items=(), has_more=False, page_token="")
            t_last = t.last if t.last >= 0 else max(0, t.start + t.replies - 1)
            end_pos = (
                int(page_token) if (page_token and page_token.lstrip("-").isdigit()) else t_last
            )
            if end_pos < t.start:
                return FeishuMessagePage(items=(), has_more=False, page_token="")
            start_pos = max(0, t.start, end_pos - max(1, min(page_size, 50)) + 1)
            topic_chat = t.chat or (
                self._msg_cache[clean_tid].chat_id if clean_tid in self._msg_cache else ""
            )
            raw_msgs = await self._web.topic_messages(
                clean_tid,
                topic_chat,
                list(range(start_pos, end_pos + 1)),
            )
            if (
                start_pos <= t.start
                and clean_tid in self._msg_cache
                and all(m.id != clean_tid for m in raw_msgs)
            ):
                raw_msgs = [self._msg_cache[clean_tid], *raw_msgs]
            parsed = [
                um
                for m in raw_msgs
                for um in (self.to_user_message(m, session=s),)
                if um is not None
            ]
            if sort_type == "ByCreateTimeDesc":
                parsed.reverse()
            has_more = start_pos > t.start
            next_tok = str(start_pos - 1) if has_more else ""
            return FeishuMessagePage(items=tuple(parsed), has_more=has_more, page_token=next_tok)

        web_chat_id = await self.resolve_web_chat_id(container_id)
        chat: WebChat | None = self._chat_cache.get(web_chat_id)
        if page_token and page_token.lstrip("-").isdigit():
            end_pos = int(page_token)
            if end_pos < 0:
                return FeishuMessagePage(items=(), has_more=False, page_token="")
        else:
            chats_map = await self._web.chats([web_chat_id])
            chat = chats_map.get(web_chat_id) or chat
            if chat is not None:
                self._remember_chat(chat)
            if chat is None:
                return FeishuMessagePage(items=(), has_more=False, page_token="")
            if not chat.has_last and chat.last_position <= 0:
                last_cached = self._last_msg_cache.get(web_chat_id)
                end_pos = last_cached.position if last_cached is not None else 0
            else:
                end_pos = chat.last_position

        step = max(1, min(page_size, 50))
        start_pos = max(0, end_pos - step + 1)
        raw_msgs = await self._web.messages_at(
            web_chat_id,
            list(range(start_pos, end_pos + 1)),
        )
        if not raw_msgs and start_pos == 0 and web_chat_id in self._last_msg_cache:
            raw_msgs = [self._last_msg_cache[web_chat_id]]

        # Discover and pull active topics/threads whose roots are in this chat window (threads.go / topics.go)
        candidate_topic_ids: list[str] = []
        for m in raw_msgs:
            for tid_cand in (m.id, m.thread_id, m.root_id):
                if tid_cand and is_web_entity_id(tid_cand) and tid_cand not in candidate_topic_ids:
                    candidate_topic_ids.append(tid_cand)
        if web_chat_id in self._last_msg_cache:
            last_m = self._last_msg_cache[web_chat_id]
            for tid_cand in (last_m.id, last_m.thread_id, last_m.root_id):
                if tid_cand and is_web_entity_id(tid_cand) and tid_cand not in candidate_topic_ids:
                    candidate_topic_ids.append(tid_cand)
        if chat is not None and chat.last_message_id:
            if (
                is_web_entity_id(chat.last_message_id)
                and chat.last_message_id not in candidate_topic_ids
            ):
                candidate_topic_ids.append(chat.last_message_id)

        if candidate_topic_ids:
            try:
                topics_map = await self._web.topics(candidate_topic_ids[:50])
            except Exception:
                topics_map = {}
            seen_msg_ids = {m.id for m in raw_msgs if m.id}
            for tid, tp in topics_map.items():
                if tp.last < 0 and tp.replies <= 0:
                    continue
                t_end = tp.last if tp.last >= 0 else max(0, tp.start + tp.replies - 1)
                t_start = max(0, tp.start, t_end - step + 1)
                try:
                    t_msgs = await self._web.topic_messages(
                        tp.id or tid,
                        tp.chat or web_chat_id,
                        list(range(t_start, t_end + 1)),
                    )
                except Exception:
                    continue
                for tm in t_msgs:
                    if tm.id and tm.id not in seen_msg_ids:
                        seen_msg_ids.add(tm.id)
                        raw_msgs.append(tm)

        parsed_msgs = [
            um for m in raw_msgs for um in (self.to_user_message(m, session=s),) if um is not None
        ]
        if sort_type == "ByCreateTimeDesc":
            parsed_msgs.sort(key=lambda item: item.create_time_ms, reverse=True)
        else:
            parsed_msgs.sort(key=lambda item: item.create_time_ms)
        has_more = start_pos > 0
        next_tok = str(start_pos - 1) if has_more else ""
        return FeishuMessagePage(items=tuple(parsed_msgs), has_more=has_more, page_token=next_tok)

    async def get_message(self, message_id: str) -> FeishuUserMessage | None:
        clean_id = (message_id or "").strip()
        if not clean_id:
            return None
        s = self._web._require_session()
        resolved = await self.resolve_web_message(clean_id)
        if resolved is None:
            if not is_web_entity_id(clean_id):
                raise FeishuWebSessionError(
                    f"Cannot fetch unresolved OpenAPI message_id {clean_id!r} on Feishu web gateway"
                )
            return None
        return self.to_user_message(resolved, session=s)

    async def add_reaction(self, message_id: str, emoji_type: str) -> str:
        clean_id = (message_id or "").strip()
        if not is_web_entity_id(clean_id):
            resolved = await self.resolve_web_message(clean_id)
            if resolved is None:
                raise FeishuWebSessionError(
                    f"Cannot add web reaction to unresolved OpenAPI message_id {clean_id!r}"
                )
            clean_id = resolved.id
        await self._web.react(clean_id, emoji_type)
        return f"{_WEB_REACTION_PREFIX}{emoji_type}"

    async def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        clean_id = (message_id or "").strip()
        if not is_web_entity_id(clean_id):
            resolved = await self.resolve_web_message(clean_id)
            if resolved is None:
                raise FeishuWebSessionError(
                    f"Cannot remove web reaction on unresolved OpenAPI message_id {clean_id!r}"
                )
            clean_id = resolved.id
        emoji_type = reaction_id
        if emoji_type.startswith(_WEB_REACTION_PREFIX):
            emoji_type = emoji_type[len(_WEB_REACTION_PREFIX) :]
        await self._web.unreact(clean_id, emoji_type or "OnIt")

    async def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: Mapping[str, Any] | str,
        *,
        uuid: str | None = None,
    ) -> dict[str, Any]:
        web_chat_id = await self.resolve_web_chat_id(chat_id)
        text, title = extract_text_and_title_from_openapi_content(msg_type, content)
        out = formatted_out(
            WebOut(chat=web_chat_id, title=title, cid=(uuid or "").strip()),
            text,
        )
        sent = await self._web.send(out)
        self._remember_msg(sent)
        return {
            "message_id": sent.id,
            "chat_id": web_chat_id,
            "msg_type": msg_type,
            "send_mode": "web_session",
        }

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: Mapping[str, Any] | str,
        *,
        chat_id: str | None = None,
        reply_in_thread: bool = False,
        uuid: str | None = None,
    ) -> FeishuReplyReceipt:
        clean_mid = (message_id or "").strip()
        if not clean_mid:
            raise FeishuWebSessionError("missing message_id for web reply")
        if is_web_entity_id(clean_mid):
            target_msg = self._msg_cache.get(clean_mid)
            if target_msg is None:
                try:
                    fetched = await self._web.messages_by_id([clean_mid])
                    if fetched:
                        target_msg = fetched[0]
                        self._remember_msg(target_msg)
                except Exception:
                    pass
        else:
            mapped_id = self._open_to_web_msg.get(clean_mid)
            target_msg = self._msg_cache.get(mapped_id) if mapped_id else None
            if target_msg is None:
                raise FeishuWebSessionError(
                    f"Cannot reply via Feishu web gateway to unresolved OpenAPI message_id {clean_mid!r}"
                )

        raw_chat_id = (chat_id or (target_msg.chat_id if target_msg else "")).strip()
        web_chat_id = await self.resolve_web_chat_id(raw_chat_id)
        if not web_chat_id:
            raise FeishuWebSessionError(f"cannot determine chatId for reply to {clean_mid}")

        if target_msg is None:
            target_msg = WebMessage(id=clean_mid, chat_id=web_chat_id)

        # In 1v1 DMs (CHAT_P2P), keep replies in the main DM stream unless the message itself is in a topic
        chat_obj = self._chat_cache.get(web_chat_id)
        if chat_obj is None:
            try:
                loaded_chats = await self._web.chats([web_chat_id])
                chat_obj = loaded_chats.get(web_chat_id)
                if chat_obj is not None:
                    self._remember_chat(chat_obj)
            except Exception:
                pass
        if chat_obj is not None and chat_obj.type == CHAT_P2P and not is_in_topic(target_msg):
            effective_in_thread = False
        else:
            effective_in_thread = bool(reply_in_thread) or is_in_topic(target_msg)

        text, title = extract_text_and_title_from_openapi_content(msg_type, content)
        out = formatted_out(
            WebOut(
                chat=web_chat_id,
                title=title,
                reply_to=target_msg,
                in_thread=effective_in_thread,
                cid=(uuid or "").strip(),
            ),
            text,
        )
        sent = await self._web.send(out)
        if not sent.id.strip():
            raise FeishuDeliveryUncertainError(
                "Feishu web reply message identity is unavailable; delivery may have occurred"
            )
        self._remember_msg(sent)
        sent_thread = (sent.thread_id or sent.root_id) if is_in_topic(sent) else None
        return FeishuReplyReceipt(message_id=sent.id, thread_id=sent_thread)

    async def search_messages_page(
        self,
        query: str,
        *,
        page_size: int = 20,
    ) -> FeishuMessageSearchPage:
        clean_q = (query or "").strip().lower()
        if not clean_q:
            return FeishuMessageSearchPage(items=(), has_more=False, page_token=None)
        s = self._web.session()
        web_uid = s.user_id.strip() if s is not None else ""
        matched_ids: list[str] = []
        for m in reversed(list(self._msg_cache.values())):
            if m.recalled or not m.id:
                continue
            if web_uid and m.from_id != web_uid:
                continue
            txt = describe(m.type, m.content, web_uid).lower()
            if clean_q in txt and m.id not in matched_ids:
                matched_ids.append(m.id)
                if len(matched_ids) >= max(1, page_size):
                    break
        return FeishuMessageSearchPage(items=tuple(matched_ids), has_more=False, page_token=None)

    def ingest_pushed_message(self, pushed: PushedMessage) -> FeishuUserMessage | None:
        """Convert a ``PushedMessage`` from ``FeishuWebFrontierListener`` into a ``FeishuUserMessage``."""
        if pushed.removed:
            return None
        return self.to_user_message(pushed.message)

    def create_frontier_listener(
        self,
        deliver: Any,
        *,
        on_state: Any = None,
    ) -> FeishuWebFrontierListener:
        return FeishuWebFrontierListener(self._web, deliver, on_state=on_state)


class FeishuWebLoginManager:
    """Tracks in-progress QR login flows and coordinates status/logout/import for a web session store."""

    def __init__(
        self,
        store: FeishuWebSessionStore | None = None,
        *,
        client_factory: Any = None,
    ) -> None:
        self._store = store or get_feishu_web_session_store()
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._logins: dict[str, FeishuWebQRLoginSession] = {}
        self._latest_login_id: str | None = None
        self._generation: int = 0

    def build_client(self) -> FeishuWebClient:
        if self._client_factory is not None:
            created: FeishuWebClient = self._client_factory(self._store)
            return created
        return FeishuWebClient(self._store)

    def get_active_login(self, login_id: str | None = None) -> FeishuWebQRLoginSession | None:
        with self._lock:
            target_id = (login_id or self._latest_login_id or "").strip()
            if not target_id:
                return None
            q = self._logins.get(target_id)
            if q is None:
                return None
            if q.cancelled or time.time() > q.expires_at:
                self._logins.pop(target_id, None)
                if self._latest_login_id == target_id:
                    self._latest_login_id = None
                return None
            return q

    async def start_login(self, *, region: str = "feishu") -> FeishuWebQRLoginSession:
        client = self.build_client()
        qr_session = await client.start_login(region=region)
        with self._lock:
            qr_session.store_generation = self._store.generation
            self._logins[qr_session.login_id] = qr_session
            self._latest_login_id = qr_session.login_id
        return qr_session

    async def poll_login(
        self, *, login_id: str | None = None
    ) -> tuple[str, FeishuWebSession | None, FeishuWebQRLoginSession | None]:
        with self._lock:
            target_id = (login_id or self._latest_login_id or "").strip()
            q = self._logins.get(target_id) if target_id else None
            gen = self._generation
        if q is None or q.cancelled:
            return "expired", None, q
        client = self.build_client()
        try:
            st, sess = await client.poll_login(q)
        except FeishuWebQRLoginExpiredError:
            with self._lock:
                self._logins.pop(q.login_id, None)
                if self._latest_login_id == q.login_id:
                    self._latest_login_id = None
            return "expired", None, q
        with self._lock:
            if q.cancelled or self._generation != gen or self._logins.get(q.login_id) is not q:
                self._logins.pop(q.login_id, None)
                if self._latest_login_id == q.login_id:
                    self._latest_login_id = None
                return "expired", None, q
            if st == "done" and sess is not None:
                self._logins.pop(q.login_id, None)
                if self._latest_login_id == q.login_id:
                    self._latest_login_id = None
                return "done", sess, q
        return "waiting", None, q

    async def astatus_dict(
        self,
        *,
        validate: bool = False,
        auto_import_larkx: bool = False,
    ) -> dict[str, Any]:
        sess = self._store.load(auto_import_larkx=auto_import_larkx)
        active = self.get_active_login()
        if sess is None:
            return {
                "has_session": False,
                "valid": False,
                "region": "feishu",
                "domain": "feishu.cn",
                "user_id": None,
                "name": None,
                "open_id": None,
                "device_id": None,
                "web_app_id": None,
                "saved_at": 0.0,
                "source": None,
                "cookie_names": [],
                "cookies": None,
                "csrf_token": None,
                "active_login": active.to_public_dict() if active is not None else None,
            }
        is_valid: bool | None = None
        if validate:
            client = self.build_client()
            try:
                is_valid = await client.valid(session=sess)
                if is_valid and not sess.user_id:
                    sess = await client.profile(session=sess)
            except Exception:
                is_valid = False
            if not is_valid:
                self._store.mark_invalid()
        out = sess.to_redacted_dict()
        if validate and not is_valid:
            out["has_session"] = False
            out["valid"] = False
        else:
            out["valid"] = is_valid if validate else True
        out["active_login"] = active.to_public_dict() if active is not None else None
        return out

    def status_dict(self, *, auto_import_larkx: bool = False) -> dict[str, Any]:
        sess = self._store.load(auto_import_larkx=auto_import_larkx)
        active = self.get_active_login()
        if sess is None:
            return {
                "has_session": False,
                "valid": False,
                "region": "feishu",
                "domain": "feishu.cn",
                "user_id": None,
                "name": None,
                "open_id": None,
                "device_id": None,
                "web_app_id": None,
                "saved_at": 0.0,
                "source": None,
                "cookie_names": [],
                "cookies": None,
                "csrf_token": None,
                "active_login": active.to_public_dict() if active is not None else None,
            }
        out = sess.to_redacted_dict()
        out["valid"] = True
        out["active_login"] = active.to_public_dict() if active is not None else None
        return out

    def logout(self) -> bool:
        with self._lock:
            self._generation += 1
            for q in self._logins.values():
                q.cancel()
            self._logins.clear()
            self._latest_login_id = None
        return self._store.clear()
