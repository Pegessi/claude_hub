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
from dataclasses import dataclass
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
        for collection, created_field in (("pending", "issued_at"), ("claims", "created_at")):
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


def environment_config(environ: Mapping[str, str] | None = None) -> FeishuBotConfig | None:
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


class FeishuBotPoolStore:
    """Single-writer JSON pool guarded by one process lock."""

    def __init__(
        self,
        path: Path | None = None,
        legacy_path: Path | None = None,
        now: Any = time.time,
    ) -> None:
        home = resolve_runtime_home()
        self.path = path or (home / "secrets" / "feishu_bot_pool.json")
        self.legacy_path = legacy_path or (home / "secrets" / "feishu_bot.json")
        self._now = now
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- storage

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _STATE_VERSION, "pool_revision": 0, "bots": {}, "rate_limits": {}}

    def _read_file(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise FeishuBotPoolStateError("Cannot read Feishu Bot pool state") from exc
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise FeishuBotPoolStateError("Invalid Feishu Bot pool state") from exc
        if not isinstance(value, dict):
            raise FeishuBotPoolStateError("Invalid Feishu Bot pool state")
        return value

    def _load(self) -> dict[str, Any]:
        value = self._read_file(self.path)
        if value is None:
            # Once the pool file exists it is the only authority. The legacy file
            # is never consulted again, so a rolled-back write cannot resurrect a
            # revoked authorization.
            return self._migrate_legacy()
        version = _as_int(value.get("version"), "version")
        if version not in {_PREVIOUS_STATE_VERSION, _STATE_VERSION}:
            raise FeishuBotPoolStateError("Unsupported Feishu Bot pool state version")
        bots = _as_mapping(value.get("bots"), "bots")
        rate_limits = _as_mapping(value.get("rate_limits"), "rate_limits")
        for bot_id, raw in bots.items():
            _as_text(bot_id, "bots key", limit=_MAX_ID_CHARS)
            _as_mapping(raw, f"bots[{bot_id}]")
            credentials = raw.get("credentials") if isinstance(raw, dict) else None
            if isinstance(credentials, dict):
                raw["credentials"] = {"app_secret": credentials.get("app_secret")}
        _validate_pool_numbers(bots, rate_limits)
        return {
            "version": _STATE_VERSION,
            "pool_revision": _as_int(value.get("pool_revision"), "pool_revision"),
            "bots": bots,
            "rate_limits": rate_limits,
        }

    def _migrate_legacy(self) -> dict[str, Any]:
        """Convert the v1 single-Bot credential file into a one-entry pool.

        Fails closed: a legacy file that exists but cannot be trusted must not
        be presented as an empty pool, or an operator would re-create a Bot
        while the old credentials are still on disk.

        Only credentials migrate. Old bindings are intentionally dropped: their
        authentication premise was that the Bot app and the Web OAuth app were
        the same, so ``owner_open_id`` could be compared with the Feishu sender.
        The pool abolishes that premise, and reinstating those bindings would
        mean trusting a historical self-identification that cannot be
        re-verified. Affected users pair once more.
        """

        value = self._read_file(self.legacy_path)
        if value is None:
            return self._empty()
        required = {"version", "revision", "binding_generation", "updated_at", "config"}
        if (
            not required.issubset(value)
            or _as_int(value.get("version"), "legacy.version") != _LEGACY_STATE_VERSION
        ):
            raise FeishuBotPoolStateError("Unsupported legacy Feishu Bot configuration")
        revision = _as_int(value.get("revision"), "legacy.revision")
        generation = _as_int(value.get("binding_generation"), "legacy.binding_generation")
        raw_updated = value.get("updated_at")
        updated_at = None if raw_updated is None else _as_float(raw_updated, "legacy.updated_at")
        raw_config = value.get("config")
        state = self._empty()
        if raw_config is None:
            # The old store wrote config=None for an explicit disable. Keep that
            # as a tombstone so events for the retired Bot stay silent.
            state["bots"][LEGACY_BOT_ID] = {
                "bot_id": LEGACY_BOT_ID,
                "revision": revision,
                "generation": generation,
                "revoked_at": updated_at if updated_at is not None else self._now(),
            }
            return state
        if not isinstance(raw_config, dict):
            raise FeishuBotPoolStateError("Invalid legacy Feishu Bot configuration")
        keys = ("app_id", "app_secret")
        if not all(isinstance(raw_config.get(key), str) and raw_config[key] for key in keys):
            raise FeishuBotPoolStateError("Incomplete legacy Feishu Bot configuration")
        state["bots"][LEGACY_BOT_ID] = {
            "bot_id": LEGACY_BOT_ID,
            "name": "Feishu Bot",
            "app_id": raw_config["app_id"],
            "credentials": {
                "app_secret": raw_config["app_secret"],
            },
            "revision": revision,
            "generation": generation,
            "enabled": True,
            "created_at": updated_at,
            "updated_at": updated_at,
            "pending": {},
            "claims": {},
            "binding": None,
            "revoked_at": None,
        }
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["pool_revision"] = _as_int(state.get("pool_revision", 0), "pool_revision") + 1
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------- read model

    @staticmethod
    def _claim_from_dict(bot_id: str, pairing_id: str, value: dict[str, Any]) -> PairingClaim:
        name = f"claims[{pairing_id}]"
        return PairingClaim(
            pairing_id=pairing_id,
            bot_id=bot_id,
            owner=_owner_from_dict(value, name),
            tab_id=_as_text(value.get("tab_id"), f"{name}.tab_id", limit=_MAX_ID_CHARS),
            workspace_id=_as_optional_text(value.get("workspace_id"), f"{name}.workspace_id"),
            sender_open_id=_as_text(
                value.get("sender_open_id"), f"{name}.sender_open_id", limit=_MAX_ID_CHARS
            ),
            chat_id=_as_text(value.get("chat_id"), f"{name}.chat_id", limit=_MAX_ID_CHARS),
            created_at=_as_float(value.get("created_at"), f"{name}.created_at"),
            expires_at=_as_float(value.get("expires_at"), f"{name}.expires_at"),
            generation=_as_int(value.get("generation"), f"{name}.generation"),
        )

    @staticmethod
    def _binding_from_dict(bot_id: str, value: dict[str, Any]) -> BotBinding:
        return BotBinding(
            pairing_id=_as_text(value.get("pairing_id"), "binding.pairing_id", limit=_MAX_ID_CHARS),
            bot_id=bot_id,
            owner=_owner_from_dict(value, "binding"),
            tab_id=_as_text(value.get("tab_id"), "binding.tab_id", limit=_MAX_ID_CHARS),
            workspace_id=_as_optional_text(value.get("workspace_id"), "binding.workspace_id"),
            sender_open_id=_as_text(
                value.get("sender_open_id"), "binding.sender_open_id", limit=_MAX_ID_CHARS
            ),
            chat_id=_as_text(value.get("chat_id"), "binding.chat_id", limit=_MAX_ID_CHARS),
            created_at=_as_float(value.get("created_at"), "binding.created_at"),
            generation=_as_int(value.get("generation"), "binding.generation"),
        )

    @staticmethod
    def _new_entry(bot_id: str, name: str, app_id: str, now: float) -> dict[str, Any]:
        return {
            "bot_id": bot_id,
            "name": name,
            "app_id": app_id,
            "credentials": None,
            "revision": 0,
            "generation": 0,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "pending": {},
            "claims": {},
            "binding": None,
            "revoked_at": None,
        }

    @staticmethod
    def _entry_config(
        raw: dict[str, Any], env_config: FeishuBotConfig | None
    ) -> FeishuBotConfig | None:
        """Credentials come from the file, or from the environment for ``env``."""

        if raw["bot_id"] == ENV_BOT_ID:
            if env_config is None or raw.get("app_id") != env_config.app_id:
                return None
            return env_config
        credentials = raw.get("credentials")
        if not isinstance(credentials, dict):
            return None
        return FeishuBotConfig(
            app_id=_as_text(raw.get("app_id"), "app_id", limit=_MAX_ID_CHARS),
            app_secret=_as_text(credentials.get("app_secret"), "app_secret"),
            api_base_url=OFFICIAL_FEISHU_API,
        )

    def _entry_view(
        self,
        bot_id: str,
        raw: dict[str, Any],
        env_config: FeishuBotConfig | None,
        now: float,
        *,
        env_seen: bool = False,
    ) -> BotEntry:
        is_env = bot_id == ENV_BOT_ID
        config = self._entry_config(raw, env_config)
        generation = _as_int(raw.get("generation"), "generation")
        claims: list[PairingClaim] = []
        for pairing_id, value in _as_mapping(raw.get("claims", {}), "claims").items():
            if not isinstance(value, dict):
                continue
            try:
                claim = self._claim_from_dict(bot_id, pairing_id, value)
            except FeishuBotPoolStateError:
                continue
            if claim.expires_at > now and claim.generation == generation:
                claims.append(claim)
        binding = None
        raw_binding = raw.get("binding")
        if raw_binding is not None:
            candidate = self._binding_from_dict(bot_id, _as_mapping(raw_binding, "binding"))
            if candidate.generation == generation:
                binding = candidate
        source = "stored"
        if is_env:
            source = "invalid_environment" if env_seen and config is None else "environment"
        return BotEntry(
            bot_id=bot_id,
            name=_as_text(raw.get("name"), "name", limit=_MAX_NAME_CHARS),
            app_id=config.app_id if config is not None else str(raw.get("app_id") or ""),
            source=source,
            enabled=_as_bool(raw.get("enabled"), "enabled"),
            credentials_editable=not is_env,
            deletable=not is_env,
            revision=_as_int(raw.get("revision"), "revision"),
            generation=generation,
            configured=config is not None,
            created_at=(
                None
                if raw.get("created_at") is None
                else _as_float(raw["created_at"], "created_at")
            ),
            updated_at=(
                None
                if raw.get("updated_at") is None
                else _as_float(raw["updated_at"], "updated_at")
            ),
            config=config,
            claims=tuple(claims),
            binding=binding,
        )

    def snapshot(self, environ: Mapping[str, str] | None = None) -> PoolSnapshot:
        env_config = environment_config(environ)
        now = self._now()
        with self._lock:
            state = self._load_synced(environ, now)
        raw_bots = dict(state["bots"])
        entries = tuple(
            self._entry_view(bot_id, raw, env_config, now, env_seen=environment_present(environ))
            for bot_id, raw in sorted(raw_bots.items())
            if isinstance(raw, dict) and not raw.get("revoked_at")
        )
        return PoolSnapshot(
            pool_revision=state["pool_revision"],
            bots=entries,
            deprecated_env=deprecated_env_present(environ),
        )

    def effective(self, bot_id: str, environ: Mapping[str, str] | None = None) -> EffectiveBot:
        """Resolve usable credentials, distinguishing revoked from unknown."""

        env_config = environment_config(environ)
        now = self._now()
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict):
            raise FeishuBotNotFound(bot_id)
        if raw.get("revoked_at"):
            raise FeishuBotRevoked(bot_id)
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        if not entry.enabled:
            raise FeishuBotRevoked(bot_id)
        if entry.config is None:
            raise FeishuBotUnavailable(bot_id)
        return EffectiveBot(
            bot_id=bot_id,
            app_id=entry.config.app_id,
            config=entry.config,
            revision=entry.revision,
            generation=entry.generation,
        )

    def is_current(self, effective: EffectiveBot, environ: Mapping[str, str] | None = None) -> bool:
        """Re-check a pre-await snapshot before acting on it."""

        try:
            current = self.effective(effective.bot_id, environ)
        except FeishuBotPoolError:
            return False
        return (
            current.app_id == effective.app_id
            and current.revision == effective.revision
            and current.generation == effective.generation
            and current.config == effective.config
        )

    # ------------------------------------------------------------ invariants

    def _prune(self, state: dict[str, Any], now: float) -> None:
        cutoff = now - _RATE_WINDOW_SECONDS
        state["rate_limits"] = {
            owner: kept
            for owner, stamps in state["rate_limits"].items()
            if isinstance(stamps, list)
            for kept in (
                [float(s) for s in stamps if isinstance(s, (int, float)) and float(s) >= cutoff],
            )
            if kept
        }
        for bot_id, raw in list(state["bots"].items()):
            if not isinstance(raw, dict):
                del state["bots"][bot_id]
                continue
            revoked_at = raw.get("revoked_at")
            if isinstance(revoked_at, (int, float)):
                if now - float(revoked_at) > _TOMBSTONE_RETENTION_SECONDS:
                    del state["bots"][bot_id]
                continue
            generation = raw.get("generation")
            raw["pending"] = {
                digest: value
                for digest, value in _as_mapping(raw.get("pending", {}), "pending").items()
                if isinstance(value, dict)
                and _safe_timestamp(value.get("expires_at")) > now
                and value.get("generation") == generation
            }
            raw["claims"] = {
                pairing_id: value
                for pairing_id, value in _as_mapping(raw.get("claims", {}), "claims").items()
                if isinstance(value, dict)
                and _safe_timestamp(value.get("expires_at")) > now
                and value.get("generation") == generation
            }

    def _live_entry(self, state: dict[str, Any], bot_id: str) -> dict[str, Any]:
        raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            raise FeishuBotNotFound(bot_id)
        return raw

    def _sync_env_entry(
        self, state: dict[str, Any], environ: Mapping[str, str] | None, now: float
    ) -> bool:
        """Revoke changed or conflicting environment identity once, durably."""
        config = environment_config(environ)
        app_id = config.app_id if config is not None else ""
        if app_id and any(
            other_id != ENV_BOT_ID
            and isinstance(other, dict)
            and not other.get("revoked_at")
            and other.get("app_id") == app_id
            for other_id, other in state["bots"].items()
        ):
            app_id = ""
        raw = state["bots"].get(ENV_BOT_ID)
        if raw is None:
            if not environment_present(environ):
                return False
            state["bots"][ENV_BOT_ID] = self._new_entry(ENV_BOT_ID, "Environment Bot", app_id, now)
            return True
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return False
        if raw.get("app_id") == app_id:
            return False
        # Compare the accepted identity, not the rejected environment value, so
        # reads in a persistent conflict cannot advance the counters forever.
        raw["app_id"] = app_id
        self._advance_generation(raw)
        self._touch(raw, now)
        return True

    def _load_synced(self, environ: Mapping[str, str] | None, now: float) -> dict[str, Any]:
        state = self._load()
        if self._sync_env_entry(state, environ, now):
            self._save(state)
        return state

    @staticmethod
    def _check_revision(raw: dict[str, Any], expected_revision: int) -> None:
        if _as_int(raw.get("revision"), "revision") != expected_revision:
            raise FeishuBotRevisionConflict(str(raw.get("bot_id")))

    @staticmethod
    def _touch(raw: dict[str, Any], now: float) -> None:
        raw["revision"] = _as_int(raw.get("revision"), "revision") + 1
        raw["updated_at"] = now

    @staticmethod
    def _advance_generation(raw: dict[str, Any]) -> None:
        """Invalidate every outstanding code, claim, and binding for this Bot."""

        raw["generation"] = _as_int(raw.get("generation"), "generation") + 1
        raw["pending"] = {}
        raw["claims"] = {}
        raw["binding"] = None

    @staticmethod
    def _require_tab_free(state: dict[str, Any], tab_id: str, *, exclude: str) -> None:
        for other_id, raw in state["bots"].items():
            if other_id == exclude or not isinstance(raw, dict) or raw.get("revoked_at"):
                continue
            binding = raw.get("binding")
            if isinstance(binding, dict) and binding.get("tab_id") == tab_id:
                raise FeishuChatOccupied(tab_id)

    @staticmethod
    def _require_app_id_free(state: dict[str, Any], app_id: str, *, exclude: str | None) -> None:
        for other_id, raw in state["bots"].items():
            if other_id == exclude or not isinstance(raw, dict) or raw.get("revoked_at"):
                continue
            if raw.get("app_id") == app_id:
                raise FeishuBotAppIdConflict(app_id)

    # ------------------------------------------------------- bot lifecycle

    def create_bot(
        self, *, name: str, config: FeishuBotConfig, environ: Mapping[str, str] | None = None
    ) -> str:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            if len([v for v in state["bots"].values() if not v.get("revoked_at")]) >= MAX_BOTS:
                raise FeishuPoolFull("pool")
            self._require_app_id_free(state, config.app_id, exclude=None)
            bot_id = secrets.token_hex(8)
            while bot_id in state["bots"] or bot_id in _RESERVED_BOT_IDS:
                bot_id = secrets.token_hex(8)
            raw = self._new_entry(bot_id, name, config.app_id, now)
            raw["credentials"] = {"app_secret": config.app_secret}
            state["bots"][bot_id] = raw
            self._save(state)
            return bot_id

    def rotate_secrets(
        self,
        bot_id: str,
        *,
        app_secret: str,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Replace the App Secret under the same app_id.

        The active binding is preserved: it identifies a conversation under an
        app identity that has not changed. Bumping ``revision`` is enough to
        retire snapshots taken before the rotation.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if bot_id == ENV_BOT_ID:
                raise FeishuBotReadOnly(bot_id)
            self._check_revision(raw, expected_revision)
            raw["credentials"] = {"app_secret": app_secret}
            self._touch(raw, now)
            self._save(state)

    def update_bot(
        self,
        bot_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        enabled: bool | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if name is not None:
                raw["name"] = name
            if enabled is not None and enabled != raw.get("enabled"):
                raw["enabled"] = enabled
                if not enabled:
                    # Disabling revokes outstanding pairing state for this Bot.
                    self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)

    def delete_bot(
        self, bot_id: str, *, expected_revision: int, environ: Mapping[str, str] | None = None
    ) -> None:
        """Replace the entry with a secret-free tombstone.

        The tombstone lets any event already accepted for a revoked Bot be
        dropped silently.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if bot_id == ENV_BOT_ID:
                raise FeishuBotReadOnly(bot_id)
            self._check_revision(raw, expected_revision)
            state["bots"][bot_id] = {
                "bot_id": bot_id,
                "revision": _as_int(raw.get("revision"), "revision") + 1,
                "generation": _as_int(raw.get("generation"), "generation") + 1,
                "revoked_at": now,
            }
            self._save(state)

    # ------------------------------------------------------------- pairing

    def issue_code(
        self,
        bot_id: str,
        *,
        owner: OwnerIdentity,
        tab_id: str,
        workspace_id: str | None,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[str, float, int]:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            if raw.get("binding") is not None:
                raise FeishuBotOccupied(bot_id)
            self._require_tab_free(state, tab_id, exclude=bot_id)
            # Rate limiting is per Hub identity, not per Bot: otherwise picking a
            # different Bot would multiply the allowance.
            stamps = state["rate_limits"].setdefault(owner.open_id, [])
            if len(stamps) >= _RATE_MAX:
                self._save(state)
                raise FeishuBotRateLimited(owner.open_id)
            if len(raw["pending"]) >= _MAX_PENDING_PER_BOT:
                raise FeishuPoolFull("pending")
            stamps.append(now)
            code = _CODE_PREFIX + "".join(
                secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)
            )
            expires_at = now + _CODE_TTL_SECONDS
            raw["pending"][_code_digest(bot_id, code)] = {
                **_owner_to_dict(owner),
                "tab_id": tab_id,
                "workspace_id": workspace_id,
                "issued_at": now,
                "expires_at": expires_at,
                "generation": raw["generation"],
            }
            self._touch(raw, now)
            self._save(state)
            return code, expires_at, _as_int(raw["revision"], "revision")

    def claim_code(
        self,
        bot_id: str,
        *,
        code: str,
        sender_open_id: str,
        chat_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[PairingClaim, str]:
        """Consume a code presented through the Bot's verified channel.

        This creates a claim, never a binding. Nothing is routed until the Hub
        owner types back the confirmation word that only this reply carries.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            digest = _code_digest(bot_id, code)
            value = raw["pending"].pop(digest, None)
            if not isinstance(value, dict):
                raise FeishuBindingCodeError("unknown")
            if _safe_timestamp(value.get("expires_at")) <= now:
                self._save(state)
                raise FeishuBindingCodeError("expired")
            if value.get("generation") != raw["generation"]:
                self._save(state)
                raise FeishuBindingCodeError("superseded")
            if len(raw["claims"]) >= _MAX_CLAIMS_PER_BOT:
                self._save(state)
                raise FeishuPoolFull("claims")
            pairing_id = secrets.token_hex(16)
            word = "".join(secrets.choice(_CONFIRM_ALPHABET) for _ in range(_CONFIRM_LENGTH))
            raw["claims"][pairing_id] = {
                **_owner_to_dict(_owner_from_dict(value, "pending")),
                "tab_id": value["tab_id"],
                "workspace_id": value.get("workspace_id"),
                "sender_open_id": sender_open_id,
                "chat_id": chat_id,
                "created_at": now,
                "expires_at": now + _CLAIM_TTL_SECONDS,
                "generation": raw["generation"],
                # The confirmation word protects online pairing only. File
                # confidentiality relies on the private directory and file mode.
                "confirm_digest": _confirm_digest(bot_id, pairing_id, word),
                "confirm_attempts": 0,
            }
            self._touch(raw, now)
            self._save(state)
            claim = self._claim_from_dict(bot_id, pairing_id, raw["claims"][pairing_id])
            return claim, word

    def activate_claim(
        self,
        bot_id: str,
        *,
        pairing_id: str,
        confirm_word: str,
        actor: OwnerIdentity,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            value = raw["claims"].get(pairing_id)
            if not isinstance(value, dict):
                raise FeishuPairingNotFound(pairing_id)
            owner = _owner_from_dict(value, "claim")
            if owner.kind != actor.kind or not hmac.compare_digest(owner.open_id, actor.open_id):
                raise FeishuPairingNotOwned(pairing_id)
            expected = str(value.get("confirm_digest", ""))
            if not hmac.compare_digest(expected, _confirm_digest(bot_id, pairing_id, confirm_word)):
                attempts = _as_int(value.get("confirm_attempts", 0), "confirm_attempts") + 1
                if attempts >= _CONFIRM_MAX_ATTEMPTS:
                    del raw["claims"][pairing_id]
                else:
                    value["confirm_attempts"] = attempts
                self._touch(raw, now)
                self._save(state)
                raise FeishuPairingMismatch(pairing_id)
            if raw.get("binding") is not None:
                raise FeishuBotOccupied(bot_id)
            tab_id = _as_text(value.get("tab_id"), "claim.tab_id", limit=_MAX_ID_CHARS)
            self._require_tab_free(state, tab_id, exclude=bot_id)
            binding_id = secrets.token_hex(16)
            raw["binding"] = {
                **_owner_to_dict(owner),
                "pairing_id": binding_id,
                "tab_id": tab_id,
                "workspace_id": value.get("workspace_id"),
                "sender_open_id": value["sender_open_id"],
                "chat_id": value["chat_id"],
                "created_at": now,
                "generation": raw["generation"],
            }
            raw["claims"] = {}
            raw["pending"] = {}
            self._touch(raw, now)
            self._save(state)
            return self._binding_from_dict(bot_id, raw["binding"])

    def release_binding(
        self, bot_id: str, *, expected_revision: int, environ: Mapping[str, str] | None = None
    ) -> BotBinding | None:
        """Unbind explicitly. Advancing the generation is what guarantees that
        a message already in flight cannot land on a later binding."""

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            previous = raw.get("binding")
            released = (
                self._binding_from_dict(bot_id, previous) if isinstance(previous, dict) else None
            )
            self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)
            return released

    def routed_binding(
        self,
        bot_id: str,
        *,
        sender_open_id: str,
        chat_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Resolve the active binding for one verified inbound conversation.

        No Web identity is compared here: the Bot-side ``open_id`` belongs to a
        different application and is not comparable with the Hub owner's.
        """

        now = self._now()
        env_config = environment_config(environ)
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return None
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        binding = entry.binding
        if binding is None or not entry.enabled:
            return None
        if not hmac.compare_digest(binding.sender_open_id, sender_open_id):
            return None
        if not hmac.compare_digest(binding.chat_id, chat_id):
            return None
        return binding

    def drop_binding(self, bot_id: str, pairing_id: str) -> bool:
        """Remove one specific binding without CAS.

        Matching on ``pairing_id`` is what makes this safe from a background
        cleanup path: a binding created after the caller's snapshot has a
        different id and is never touched.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            raw = state["bots"].get(bot_id)
            if not isinstance(raw, dict) or raw.get("revoked_at"):
                return False
            binding = raw.get("binding")
            if not isinstance(binding, dict) or binding.get("pairing_id") != pairing_id:
                return False
            self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)
            return True


__all__ = [
    "BotBinding",
    "BotEntry",
    "DEPRECATED_ENV_KEYS",
    "ENV_BOT_ID",
    "EffectiveBot",
    "FeishuBindingCodeError",
    "FeishuBotAppIdConflict",
    "FeishuBotAppIdImmutable",
    "FeishuBotNotFound",
    "FeishuBotOccupied",
    "FeishuBotPoolError",
    "FeishuBotPoolStateError",
    "FeishuBotPoolStore",
    "FeishuBotRateLimited",
    "FeishuBotReadOnly",
    "FeishuBotRevisionConflict",
    "FeishuBotRevoked",
    "FeishuBotUnavailable",
    "FeishuChatOccupied",
    "FeishuPairingMismatch",
    "FeishuPairingNotFound",
    "FeishuPairingNotOwned",
    "FeishuPoolFull",
    "LEGACY_BOT_ID",
    "MAX_BOTS",
    "OWNER_KIND_LOCAL",
    "OWNER_KIND_OAUTH",
    "OwnerIdentity",
    "PairingClaim",
    "PoolSnapshot",
    "dedup_key",
    "deprecated_env_present",
    "environment_config",
    "environment_present",
    "turn_id_for",
]
