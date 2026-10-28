"""Atomic multi-Bot pool: credentials, pairing claims, and active bindings.

One JSON record holds every Bot's credentials together with its pairing state so
that a configuration change and the binding it invalidates commit in the same
write. Cross-Bot uniqueness (one Chat tab may be bound at most once in the whole
pool) is checked and written inside a single critical section, which is what
prevents concurrent double occupancy; the per-entry integer ``revision`` is a
separate concern and only rejects writes issued from a stale view.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from claude_hub.services.feishu_bot import FeishuBotConfig
from claude_hub.services.runtime_isolation import resolve_runtime_home

_STATE_VERSION = 3
_PREVIOUS_STATE_VERSION = 2
_LEGACY_STATE_VERSION = 1

ENV_BOT_ID = "env"
LEGACY_BOT_ID = "legacy"
_RESERVED_BOT_IDS = frozenset({ENV_BOT_ID, LEGACY_BOT_ID})

OFFICIAL_FEISHU_API = "https://open.feishu.cn"

BOT_ENV_KEYS = (
    "CLAUDE_HUB_FEISHU_BOT_APP_ID",
    "CLAUDE_HUB_FEISHU_BOT_APP_SECRET",
)
_REQUIRED_ENV_KEYS = BOT_ENV_KEYS
DEPRECATED_ENV_KEYS = (
    "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN",
    "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY",
    "CLAUDE_HUB_FEISHU_BOT_ADMIN_OPEN_IDS",
)

OWNER_KIND_OAUTH = "oauth"
OWNER_KIND_LOCAL = "local"
_OWNER_KINDS = frozenset({OWNER_KIND_OAUTH, OWNER_KIND_LOCAL})

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_PREFIX = "CH-"
_CODE_LENGTH = 10
_CODE_TTL_SECONDS = 600.0
_CLAIM_TTL_SECONDS = 600.0
# Excludes 0/O/1/I so a person can retype the word from a phone screen.
_CONFIRM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CONFIRM_LENGTH = 6
_CONFIRM_MAX_ATTEMPTS = 5

_RATE_WINDOW_SECONDS = 60.0
_RATE_MAX = 5
_MAX_PENDING_PER_BOT = 8
_MAX_CLAIMS_PER_BOT = 8
MAX_BOTS = 32
_TOMBSTONE_RETENTION_SECONDS = 30 * 24 * 60 * 60.0
_MAX_NAME_CHARS = 64
_MAX_SECRET_CHARS = 4096
_MAX_ID_CHARS = 128
_MIN_TIMESTAMP = 0.0
_MAX_TIMESTAMP = 4_102_444_800.0


class FeishuBotPoolError(RuntimeError):
    """Base error for an expected Bot pool failure."""


class FeishuBotPoolStateError(FeishuBotPoolError):
    """Raised when the persisted pool record cannot be read or trusted."""


class FeishuBotNotFound(FeishuBotPoolError):
    """Raised when no live or tombstoned entry exists for a bot_id."""


class FeishuBotRevoked(FeishuBotPoolError):
    """Raised for a deleted or disabled Bot whose events must stay silent."""


class FeishuBotUnavailable(FeishuBotPoolError):
    """Raised when an entry exists but currently has no usable credentials."""


class FeishuBotReadOnly(FeishuBotPoolError):
    """Raised when credentials come from the environment and cannot be edited."""


class FeishuBotRevisionConflict(FeishuBotPoolError):
    """Raised when a write was issued from a stale view of one entry."""


class FeishuBotAppIdConflict(FeishuBotPoolError):
    """Raised when another pool entry already uses that app_id."""


class FeishuBotAppIdImmutable(FeishuBotPoolError):
    """Raised on an attempt to repoint an existing bot_id at another app."""


class FeishuBotOccupied(FeishuBotPoolError):
    """Raised when the Bot already has an active binding."""


class FeishuChatOccupied(FeishuBotPoolError):
    """Raised when the Chat tab is already bound elsewhere in the pool."""


class FeishuPoolFull(FeishuBotPoolError):
    """Raised when the pool or one entry's bounded pairing state is full."""


