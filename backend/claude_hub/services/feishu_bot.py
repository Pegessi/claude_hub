"""Feishu Bot credentials, outbound API client and persistent deduplication."""

from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import httpx

from claude_hub.services.runtime_isolation import resolve_runtime_home

_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_LEGACY_CLAIM_RETENTION_SECONDS = 60 * 60
_MAX_REPLY_CHARS = 20_000


class FeishuBotError(RuntimeError):
    """Base error for a safe, expected Feishu Bot failure."""


class FeishuBotConfigurationError(FeishuBotError):
    """Raised when required Bot settings are absent or inconsistent."""


@dataclass(frozen=True)
class FeishuBotConfig:
    """Credentials for one Feishu Bot long connection."""

    app_id: str
    app_secret: str
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
    """Validated text message fields from ``im.message.receive_v1``."""

    event_id: str
    message_id: str
    app_id: str
    sender_open_id: str
    chat_id: str
    text: str
    message_created_at_ms: int


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
        async with httpx.AsyncClient(base_url=self._config.api_base_url, timeout=15.0) as client:
            return await client.request(method, path, **kwargs)

    async def get_tenant_token(self) -> str:
        now = time.monotonic()
        if self._tenant_token and now < self._token_expires_at:
            return self._tenant_token
        response = await self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        raw_token = payload.get("tenant_access_token")
        if payload.get("code") != 0 or not isinstance(raw_token, str) or not raw_token.strip():
            raise FeishuBotError("Feishu rejected the Bot tenant token request")
        self._tenant_token = raw_token
        expires_in = payload.get("expire", 7200)
        ttl = int(expires_in) if isinstance(expires_in, int) else 7200
        self._token_expires_at = now + max(60, ttl - 60)
        return self._tenant_token

    async def send_text(self, chat_id: str, text: str) -> None:
        token = await self.get_tenant_token()
        bounded = text.strip() or "Claude Hub completed without a text response."
        if len(bounded) > _MAX_REPLY_CHARS:
            bounded = bounded[: _MAX_REPLY_CHARS - 1] + "…"
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": bounded}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected the Bot message")

    async def reply_text(
        self, message_id: str, text: str, *, access_token: str | None = None
    ) -> None:
        """Reply to one exact inbound message instead of a mutable chat target."""

        token = await self.get_tenant_token() if access_token is None else access_token
        if not token.strip():
            raise FeishuBotError("Feishu Bot tenant token is empty")
        bounded = text.strip() or "Claude Hub completed without a text response."
        if len(bounded) > _MAX_REPLY_CHARS:
            bounded = bounded[: _MAX_REPLY_CHARS - 1] + "…"
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "text",
                "content": json.dumps({"text": bounded}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuBotError("Feishu rejected the Bot reply")

    async def validate_credentials(self) -> None:
        """Validate app credentials without returning or persisting the token."""

        await self.get_tenant_token()


_MAX_FEISHU_MESSAGE_TIME_MS = 4_102_444_800_000


def feishu_message_time_is_valid(created_at_ms: int, *, activated_at: float, now: float) -> bool:
    """Check exact UTC bounds without rounding activation down to milliseconds.

    Host and Feishu clocks must be normally synchronized. This rejects
    observable future/inverted times, not every unobserved wall-clock jump.
    """
    if (
        isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or not 0 <= created_at_ms <= _MAX_FEISHU_MESSAGE_TIME_MS
    ):
        return False
    ratios: list[tuple[int, int]] = []
    for value in (activated_at, now):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            seconds = float(value)
        except OverflowError:
            return False
        if not math.isfinite(seconds) or not 0 <= seconds <= _MAX_FEISHU_MESSAGE_TIME_MS / 1000:
            return False
        ratios.append(seconds.as_integer_ratio())
    (lower_n, lower_d), (upper_n, upper_d) = ratios
    return created_at_ms * lower_d >= lower_n * 1000 and created_at_ms * upper_d <= upper_n * 1000


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

    def claim(self, key: str) -> bool:
        message_id = _dedup_message_id(key)
        now = _dedup_timestamp(self._now())
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
            if key in events or message_id in state["legacy_events"]:
                self._save(state)
                return False
            events[key] = {"claimed_at": now, "status": "processing"}
            self._save(state)
            return True

    def finish(self, key: str, status: str) -> None:
        _dedup_message_id(key)
        if not isinstance(status, str) or not status.strip():
            raise FeishuBotError("Invalid Feishu message claim status")
        with self._lock:
            state = self._load()
            value = state["events"].get(key)
            if value is not None:
                value["status"] = status
                value["finished_at"] = _dedup_timestamp(self._now())
                self._save(state)


__all__ = [
    "FeishuMessageDedupStore",
    "FeishuBinding",
    "FeishuBotClient",
    "FeishuBotConfig",
    "FeishuBotConfigurationError",
    "FeishuBotError",
    "FeishuMessageEvent",
    "feishu_message_time_is_valid",
]
