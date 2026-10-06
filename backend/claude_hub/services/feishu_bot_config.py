"""Revisioned instance-level Feishu Bot configuration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from claude_hub.config import settings
from claude_hub.services.feishu_bot import FeishuBotConfig
from claude_hub.services.runtime_isolation import resolve_runtime_home

BOT_ADMIN_OPEN_IDS_ENV = "CLAUDE_HUB_FEISHU_BOT_ADMIN_OPEN_IDS"
_BOT_ENV_KEYS = (
    "CLAUDE_HUB_FEISHU_BOT_APP_ID",
    "CLAUDE_HUB_FEISHU_BOT_APP_SECRET",
    "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN",
    "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY",
)
_REQUIRED_ENV_KEYS = _BOT_ENV_KEYS[:3]
_STATE_VERSION = 1


class FeishuBotConfigStoreError(RuntimeError):
    pass


class FeishuBotConfigRevisionError(FeishuBotConfigStoreError):
    pass


class FeishuBotAppChangeConfirmationRequired(FeishuBotConfigStoreError):
    pass


@dataclass(frozen=True)
class EffectiveFeishuBotConfig:
    config: FeishuBotConfig
    source: str
    editable: bool
    revision: int | None
    binding_generation: int
    snapshot_token: str


@dataclass(frozen=True)
class FeishuBotConfigState:
    configured: bool
    source: str
    editable: bool
    revision: int | None
    binding_generation: int
    updated_at: float | None
    config: FeishuBotConfig | None = None
    snapshot_token: str | None = None


class FeishuBotConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (resolve_runtime_home() / "secrets" / "feishu_bot.json")
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "revision": 0,
            "binding_generation": 0,
            "updated_at": None,
            "config": None,
        }

    def _load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeDecodeError) as exc:
            raise FeishuBotConfigStoreError("Cannot read Feishu Bot configuration") from exc
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FeishuBotConfigStoreError("Invalid Feishu Bot configuration state") from exc
        required_keys = {
            "version",
            "revision",
            "binding_generation",
            "updated_at",
            "config",
        }
        if not isinstance(value, dict) or not required_keys.issubset(value):
            raise FeishuBotConfigStoreError("Invalid Feishu Bot configuration state")
        version = value["version"]
        revision = value["revision"]
        generation = value["binding_generation"]
        updated_at = value["updated_at"]
        raw_config = value["config"]
        updated_at_valid = updated_at is None
        if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
            try:
                datetime.fromtimestamp(updated_at, timezone.utc)
            except (OverflowError, OSError, ValueError):
                pass
            else:
                updated_at_valid = True
        if (
            version != _STATE_VERSION
            or isinstance(version, bool)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not updated_at_valid
            or (raw_config is not None and not isinstance(raw_config, dict))
        ):
            raise FeishuBotConfigStoreError("Invalid Feishu Bot configuration state")
        return value

    def _save(self, state: dict[str, Any]) -> None:
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

    @staticmethod
    def _config_from_dict(value: dict[str, Any]) -> FeishuBotConfig:
        keys = ("app_id", "app_secret", "verification_token", "encrypt_key")
        if not all(isinstance(value.get(key), str) and value.get(key) for key in keys):
            raise FeishuBotConfigStoreError("Stored Feishu Bot configuration is incomplete")
        return FeishuBotConfig(
            app_id=value["app_id"],
            app_secret=value["app_secret"],
            verification_token=value["verification_token"],
            encrypt_key=value["encrypt_key"],
            api_base_url="https://open.feishu.cn",
        )

    @staticmethod
    def _validate_oauth_app(config: FeishuBotConfig) -> None:
        if not settings.feishu_app_id or config.app_id != settings.feishu_app_id:
            raise FeishuBotConfigStoreError("Feishu Bot and Web login must use the same app_id")

    @staticmethod
    def _environment_token(config: FeishuBotConfig) -> str:
        digest = hashlib.sha256(
            "\0".join(
                (
                    config.app_id,
                    config.app_secret,
                    config.verification_token,
                    config.encrypt_key or "",
                )
            ).encode()
        ).hexdigest()
        return f"environment:{digest}"

    def state(self, environ: Mapping[str, str] | None = None) -> FeishuBotConfigState:
        env = os.environ if environ is None else environ
        with self._lock:
            authority = self._load()
        revision = int(authority["revision"])
        generation = int(authority["binding_generation"])
        raw_updated = authority.get("updated_at")
        updated = float(raw_updated) if isinstance(raw_updated, (int, float)) else None
        present = {key: env.get(key, "").strip() for key in _BOT_ENV_KEYS}
        if any(present.values()):
            if not all(present[key] for key in _REQUIRED_ENV_KEYS):
                return FeishuBotConfigState(
                    False, "invalid_environment", False, None, generation, updated
                )
            config = FeishuBotConfig.from_env(env)
            self._validate_oauth_app(config)
            return FeishuBotConfigState(
                True,
                "environment",
                False,
                None,
                generation,
                updated,
                config,
                self._environment_token(config),
            )
        raw_config = authority.get("config")
        if raw_config is None:
            return FeishuBotConfigState(False, "none", True, revision, generation, updated)
        config = self._config_from_dict(raw_config)
        return FeishuBotConfigState(
            True,
            "stored",
            True,
            revision,
            generation,
            updated,
            config,
            f"stored:{revision}",
        )

    def require(self) -> EffectiveFeishuBotConfig:
        state = self.state()
        if not state.configured or state.config is None or state.snapshot_token is None:
            if state.source == "invalid_environment":
                raise FeishuBotConfigStoreError(
                    "Feishu Bot environment configuration is incomplete"
                )
            raise FeishuBotConfigStoreError("Feishu Bot is not configured")
        self._validate_oauth_app(state.config)
        return EffectiveFeishuBotConfig(
            state.config,
            state.source,
            state.editable,
            state.revision,
            state.binding_generation,
            state.snapshot_token,
        )

    def is_current(self, snapshot: EffectiveFeishuBotConfig) -> bool:
        try:
            current = self.require()
        except FeishuBotConfigStoreError:
            return False
        return (
            current.snapshot_token == snapshot.snapshot_token
            and current.binding_generation == snapshot.binding_generation
        )

    def replace(
        self,
        config: FeishuBotConfig,
        *,
        expected_revision: int,
        allow_app_id_change: bool,
    ) -> FeishuBotConfigState:
        self._validate_oauth_app(config)
        with self._lock:
            authority = self._load()
            revision = int(authority["revision"])
            if revision != expected_revision:
                raise FeishuBotConfigRevisionError("config_revision_conflict")
            previous = authority.get("config")
            old_app_id = previous.get("app_id") if isinstance(previous, dict) else None
            app_changed = bool(old_app_id and old_app_id != config.app_id)
            if app_changed and not allow_app_id_change:
                raise FeishuBotAppChangeConfirmationRequired("app_id_change_confirmation_required")
            generation = int(authority["binding_generation"])
            if previous is None or app_changed:
                generation += 1
            self._save(
                {
                    "version": _STATE_VERSION,
                    "revision": revision + 1,
                    "binding_generation": generation,
                    "updated_at": time.time(),
                    "config": {
                        "app_id": config.app_id,
                        "app_secret": config.app_secret,
                        "verification_token": config.verification_token,
                        "encrypt_key": config.encrypt_key,
                    },
                }
            )
        return self.state({})

    def disable(self, *, expected_revision: int) -> FeishuBotConfigState:
        with self._lock:
            authority = self._load()
            revision = int(authority["revision"])
            if revision != expected_revision:
                raise FeishuBotConfigRevisionError("config_revision_conflict")
            self._save(
                {
                    "version": _STATE_VERSION,
                    "revision": revision + 1,
                    "binding_generation": int(authority["binding_generation"]) + 1,
                    "updated_at": time.time(),
                    "config": None,
                }
            )
        return self.state({})


def configured_admin_open_ids(environ: Mapping[str, str] | None = None) -> set[str]:
    env = os.environ if environ is None else environ
    return {item.strip() for item in env.get(BOT_ADMIN_OPEN_IDS_ENV, "").split(",") if item.strip()}