class FeishuPairingNotFound(FeishuBotPoolError):
    """Raised when no live claim matches that pairing_id."""


class FeishuPairingNotOwned(FeishuBotPoolError):
    """Raised when the actor is not the Hub identity that started the pairing."""


class FeishuPairingMismatch(FeishuBotPoolError):
    """Raised when the typed confirmation word does not match the claim."""


class FeishuBindingCodeError(FeishuBotPoolError):
    """Raised when an inbound pairing code is unknown, expired, or superseded."""


class FeishuBotRateLimited(FeishuBotPoolError):
    """Raised when one Hub identity requests too many pairing codes."""


@dataclass(frozen=True)
class OwnerIdentity:
    """The Hub identity that performed an authorized pool operation.

    ``kind`` records how that identity was established so revocation can be
    re-evaluated honestly later. A ``local`` identity is instance-wide and
    shared, never a person.
    """

    open_id: str
    email: str
    kind: str


@dataclass(frozen=True)
class PairingClaim:
    pairing_id: str
    bot_id: str
    owner: OwnerIdentity
    tab_id: str
    workspace_id: str | None
    sender_open_id: str
    chat_id: str
    created_at: float
    expires_at: float
    generation: int


@dataclass(frozen=True)
class BotBinding:
    pairing_id: str
    bot_id: str
    owner: OwnerIdentity
    tab_id: str
    workspace_id: str | None
    sender_open_id: str
    chat_id: str
    created_at: float
    generation: int


@dataclass(frozen=True)
class BotEntry:
    bot_id: str
    name: str
    feishu_name: str | None
    feishu_avatar_url: str | None
    feishu_open_id: str | None
    app_id: str
    source: str
    enabled: bool
    credentials_editable: bool
    deletable: bool
    revision: int
    generation: int
    configured: bool
    created_at: float | None
    updated_at: float | None
    config: FeishuBotConfig | None
    claims: tuple[PairingClaim, ...]
    binding: BotBinding | None
    callback_enabled: bool = False
    verification_token: str | None = field(default=None, repr=False)
    encrypt_key: str | None = field(default=None, repr=False)
    expected_tenant_key: str | None = None


@dataclass(frozen=True)
class PoolSnapshot:
    pool_revision: int
    bots: tuple[BotEntry, ...]
    deprecated_env: tuple[str, ...]

    def get(self, bot_id: str) -> BotEntry | None:
        for entry in self.bots:
            if entry.bot_id == bot_id:
                return entry
        return None


@dataclass(frozen=True)
class EffectiveBot:
    """A usable credential snapshot taken before any await."""

    bot_id: str
    app_id: str
    config: FeishuBotConfig
    revision: int
    generation: int
    feishu_name: str | None
    feishu_avatar_url: str | None
    feishu_open_id: str | None
    callback_enabled: bool = False
    verification_token: str | None = field(default=None, repr=False)
    encrypt_key: str | None = field(default=None, repr=False)
    expected_tenant_key: str | None = None


def _state_error(name: str) -> FeishuBotPoolStateError:
    return FeishuBotPoolStateError(f"Invalid Feishu Bot pool field {name!r}")


def _as_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _state_error(name)
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _state_error(name)
    try:
        number = float(value)
    except OverflowError as exc:
        raise _state_error(name) from exc
    if not math.isfinite(number) or not _MIN_TIMESTAMP <= number <= _MAX_TIMESTAMP:
        raise _state_error(name)
    return number


def _safe_timestamp(value: Any) -> float:
    # Malformed expiry data must not be silently erased by a later mutation.
    return _as_float(value, "pairing.expires_at")


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _state_error(name)
    return value


