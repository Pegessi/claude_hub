"""Persistent Feishu Bot binding state and outbound API client."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import string
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from claude_hub.config import settings
from claude_hub.services.runtime_isolation import resolve_runtime_home

_BIND_CODE_ALPHABET = string.ascii_uppercase + string.digits
_BIND_CODE_PREFIX = "CH-"
_BIND_CODE_LENGTH = 10
_DEFAULT_BIND_CODE_TTL_SECONDS = 600
_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_EVENT_BYTES = 256 * 1024
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


class BindingCodeError(FeishuBotError):
    """Raised when a binding code is invalid, expired, replayed, or mismatched."""


@dataclass(frozen=True)
class FeishuBotConfig:
    """Explicit environment configuration for one Feishu Bot application."""

    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    api_base_url: str = "https://open.feishu.cn"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FeishuBotConfig":
        env = os.environ if environ is None else environ
        values = {
            "app_id": env.get("CLAUDE_HUB_FEISHU_BOT_APP_ID", "").strip(),
            "app_secret": env.get("CLAUDE_HUB_FEISHU_BOT_APP_SECRET", "").strip(),
            "verification_token": env.get("CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN", "").strip(),
            "encrypt_key": env.get("CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise FeishuBotConfigurationError(
                "Feishu Bot is not configured; missing explicit environment values: "
                + ", ".join(missing)
            )
        oauth_app_id = settings.feishu_app_id
        if oauth_app_id and values["app_id"] != oauth_app_id:
            raise FeishuBotConfigurationError(
                "Feishu Bot and Web login must use the same app_id so open_id identity matches"
            )
        base_url = env.get("CLAUDE_HUB_FEISHU_API_BASE_URL", "https://open.feishu.cn").rstrip("/")
        return cls(api_base_url=base_url, **values)


@dataclass(frozen=True)
class FeishuBinding:
    """An authorized p2p conversation bound to one existing Hub Chat tab."""

    owner_open_id: str
    sender_open_id: str
    app_id: str
    chat_id: str
    tab_id: str
    workspace_id: str | None
    created_at: float


@dataclass(frozen=True)
class PendingBinding:
    """A single-use browser-issued binding request."""

    owner_open_id: str
    tab_id: str
    workspace_id: str | None
    expires_at: float
    consumed_at: float | None = None


@dataclass(frozen=True)
class FeishuMessageEvent:
    """Validated text message fields from ``im.message.receive_v1``."""

    event_id: str
    message_id: str
    app_id: str
    sender_open_id: str
    chat_id: str
    text: str


class FeishuBindingStore:
    """Atomic JSON store for codes, bindings, and inbound message deduplication."""

    def __init__(self, path: Path | None = None, now: Callable[[], float] = time.time) -> None:
        self.path = path or (resolve_runtime_home() / "feishu_bot.json")
        self._now = now
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "pending": {}, "bindings": {}, "events": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeishuBotError(f"Cannot read Feishu Bot state at {self.path}") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise FeishuBotError(f"Unsupported Feishu Bot state at {self.path}")
        for key in ("pending", "bindings", "events"):
            if not isinstance(value.get(key), dict):
                raise FeishuBotError(f"Invalid Feishu Bot state field {key!r} at {self.path}")
        return value

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

    @staticmethod
    def _code_digest(code: str) -> str:
        return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()

    def create_code(
        self,
        owner_open_id: str,
        tab_id: str,
        workspace_id: str | None,
        ttl_seconds: int = _DEFAULT_BIND_CODE_TTL_SECONDS,
    ) -> tuple[str, PendingBinding]:
        code = _BIND_CODE_PREFIX + "".join(
            secrets.choice(_BIND_CODE_ALPHABET) for _ in range(_BIND_CODE_LENGTH)
        )
        pending = PendingBinding(
            owner_open_id=owner_open_id,
            tab_id=tab_id,
            workspace_id=workspace_id,
            expires_at=self._now() + ttl_seconds,
        )
        with self._lock:
            state = self._load()
            for digest, value in list(state["pending"].items()):
                if value.get("owner_open_id") == owner_open_id and value.get("consumed_at") is None:
                    del state["pending"][digest]
            state["pending"][self._code_digest(code)] = asdict(pending)
            self._save(state)
        return code, pending

    def consume_code(
        self,
        code: str,
        *,
        sender_open_id: str,
        app_id: str,
        chat_id: str,
    ) -> FeishuBinding:
        digest = self._code_digest(code)
        with self._lock:
            state = self._load()
            value = state["pending"].get(digest)
            if not isinstance(value, dict):
                raise BindingCodeError("Binding code is invalid")
            pending = PendingBinding(**value)
            if pending.consumed_at is not None:
                raise BindingCodeError("Binding code has already been used")
            if self._now() > pending.expires_at:
                raise BindingCodeError("Binding code has expired")
            if not hmac.compare_digest(pending.owner_open_id, sender_open_id):
                raise BindingCodeError("Binding code belongs to a different Feishu user")
            binding = FeishuBinding(
                owner_open_id=pending.owner_open_id,
                sender_open_id=sender_open_id,
                app_id=app_id,
                chat_id=chat_id,
                tab_id=pending.tab_id,
                workspace_id=pending.workspace_id,
                created_at=self._now(),
            )
            state["pending"][digest]["consumed_at"] = self._now()
            state["bindings"][binding.owner_open_id] = asdict(binding)
            self._save(state)
            return binding

    def get_owner_binding(self, owner_open_id: str) -> FeishuBinding | None:
        with self._lock:
            value = self._load()["bindings"].get(owner_open_id)
        return FeishuBinding(**value) if isinstance(value, dict) else None

    def get_sender_binding(
        self, sender_open_id: str, app_id: str, chat_id: str
    ) -> FeishuBinding | None:
        binding = self.get_owner_binding(sender_open_id)
        if binding is None:
            return None
        if not hmac.compare_digest(binding.sender_open_id, sender_open_id):
            return None
        if not hmac.compare_digest(binding.app_id, app_id):
            return None
        if not hmac.compare_digest(binding.chat_id, chat_id):
            return None
        return binding

    def delete_owner_binding(self, owner_open_id: str) -> bool:
        with self._lock:
            state = self._load()
            removed = state["bindings"].pop(owner_open_id, None) is not None
            if removed:
                self._save(state)
            return removed

    def claim_message(self, message_id: str) -> bool:
        """Persist an at-most-once claim, using the official message deduplication key."""

        now = self._now()
        with self._lock:
            state = self._load()
            cutoff = now - _EVENT_RETENTION_SECONDS
            state["events"] = {
                key: value
                for key, value in state["events"].items()
                if isinstance(value, dict) and float(value.get("claimed_at", 0)) >= cutoff
            }
            if message_id in state["events"]:
                self._save(state)
                return False
            state["events"][message_id] = {"claimed_at": now, "status": "processing"}
            self._save(state)
            return True

    def finish_message(self, message_id: str, status: str) -> None:
        with self._lock:
            state = self._load()
            value = state["events"].get(message_id)
            if isinstance(value, dict):
                value["status"] = status
                value["finished_at"] = self._now()
                self._save(state)


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

    async def _get_tenant_token(self) -> str:
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
        if payload.get("code") != 0 or not isinstance(payload.get("tenant_access_token"), str):
            raise FeishuBotError("Feishu rejected the Bot tenant token request")
        self._tenant_token = payload["tenant_access_token"]
        expires_in = payload.get("expire", 7200)
        ttl = int(expires_in) if isinstance(expires_in, int) else 7200
        self._token_expires_at = now + max(60, ttl - 60)
        return self._tenant_token

    async def send_text(self, chat_id: str, text: str) -> None:
        token = await self._get_tenant_token()
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


def _parse_json_object(raw_body: bytes) -> dict[str, Any]:
    if len(raw_body) > _MAX_EVENT_BYTES:
        raise FeishuEventPayloadError(f"Feishu event exceeds {_MAX_EVENT_BYTES} byte limit")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuEventPayloadError("Feishu event body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FeishuEventPayloadError("Feishu event body must be a JSON object")
    if "encrypt" in payload:
        raise FeishuEventPayloadError("Encrypted Feishu events are not supported")
    return payload


def _verify_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    config: FeishuBotConfig,
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
    signed = str(timestamp).encode() + nonce.encode() + config.encrypt_key.encode() + raw_body
    expected = hashlib.sha256(signed).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise FeishuEventVerificationError("Invalid Feishu request signature")


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


def parse_feishu_callback(
    raw_body: bytes,
    headers: Mapping[str, str],
    config: FeishuBotConfig,
    now: float | None = None,
) -> tuple[str, str | FeishuMessageEvent]:
    """Authenticate and parse URL verification or a p2p text message event."""

    payload = _parse_json_object(raw_body)
    current_time = time.time() if now is None else now

    if payload.get("type") == "url_verification":
        token = payload.get("token")
        challenge = payload.get("challenge")
        if not isinstance(token, str) or not hmac.compare_digest(token, config.verification_token):
            raise FeishuEventVerificationError("Invalid Feishu verification token")
        if not isinstance(challenge, str) or not challenge:
            raise FeishuEventPayloadError("Feishu challenge is missing")
        return "challenge", challenge

    normalized_headers = {key.lower(): value for key, value in headers.items()}
    _verify_signature(raw_body, normalized_headers, config, current_time)
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
    )


__all__ = [
    "BindingCodeError",
    "FeishuBinding",
    "FeishuBindingStore",
    "FeishuBotClient",
    "FeishuBotConfig",
    "FeishuBotConfigurationError",
    "FeishuBotError",
    "FeishuEventPayloadError",
    "FeishuEventVerificationError",
    "FeishuMessageEvent",
    "parse_feishu_callback",
]
