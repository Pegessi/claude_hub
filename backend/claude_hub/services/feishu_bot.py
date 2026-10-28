"""Feishu Bot credentials, outbound API client and persistent deduplication."""

from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import httpx

from claude_hub.services.proxy_rules import create_async_client
from claude_hub.services.runtime_isolation import resolve_runtime_home

_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_LEGACY_CLAIM_RETENTION_SECONDS = 60 * 60
_MAX_REPLY_CHARS = 20_000
_MAX_INBOUND_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_MIME_PREFIXES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


class FeishuBotError(RuntimeError):
    """Base error for a safe, expected Feishu Bot failure."""


class FeishuBotConfigurationError(FeishuBotError):
    """Raised when required Bot settings are absent or inconsistent."""


class FeishuBotRemoteError(FeishuBotError):
    """A safe description of a Feishu API request the remote service rejected.

    A generic failure cannot be acted on. The upstream code and support id
    identify which Feishu-side policy refused the call, and neither carries Hub
    credentials or request bodies.
    """

    def __init__(
        self,
        operation: str,
        *,
        code: int | str | None,
        remote_message: str,
        log_id: str | None,
    ) -> None:
        label = str(code) if code is not None else "unknown"
        super().__init__(f"Feishu rejected {operation} with code {label}")
        self.operation = operation
        self.code = code
        self.remote_message = remote_message
        self.log_id = log_id


@dataclass(frozen=True)
class FeishuBotProfile:
    """Public identity returned by Feishu for the authenticated Bot app."""

    name: str
    avatar_url: str | None
    open_id: str | None = None


