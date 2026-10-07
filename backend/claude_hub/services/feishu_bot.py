"""Feishu callback verification, outbound API client and persistent deduplication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
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
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_hub.services.runtime_isolation import resolve_runtime_home

_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_LEGACY_CLAIM_RETENTION_SECONDS = 60 * 60
MAX_FEISHU_EVENT_BYTES = 256 * 1024
_MAX_INPUT_CHARS = 4_000
_MAX_REPLY_CHARS = 20_000
_EVENT_MAX_AGE_SECONDS = 300


class FeishuBotError(RuntimeError):
    """Base error for a safe, expected Feishu Bot failure."""


class FeishuBotConfigurationError(FeishuBotError):
    """Raised when required Bot settings are absent or inconsistent."""


class FeishuEventVerificationError(FeishuBotError):
    """Raised when an inbound callback cannot be authenticated."""


class FeishuEventPayloadError(FeishuBotError):
    """Raised when an authenticated callback has an unsupported payload."""


@dataclass(frozen=True)
class FeishuBotConfig:
    """Explicit environment configuration for one Feishu Bot application."""

    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str | None = None
    api_base_url: str = "https://open.feishu.cn"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeishuBotConfig":
        env = os.environ if environ is None else environ
        values = {
            "app_id": env.get("CLAUDE_HUB_FEISHU_BOT_APP_ID", "").strip(),
            "app_secret": env.get("CLAUDE_HUB_FEISHU_BOT_APP_SECRET", "").strip(),
            "verification_token": env.get("CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise FeishuBotConfigurationError(
                "Feishu Bot is not configured; missing explicit environment values: "
                + ", ".join(missing)
            )
        base_url = env.get("CLAUDE_HUB_FEISHU_API_BASE_URL", "https://open.feishu.cn").rstrip("/")
        encrypt_key = env.get("CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY", "").strip() or None
        return cls(api_base_url=base_url, encrypt_key=encrypt_key, **values)


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


def _parse_json_object(raw_body: bytes) -> dict[str, Any]:
    if len(raw_body) > MAX_FEISHU_EVENT_BYTES:
        raise FeishuEventPayloadError(f"Feishu event exceeds {MAX_FEISHU_EVENT_BYTES} byte limit")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuEventPayloadError("Feishu event body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FeishuEventPayloadError("Feishu event body must be a JSON object")
    return payload


def _verify_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    encrypt_key: str,
    now: float,
) -> None:
    try:
        timestamp = int(headers.get("x-lark-request-timestamp", ""))
    except ValueError as exc:
        raise FeishuEventVerificationError("Invalid Feishu request timestamp") from exc
    nonce = headers.get("x-lark-request-nonce", "")
    signature = headers.get("x-lark-signature", "")
    if not nonce or not signature:
        raise FeishuEventVerificationError("Missing Feishu request signature headers")
    if abs(now - timestamp) > _EVENT_MAX_AGE_SECONDS:
        raise FeishuEventVerificationError("Feishu request timestamp is outside replay window")
    signed = str(timestamp).encode() + nonce.encode() + encrypt_key.encode() + raw_body
    expected = hashlib.sha256(signed).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise FeishuEventVerificationError("Invalid Feishu request signature")


def _decrypt_callback(encrypted_value: Any, encrypt_key: str) -> bytes:
    if not isinstance(encrypted_value, str) or not encrypted_value:
        raise FeishuEventPayloadError("Encrypted Feishu event is missing ciphertext")
    try:
        ciphertext = base64.b64decode(encrypted_value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FeishuEventVerificationError("Encrypted Feishu event is not valid base64") from exc
    if len(ciphertext) < 32 or len(ciphertext) % 16 != 0:
        raise FeishuEventVerificationError("Encrypted Feishu event has invalid ciphertext length")
    iv, encrypted_body = ciphertext[:16], ciphertext[16:]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(encrypted_body) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise FeishuEventVerificationError("Encrypted Feishu event has invalid padding") from exc
    plaintext = bytes(plaintext)
    if len(plaintext) > MAX_FEISHU_EVENT_BYTES:
        raise FeishuEventPayloadError(f"Feishu event exceeds {MAX_FEISHU_EVENT_BYTES} byte limit")
    return plaintext


def _event_created_seconds(value: Any) -> float:
    try:
        timestamp = int(str(value))
    except ValueError as exc:
        raise FeishuEventVerificationError("Invalid Feishu event create_time") from exc
    if timestamp >= 100_000_000_000_000:
        return timestamp / 1_000_000
    if timestamp >= 100_000_000_000:
        return timestamp / 1_000
    return float(timestamp)


_MAX_FEISHU_MESSAGE_TIME_MS = 4_102_444_800_000


def _message_created_millis(value: Any) -> int:
    # The event schema defines this field as a decimal string in milliseconds.
    # Never infer units or replace it with the event header/receipt time.
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 13
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise FeishuEventPayloadError("Invalid Feishu message create_time")
    timestamp = int(value)
    if timestamp > _MAX_FEISHU_MESSAGE_TIME_MS:
        raise FeishuEventPayloadError("Invalid Feishu message create_time")
    return timestamp


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


def parse_feishu_callback(
    raw_body: bytes,
    headers: Mapping[str, str],
    config: FeishuBotConfig,
    now: float | None = None,
) -> tuple[str, str | FeishuMessageEvent]:
    """Authenticate and parse URL verification or a p2p text message event."""

    outer_payload = _parse_json_object(raw_body)
    current_time = time.time() if now is None else now
    encrypted_value = outer_payload.get("encrypt")
    if encrypted_value is not None:
        if config.encrypt_key is None:
            raise FeishuEventPayloadError("Encrypted Feishu event received without an Encrypt Key")
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        _verify_signature(raw_body, normalized_headers, config.encrypt_key, current_time)
        payload = _parse_json_object(_decrypt_callback(encrypted_value, config.encrypt_key))
    else:
        if config.encrypt_key is not None:
            raise FeishuEventPayloadError(
                "Plaintext Feishu event is not accepted while an Encrypt Key is configured"
            )
        payload = outer_payload

    if payload.get("type") == "url_verification":
        token = payload.get("token")
        challenge = payload.get("challenge")
        if not isinstance(token, str) or not hmac.compare_digest(token, config.verification_token):
            raise FeishuEventVerificationError("Invalid Feishu verification token")
        if not isinstance(challenge, str) or not challenge:
            raise FeishuEventPayloadError("Feishu challenge is missing")
        return "challenge", challenge

    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise FeishuEventPayloadError("Feishu event header or event body is missing")
    token = header.get("token")
    if not isinstance(token, str) or not hmac.compare_digest(token, config.verification_token):
        raise FeishuEventVerificationError("Invalid Feishu verification token")
    if header.get("event_type") != "im.message.receive_v1":
        raise FeishuEventPayloadError("Unsupported Feishu event type")
    app_id = header.get("app_id")
    if not isinstance(app_id, str) or not hmac.compare_digest(app_id, config.app_id):
        raise FeishuEventVerificationError("Feishu event app_id does not match this Bot")
    create_seconds = _event_created_seconds(header.get("create_time", ""))
    if abs(current_time - create_seconds) > _EVENT_MAX_AGE_SECONDS:
        raise FeishuEventVerificationError("Feishu event is outside replay window")

    sender = event.get("sender")
    message = event.get("message")
    if not isinstance(sender, dict) or not isinstance(message, dict):
        raise FeishuEventPayloadError("Feishu sender or message is missing")
    message_created_at_ms = _message_created_millis(message.get("create_time"))
    if not feishu_message_time_is_valid(message_created_at_ms, activated_at=0.0, now=current_time):
        raise FeishuEventVerificationError(
            "Feishu message timestamp is outside the trusted time range"
        )
    sender_id = sender.get("sender_id")
    if sender.get("sender_type") != "user" or not isinstance(sender_id, dict):
        raise FeishuEventPayloadError("Only Feishu user messages are supported")
    sender_open_id = sender_id.get("open_id")
    if not isinstance(sender_open_id, str) or not sender_open_id:
        raise FeishuEventPayloadError("Feishu sender open_id is missing")
    if message.get("chat_type") != "p2p":
        raise FeishuEventPayloadError("Only Feishu p2p messages are supported")
    if message.get("message_type") != "text":
        raise FeishuEventPayloadError("Only Feishu text messages are supported")
    try:
        content = json.loads(message.get("content", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise FeishuEventPayloadError("Feishu text message content is invalid") from exc
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise FeishuEventPayloadError("Feishu text message is empty")
    text = text.strip()
    if len(text) > _MAX_INPUT_CHARS:
        raise FeishuEventPayloadError(
            f"Feishu text message exceeds {_MAX_INPUT_CHARS} character limit"
        )
    event_id = header.get("event_id")
    message_id = message.get("message_id")
    chat_id = message.get("chat_id")
    if not isinstance(event_id, str) or not event_id:
        raise FeishuEventPayloadError("Feishu event_id is missing")
    if not isinstance(message_id, str) or not message_id:
        raise FeishuEventPayloadError("Feishu message_id is missing")
    if not isinstance(chat_id, str) or not chat_id:
        raise FeishuEventPayloadError("Feishu chat_id is missing")
    return "message", FeishuMessageEvent(
        event_id=event_id,
        message_id=message_id,
        app_id=app_id,
        sender_open_id=sender_open_id,
        chat_id=chat_id,
        text=text,
        message_created_at_ms=message_created_at_ms,
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
    "FeishuEventPayloadError",
    "FeishuEventVerificationError",
    "FeishuMessageEvent",
    "MAX_FEISHU_EVENT_BYTES",
    "feishu_message_time_is_valid",
    "parse_feishu_callback",
]
