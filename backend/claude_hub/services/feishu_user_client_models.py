"""Value objects and message parsing for the Feishu user client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from claude_hub.services.feishu_bot import (
    FeishuBotConfigurationError,
    _forward_body,
    _forward_content,
)
from claude_hub.services.feishu_user_message import FeishuUserMessage
from claude_hub.services.feishu_user_token_store import FeishuUserTokenRecord

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
    has_web_session: bool = False
    web_user_id: str | None = None
    web_user_name: str | None = None

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
            "has_web_session": self.has_web_session,
            "web_user_id": self.web_user_id,
            "web_user_name": self.web_user_name,
            "send_mode": self.send_mode,
            "can_react_as_user": self.can_react_as_user,
            "can_read_messages": self.can_read_messages,
            "missing_required_scopes": list(self.missing_required_scopes),
            "missing_recommended_scopes": list(self.missing_recommended_scopes),
            "auth_login_command": self.auth_login_command,
            "remediation_steps": list(self.remediation_steps),
        }


@dataclass(frozen=True)
class FeishuMessageSearchPage:
    """One page of message IDs from ``POST /open-apis/search/v2/message``."""

    items: tuple[str, ...]
    has_more: bool
    page_token: str | None = None


@dataclass(frozen=True)
class FeishuMessagePage:
    """One paginated page of messages from ``GET /open-apis/im/v1/messages``."""

    items: tuple[FeishuUserMessage, ...]
    has_more: bool = False
    page_token: str = ""


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
    _, image_keys = _forward_content(_forward_body(item))
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
        has_images=msg_type == "image" or bool(image_keys),
        image_refs=tuple((message_id, key) for key in dict.fromkeys(image_keys)),
    )


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
    web_session: Any | None = None,
    configured_send_mode: str = "auto",
    configured_owner_open_id: str | None = None,
    api_base_url: str | None = None,
) -> FeishuScopePreflightResult:
    """Inspect a user token record (and optional web session) and produce clear scope & auth guidance."""
    from claude_hub.services.feishu_web_session.adapter import (
        evaluate_scope_preflight_with_web_session,
    )

    return evaluate_scope_preflight_with_web_session(
        record,
        now=now,
        fallback_bot_app_id=fallback_bot_app_id,
        web_session=web_session,
        configured_send_mode=configured_send_mode,
        configured_owner_open_id=configured_owner_open_id,
        api_base_url=api_base_url,
    )