@dataclass(frozen=True)
class FeishuBotConfig:
    """Credentials for one Feishu Bot long connection."""

    app_id: str
    app_secret: str = field(repr=False)
    api_base_url: str = "https://open.feishu.cn"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeishuBotConfig":
        env = os.environ if environ is None else environ
        values = {
            "app_id": env.get("CLAUDE_HUB_FEISHU_BOT_APP_ID", "").strip(),
            "app_secret": env.get("CLAUDE_HUB_FEISHU_BOT_APP_SECRET", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise FeishuBotConfigurationError(
                "Feishu Bot is not configured; missing explicit environment values: "
                + ", ".join(missing)
            )
        base_url = env.get("CLAUDE_HUB_FEISHU_API_BASE_URL", "https://open.feishu.cn").rstrip("/")
        return cls(api_base_url=base_url, **values)


def create_feishu_http_client(api_base_url: str, *, timeout: float) -> httpx.AsyncClient:
    """Create the Bot's outbound client using the Hub's shared proxy rules.

    Delegating keeps HTTPX from parsing ``NO_PROXY`` itself, which fails at
    construction time on the IPv6 CIDR entries deployments commonly set.
    """
    try:
        return create_async_client(api_base_url, timeout=timeout)
    except ValueError as exc:
        raise FeishuBotConfigurationError("Feishu API base URL must use HTTP or HTTPS") from exc


@dataclass(frozen=True)
class FeishuBinding:
    """An authorized p2p conversation bound to one existing Hub Chat tab."""

    owner_open_id: str
    owner_email: str
    sender_open_id: str
    app_id: str
    chat_id: str
    tab_id: str
    workspace_id: str | None
    created_at: float
    binding_generation: int = 0


@dataclass(frozen=True)
class FeishuMessageEvent:
    """Validated message fields from ``im.message.receive_v1``."""

    event_id: str
    message_id: str
    app_id: str
    sender_open_id: str
    chat_id: str
    text: str
    message_created_at_ms: int
    image_keys: tuple[str, ...] = ()
    chat_type: str = "p2p"
    thread_id: str | None = None
    parent_id: str | None = None
    root_id: str | None = None


class FeishuBotClient:
    """Small async client for Bot tenant tokens and text messages."""

    def __init__(
        self,
        config: FeishuBotConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._tenant_token: str | None = None
        self._token_expires_at = 0.0

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, path, **kwargs)
        async with create_feishu_http_client(self._config.api_base_url, timeout=15.0) as client:
            return await client.request(method, path, **kwargs)

    @staticmethod
    def _remote_error(
        response: httpx.Response,
        payload: Mapping[str, Any],
        operation: str,
    ) -> FeishuBotRemoteError:
        """Carry the upstream code and support id, never the Hub's request data."""
        raw_code = payload.get("code")
        usable_code = isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool)
        raw_message = payload.get("msg")
        raw_log_id = response.headers.get("x-tt-logid") or response.headers.get("x-request-id")
        return FeishuBotRemoteError(
            operation,
            code=raw_code if usable_code else None,
            remote_message=raw_message.strip()[:500] if isinstance(raw_message, str) else "",
            log_id=raw_log_id.strip()[:256] if raw_log_id and raw_log_id.strip() else None,
        )

    async def get_tenant_token(self) -> str:
        now = time.monotonic()
        if self._tenant_token and now < self._token_expires_at:
            return self._tenant_token
        response = await self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise FeishuBotError("Feishu returned an invalid Bot tenant token response") from exc
        if not isinstance(payload, dict):
            raise FeishuBotError("Feishu returned an invalid Bot tenant token response")
        # A rejected call carries its reason in the body, which raise_for_status
        # would discard before the caller can report the upstream code.
        if response.is_error and payload.get("code") not in (None, 0):
            raise self._remote_error(response, payload, "the Bot tenant token request")
        response.raise_for_status()
        raw_token = payload.get("tenant_access_token")
        if payload.get("code") != 0 or not isinstance(raw_token, str) or not raw_token.strip():
            raise self._remote_error(response, payload, "the Bot tenant token request")
        self._tenant_token = raw_token
        expires_in = payload.get("expire", 7200)
        ttl = int(expires_in) if isinstance(expires_in, int) else 7200
        self._token_expires_at = now + max(60, ttl - 60)
        return self._tenant_token

    @staticmethod
    def _message_content(content: Any) -> str:
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise FeishuBotError("Feishu Bot message content is not JSON serializable") from exc

    async def _send_message_payload(
        self, chat_id: str, msg_type: str, content: Mapping[str, object], *, uuid: str | None = None
    ) -> dict[str, Any]:
        if uuid is not None and (not uuid.strip() or len(uuid) > 50):
            raise FeishuBotError("Feishu message UUID must contain 1–50 characters")
        token = await self.get_tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": self._message_content(content),
                **({"uuid": uuid} if uuid is not None else {}),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected the Bot message")
        return payload

    async def send_message(
        self, chat_id: str, msg_type: str, content: Mapping[str, object]
    ) -> None:
        """Preserve existing one-way callers without requiring a returned identity."""
        await self._send_message_payload(chat_id, msg_type, content)

    async def send_message_with_identity(
        self, chat_id: str, msg_type: str, content: Mapping[str, object], *, uuid: str | None = None
    ) -> str:
        """Send a mirror using stable Feishu dedupe UUID and return exact message ID."""
        payload = await self._send_message_payload(chat_id, msg_type, content, uuid=uuid)
        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise FeishuBotError(
                "Feishu message identity is unavailable; delivery may have occurred"
            )
        return message_id

    async def patch_message_card(self, message_id: str, card: Mapping[str, object]) -> None:
        """Update one shared card sent by this same bot identity."""
        config = card.get("config")
        if not isinstance(config, Mapping) or config.get("update_multi") is not True:
            raise FeishuBotError("Updated Feishu card must be shared")
        token = await self.get_tenant_token()
        response = await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": self._message_content(card)},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected the Bot card update")

    async def send_text(self, chat_id: str, text: str) -> None:
        bounded = text.strip() or "Claude Hub completed without a text response."
        if len(bounded) > _MAX_REPLY_CHARS:
            bounded = bounded[: _MAX_REPLY_CHARS - 1] + "…"
        await self.send_message(chat_id, "text", {"text": bounded})

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: Mapping[str, object],
        *,
        access_token: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        """Reply to one exact inbound message with any supported Feishu type."""

        token = await self.get_tenant_token() if access_token is None else access_token
        if not token.strip():
            raise FeishuBotError("Feishu Bot tenant token is empty")
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": msg_type,
                "content": self._message_content(content),
                **({"reply_in_thread": True} if reply_in_thread else {}),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected the Bot reply")

    async def reply_text(
        self,
        message_id: str,
        text: str,
        *,
        access_token: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        """Reply to one exact inbound message instead of a mutable chat target."""

        bounded = text.strip() or "Claude Hub completed without a text response."
        if len(bounded) > _MAX_REPLY_CHARS:
            bounded = bounded[: _MAX_REPLY_CHARS - 1] + "…"
        await self.reply_message(
            message_id,
            "text",
            {"text": bounded},
            access_token=access_token,
            reply_in_thread=reply_in_thread,
        )

    async def add_reaction(
        self, message_id: str, emoji_type: str, *, access_token: str | None = None
    ) -> str:
        """Add a reaction and return its id for an exact later deletion."""

        token = await self.get_tenant_token() if access_token is None else access_token
        if not token.strip():
            raise FeishuBotError("Feishu Bot tenant token is empty")
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reactions",
            headers={"Authorization": f"Bearer {token}"},
            json={"reaction_type": {"emoji_type": emoji_type}},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        reaction_id = data.get("reaction_id") if isinstance(data, dict) else None
        if payload.get("code") != 0 or not isinstance(reaction_id, str) or not reaction_id.strip():
            raise FeishuBotError("Feishu rejected the Bot reaction")
        return reaction_id

    async def delete_reaction(
        self,
        message_id: str,
        reaction_id: str,
        *,
        access_token: str | None = None,
    ) -> None:
        token = await self.get_tenant_token() if access_token is None else access_token
        if not token.strip():
            raise FeishuBotError("Feishu Bot tenant token is empty")
        response = await self._request(
            "DELETE",
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reactions/"
            f"{quote(reaction_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected deleting the Bot reaction")

    async def get_bot_profile(self) -> FeishuBotProfile:
        """Return the authenticated Bot's Feishu-managed name and avatar."""

        token = await self.get_tenant_token()
        response = await self._request(
            "GET",
            "/open-apis/bot/v3/info/",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        bot = payload.get("bot") if isinstance(payload, dict) else None
        name = bot.get("app_name") if isinstance(bot, dict) else None
        avatar_url = bot.get("avatar_url") if isinstance(bot, dict) else None
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        if payload.get("code") != 0 or not isinstance(name, str) or not name.strip():
            raise FeishuBotError("Feishu rejected the Bot info request")
        if avatar_url is not None and (
            not isinstance(avatar_url, str)
            or not avatar_url.strip()
            or not avatar_url.startswith("https://")
        ):
            avatar_url = None
        if open_id is not None and (not isinstance(open_id, str) or not open_id.strip()):
            open_id = None
        return FeishuBotProfile(
            name=name.strip(),
            avatar_url=avatar_url.strip() if isinstance(avatar_url, str) else None,
            open_id=open_id.strip() if isinstance(open_id, str) else None,
        )

    async def validate_credentials(self) -> FeishuBotProfile:
        """Validate credentials and return the authenticated public identity."""

        return await self.get_bot_profile()

    async def download_message_image(self, message_id: str, image_key: str) -> bytes:
        """Download and validate an image embedded in one inbound message."""

        token = await self.get_tenant_token()
        path = (
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/resources/"
            f"{quote(image_key, safe='')}"
        )
        if self._client is not None:
            async with self._client.stream(
                "GET",
                path,
                params={"type": "image"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                return await self._read_message_image(response)
        async with create_feishu_http_client(self._config.api_base_url, timeout=15.0) as client:
            async with client.stream(
                "GET",
                path,
                params={"type": "image"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                return await self._read_message_image(response)

    @staticmethod
    async def _read_message_image(response: httpx.Response) -> bytes:
        """Read at most the accepted body size before validating its magic."""

        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        prefixes = _IMAGE_MIME_PREFIXES.get(mime_type)
        if prefixes is None:
            raise FeishuBotError("Feishu returned an invalid message image")
        raw_length = response.headers.get("content-length")
        try:
            content_length = int(raw_length) if raw_length is not None else None
        except ValueError:
            content_length = None
        if content_length is not None and (
            content_length <= 0 or content_length > _MAX_INBOUND_IMAGE_BYTES
        ):
            raise FeishuBotError("Feishu returned an invalid message image")
        content_buffer = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            remaining = _MAX_INBOUND_IMAGE_BYTES + 1 - len(content_buffer)
            content_buffer.extend(chunk[:remaining])
            if len(content_buffer) > _MAX_INBOUND_IMAGE_BYTES:
                raise FeishuBotError("Feishu returned an invalid message image")
        content = bytes(content_buffer)
        valid_magic = bool(prefixes and any(content.startswith(prefix) for prefix in prefixes))
        if mime_type == "image/webp":
            valid_magic = content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        if not content or not valid_magic:
            raise FeishuBotError("Feishu returned an invalid message image")
        return content


_MAX_FEISHU_MESSAGE_TIME_MS = 4_102_444_800_000
_MAX_DEDUP_RETRY_ATTEMPTS = 2
_PRE_DISPATCH_RETRY_WINDOW_SECONDS = 10 * 60
_DEDUP_RETRYABLE_REASONS = frozenset({"message_future_over_skew"})


class FeishuMessageTimeStatus(str, Enum):
    VALID = "valid"
    INVALID_TIMESTAMP = "invalid_timestamp"
    CLOCK_INVERTED = "clock_inverted"
    MESSAGE_BEFORE_BINDING = "message_before_binding"
    MESSAGE_FUTURE_OVER_SKEW = "message_future_over_skew"


def feishu_message_time_notice(status: FeishuMessageTimeStatus) -> str | None:
    """Explain host-clock failures without replying to stale or malformed input."""
    if status == FeishuMessageTimeStatus.MESSAGE_FUTURE_OVER_SKEW:
        return (
            "Claude Hub 尚未执行这条消息：飞书消息时间晚于服务器时间且超出允许偏差。"
            "如稍后仍未收到回复，请重新发送。"
        )
    if status == FeishuMessageTimeStatus.CLOCK_INVERTED:
        return "Claude Hub 尚未执行这条消息：服务器时间校验异常。请稍后重新发送。"
    return None


def feishu_message_time_status(
    created_at_ms: int,
    *,
    activated_at: float,
    now: float,
    future_skew_seconds: float = 0.0,
) -> FeishuMessageTimeStatus:
    """Classify an inbound Feishu message timestamp against binding time."""
    if (
        isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or not 0 <= created_at_ms <= _MAX_FEISHU_MESSAGE_TIME_MS
    ):
        return FeishuMessageTimeStatus.INVALID_TIMESTAMP
    ratios: list[tuple[int, int]] = []
    for value in (activated_at, now):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return FeishuMessageTimeStatus.INVALID_TIMESTAMP
        try:
            seconds = float(value)
        except OverflowError:
            return FeishuMessageTimeStatus.INVALID_TIMESTAMP
        if not math.isfinite(seconds) or not 0 <= seconds <= _MAX_FEISHU_MESSAGE_TIME_MS / 1000:
            return FeishuMessageTimeStatus.INVALID_TIMESTAMP
        ratios.append(seconds.as_integer_ratio())
    if isinstance(future_skew_seconds, bool) or not isinstance(future_skew_seconds, (int, float)):
        return FeishuMessageTimeStatus.INVALID_TIMESTAMP
    try:
        skew = float(future_skew_seconds)
    except OverflowError:
        return FeishuMessageTimeStatus.INVALID_TIMESTAMP
    if not math.isfinite(skew) or skew < 0:
        return FeishuMessageTimeStatus.INVALID_TIMESTAMP
    (lower_n, lower_d), (upper_n, upper_d) = ratios
    if lower_n * upper_d > upper_n * lower_d:
        return FeishuMessageTimeStatus.CLOCK_INVERTED
    if created_at_ms * lower_d < lower_n * 1000:
        return FeishuMessageTimeStatus.MESSAGE_BEFORE_BINDING
    # Keep the host clock's exact bound. The configured allowance uses its
    # decimal value, so 0.152 means 152 ms without an arbitrary epsilon.
    upper_ms = 1000 * (Fraction(upper_n, upper_d) + Fraction(str(skew)))
    if created_at_ms > upper_ms:
        return FeishuMessageTimeStatus.MESSAGE_FUTURE_OVER_SKEW
    return FeishuMessageTimeStatus.VALID


def feishu_message_time_is_valid(created_at_ms: int, *, activated_at: float, now: float) -> bool:
    """Check exact UTC bounds without rounding activation down to milliseconds.

    Host and Feishu clocks must be normally synchronized. This rejects
    observable future/inverted times, not every unobserved wall-clock jump.
    """
    return (
        feishu_message_time_status(created_at_ms, activated_at=activated_at, now=now)
        == FeishuMessageTimeStatus.VALID
    )


_MAX_DEDUP_TIMESTAMP = 4_102_444_800.0


def _dedup_timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeishuBotError("Invalid Feishu message claim timestamp")
    try:
        number = float(value)
    except OverflowError as exc:
        raise FeishuBotError("Invalid Feishu message claim timestamp") from exc
    if not math.isfinite(number) or not 0.0 <= number <= _MAX_DEDUP_TIMESTAMP:
        raise FeishuBotError("Invalid Feishu message claim timestamp")
    return number


def _dedup_message_id(key: str) -> str:
    if not isinstance(key, str):
        raise FeishuBotError("Invalid namespaced Feishu message key")
    bot_id, separator, message_id = key.partition(":")
    if not separator or not bot_id or not message_id:
        raise FeishuBotError("Invalid namespaced Feishu message key")
    return message_id


_RETRY_CONTEXT_KEYS = frozenset(
    {
        "bot_id",
        "pairing_id",
        "tab_id",
        "workspace_id",
        "chat_id",
        "sender_open_id",
        "message_id",
        "event_fingerprint",
    }
)


def _validate_retry_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not _RETRY_CONTEXT_KEYS.issubset(value):
        raise FeishuBotError("Invalid Feishu message retry context")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise FeishuBotError("Invalid Feishu message retry context")
        if not isinstance(item, str) or not item.strip():
            raise FeishuBotError("Invalid Feishu message retry context")
    return value


def _same_retry_context(left: Mapping[str, str] | None, right: Mapping[str, str] | None) -> bool:
    return bool(left is not None and right is not None and dict(left) == dict(right))


def _pre_dispatch_retry_allowed(
    record: Mapping[str, Any], *, now: float, retry_context: Mapping[str, str] | None
) -> bool:
    if record.get("status") != "pre_dispatch_rejected":
        return False
    if record.get("stage") != "pre_dispatch":
        return False
    if record.get("reason") not in _DEDUP_RETRYABLE_REASONS:
        return False
    attempts = record.get("attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        return False
    if attempts >= _MAX_DEDUP_RETRY_ATTEMPTS:
        return False
    finished_at = record.get("finished_at")
    if not isinstance(finished_at, (int, float)) or isinstance(finished_at, bool):
        return False
    if not 0 <= now - float(finished_at) <= _PRE_DISPATCH_RETRY_WINDOW_SECONDS:
        return False
    stored_context = record.get("retry_context")
    if stored_context is not None:
        _validate_retry_context(stored_context)
    return _same_retry_context(stored_context, retry_context)


def _validate_dedup_events(value: Any, *, namespaced: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FeishuBotError("Invalid Feishu message claims")
    for key, record in value.items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            raise FeishuBotError("Invalid Feishu message claim record")
        if namespaced:
            _dedup_message_id(key)
        _dedup_timestamp(record.get("claimed_at"))
        status = record.get("status")
        if not isinstance(status, str) or not status.strip():
            raise FeishuBotError("Invalid Feishu message claim status")
        if "finished_at" in record:
            _dedup_timestamp(record["finished_at"])
        attempts = record.get("attempts", 1)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise FeishuBotError("Invalid Feishu message claim attempts")
        for field in ("stage", "reason"):
            raw = record.get(field)
            if raw is not None and (not isinstance(raw, str) or not raw.strip()):
                raise FeishuBotError("Invalid Feishu message claim metadata")
        if "retry_context" in record:
            _validate_retry_context(record["retry_context"])
    return value


class FeishuMessageDedupStore:
    """At-most-once claims for inbound messages, keyed per Bot.

    Deduplication state is separate from credentials because it is written on
    every inbound message. Invalid authority records are never pruned into a
    fresh claim, including records whose timestamps appear to have expired.
    """

    def __init__(self, path: Path | None = None, now: Callable[[], float] = time.time) -> None:
        self.path = path or (resolve_runtime_home() / "feishu_bot.json")
        self._now = now
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 2, "events": {}, "legacy_events": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError also covers invalid UTF-8 and JSON integer limits.
            raise FeishuBotError("Cannot read Feishu message deduplication state") from exc
        if not isinstance(value, dict):
            raise FeishuBotError("Unsupported Feishu message deduplication state")
        version = value.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
            raise FeishuBotError("Unsupported Feishu message deduplication state")
        events = _validate_dedup_events(value.get("events"), namespaced=version == 2)
        if version == 1:
            # Legacy IDs have no trustworthy Bot identity. Preserve their
            # original claim times in the bounded compatibility namespace.
            return {"version": 2, "events": {}, "legacy_events": events}
        legacy = _validate_dedup_events(value.get("legacy_events", {}), namespaced=False)
        return {"version": 2, "events": events, "legacy_events": legacy}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as file_handle:
                os.chmod(temporary, 0o600)
                json.dump(state, file_handle, sort_keys=True, separators=(",", ":"))
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def claim(self, key: str, *, retry_context: Mapping[str, str] | None = None) -> bool:
        message_id = _dedup_message_id(key)
        now = _dedup_timestamp(self._now())
        if retry_context is not None:
            _validate_retry_context(dict(retry_context))
        with self._lock:
            state = self._load()
            cutoff = now - _EVENT_RETENTION_SECONDS
            events = {
                item: record
                for item, record in state["events"].items()
                if record["claimed_at"] >= cutoff
            }
            state["events"] = events
            legacy_cutoff = now - _LEGACY_CLAIM_RETENTION_SECONDS
            state["legacy_events"] = {
                item: record
                for item, record in state["legacy_events"].items()
                if record["claimed_at"] >= legacy_cutoff
            }
            if message_id in state["legacy_events"]:
                self._save(state)
                return False
            existing = events.get(key)
            if existing is not None:
                if _pre_dispatch_retry_allowed(existing, now=now, retry_context=retry_context):
                    existing.update(
                        claimed_at=now,
                        status="processing",
                        stage="intake",
                        reason="retry_after_pre_dispatch_reject",
                        attempts=int(existing.get("attempts", 1)) + 1,
                    )
                    existing.pop("finished_at", None)
                    self._save(state)
                    return True
                self._save(state)
                return False
            events[key] = {"claimed_at": now, "status": "processing"}
            self._save(state)
            return True

    def finish(
        self,
        key: str,
        status: str,
        *,
        stage: str | None = None,
        reason: str | None = None,
        retry_context: Mapping[str, str] | None = None,
    ) -> None:
        _dedup_message_id(key)
        if not isinstance(status, str) or not status.strip():
            raise FeishuBotError("Invalid Feishu message claim status")
        for value in (stage, reason):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise FeishuBotError("Invalid Feishu message claim metadata")
        if retry_context is not None:
            retry_context = _validate_retry_context(dict(retry_context))
        with self._lock:
            state = self._load()
            value = state["events"].get(key)
            if value is not None:
                value["status"] = status
                if stage is not None:
                    value["stage"] = stage
                if reason is not None:
                    value["reason"] = reason
                else:
                    value.pop("reason", None)
                if retry_context is not None:
                    value["retry_context"] = dict(retry_context)
                value["finished_at"] = _dedup_timestamp(self._now())
                self._save(state)

    def release(self, key: str) -> None:
        """Release a cancelled claim after its native Chat turn is stopped."""

        _dedup_message_id(key)
        with self._lock:
            state = self._load()
            if state["events"].pop(key, None) is not None:
                self._save(state)


__all__ = [
    "FeishuMessageDedupStore",
    "FeishuBinding",
    "FeishuBotClient",
    "FeishuBotConfig",
    "FeishuBotConfigurationError",
    "FeishuBotProfile",
    "FeishuBotError",
    "FeishuMessageEvent",
    "FeishuMessageTimeStatus",
    "feishu_message_time_is_valid",
    "feishu_message_time_notice",
    "feishu_message_time_status",
]