def _as_text(value: Any, name: str, *, limit: int = _MAX_SECRET_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _state_error(name)
    return value


def _as_optional_text(value: Any, name: str, *, limit: int = _MAX_ID_CHARS) -> str | None:
    if value is None:
        return None
    return _as_text(value, name, limit=limit)


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _state_error(name)
    return value


def _validate_pool_numbers(bots: dict[str, Any], rate_limits: dict[str, Any]) -> None:
    for stamps in rate_limits.values():
        if not isinstance(stamps, list):
            raise _state_error("rate_limits")
        for stamp in stamps:
            _as_float(stamp, "rate_limits.timestamp")
    for raw in bots.values():
        raw = _as_mapping(raw, "bot")
        _as_int(raw.get("revision"), "bot.revision")
        _as_int(raw.get("generation"), "bot.generation")
        for field in ("created_at", "updated_at", "revoked_at"):
            value = raw.get(field)
            if value is not None:
                _as_float(value, f"bot.{field}")
        for collection, created_field in (
            ("pending", "issued_at"),
            ("claims", "created_at"),
        ):
            records = _as_mapping(raw.get(collection, {}), collection)
            for value in records.values():
                value = _as_mapping(value, collection)
                _as_float(value.get(created_field), f"{collection}.{created_field}")
                _as_float(value.get("expires_at"), f"{collection}.expires_at")
                _as_int(value.get("generation"), f"{collection}.generation")
                if collection == "claims":
                    _as_int(value.get("confirm_attempts", 0), "claims.confirm_attempts")
        binding = raw.get("binding")
        if binding is not None:
            binding = _as_mapping(binding, "binding")
            _as_float(binding.get("created_at"), "binding.created_at")
            _as_int(binding.get("generation"), "binding.generation")


def _owner_from_dict(value: dict[str, Any], name: str) -> OwnerIdentity:
    kind = value.get("owner_kind")
    if kind not in _OWNER_KINDS:
        raise _state_error(f"{name}.owner_kind")
    email = value.get("owner_email", "")
    if not isinstance(email, str) or len(email) > _MAX_ID_CHARS:
        raise _state_error(f"{name}.owner_email")
    return OwnerIdentity(
        open_id=_as_text(value.get("owner_open_id"), f"{name}.owner_open_id", limit=_MAX_ID_CHARS),
        email=email,
        kind=kind,
    )


def _owner_to_dict(owner: OwnerIdentity) -> dict[str, Any]:
    return {
        "owner_open_id": owner.open_id,
        "owner_email": owner.email,
        "owner_kind": owner.kind,
    }


def _code_digest(bot_id: str, code: str) -> str:
    """Namespace the digest by Bot so a code is structurally unusable elsewhere."""

    payload = f"{bot_id}\0{code.strip().upper()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _confirm_digest(bot_id: str, pairing_id: str, word: str) -> str:
    payload = f"{bot_id}\0{pairing_id}\0{word.strip().upper()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dedup_key(bot_id: str, message_id: str) -> str:
    return f"{bot_id}:{message_id}"


def turn_id_for(bot_id: str, message_id: str) -> str:
    payload = f"{bot_id}\0{message_id}".encode("utf-8")
    return f"feishu-{hashlib.sha256(payload).hexdigest()[:32]}"


def environment_config(
    environ: Mapping[str, str] | None = None,
) -> FeishuBotConfig | None:
    """Build the environment Bot's long-connection credentials, if complete."""

    env = os.environ if environ is None else environ
    present = {key: env.get(key, "").strip() for key in BOT_ENV_KEYS}
    if not any(present.values()):
        return None
    if not all(present[key] for key in _REQUIRED_ENV_KEYS):
        return None
    base_url = env.get("CLAUDE_HUB_FEISHU_API_BASE_URL", OFFICIAL_FEISHU_API).rstrip("/")
    return FeishuBotConfig(
        app_id=present[BOT_ENV_KEYS[0]],
        app_secret=present[BOT_ENV_KEYS[1]],
        api_base_url=base_url,
    )


def environment_present(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return any(env.get(key, "").strip() for key in BOT_ENV_KEYS)


def deprecated_env_present(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    return tuple(key for key in DEPRECATED_ENV_KEYS if env.get(key, "").strip())
