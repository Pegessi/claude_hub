"""Persistent Feishu user access token store and ``~/.lark-cli`` loader.

Supports two token sources for the personal ``/ai`` assistant workflow:
1. Hub's own Feishu OAuth login callback (``backend/claude_hub/api/auth.py``).
2. Local ``lark-cli`` credentials (``~/.lark-cli/config.json`` plus the
   AES-256-GCM keychain store under ``~/.local/share/lark-cli/`` or plaintext
   JSON token files).

Tokens are persisted in the Hub SQLite document store under
``secrets/feishu_user_tokens.json`` with sensitive fields encrypted at rest
using a ``0600`` per-runtime-home master key file.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.util
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Mapping

from claude_hub.services.local_storage import (
    document_exists,
    read_document,
    write_document,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home

logger = logging.getLogger(__name__)

_STORE_VERSION = 1
_ENC_PREFIX = "enc:v1:"
_FALLBACK_PREFIX = "enc:hmac1:"


def _normalize_epoch_seconds(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 100_000_000_000:
            ts /= 1000.0
        return max(0.0, ts)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0.0
        try:
            ts = float(stripped)
            if ts > 100_000_000_000:
                ts /= 1000.0
            return max(0.0, ts)
        except ValueError:
            pass
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return max(0.0, parsed.timestamp())
        except ValueError:
            return 0.0
    return 0.0


def _normalize_scopes(raw: Any) -> str:
    if isinstance(raw, str):
        tokens = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
        return " ".join(dict.fromkeys(tokens))
    if isinstance(raw, (list, tuple, set)):
        tokens = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                tokens.extend(part.strip() for part in item.split() if part.strip())
        return " ".join(dict.fromkeys(tokens))
    return ""


_CLI_META_ALIASES: tuple[tuple[str, ...], ...] = (
    ("accessToken", "access_token"),
    ("refreshToken", "refresh_token"),
    ("appSecret", "app_secret"),
    ("expiresAt", "expires_at"),
    ("refreshExpiresAt", "refresh_expires_at"),
    ("userName", "user_name"),
    ("scope",),
)


def _normalize_cli_meta(raw: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group in _CLI_META_ALIASES:
        canon = group[0]
        for k in group:
            if k not in raw or raw[k] is None:
                continue
            val = raw[k]
            if canon == "scope" and isinstance(val, (str, list, tuple, set)):
                out[canon] = val
                break
            if isinstance(val, str) and val.strip():
                out[canon] = val.strip()
                break
            if canon in {"expiresAt", "refreshExpiresAt"} and isinstance(val, (int, float)):
                out[canon] = val
                break
    return out


class _LibcryptoAesGcm:
    """Thin ctypes wrapper around OpenSSL EVP_aes_256_gcm (no third-party deps)."""

    def __init__(self) -> None:
        lib_name = (
            ctypes.util.find_library("crypto")
            or "libcrypto.so.3"
            or "libcrypto.so.1.1"
            or "libcrypto.dylib"
        )
        lib = ctypes.CDLL(lib_name)
        self._lib = lib
        vp, cp, ci, cip = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        )

        def _bind(sym: str, restype: Any, argtypes: list[Any]) -> Any:
            fn = getattr(lib, sym)
            fn.restype, fn.argtypes = restype, argtypes
            return fn

        self._ctx_new = _bind("EVP_CIPHER_CTX_new", vp, [])
        self._ctx_free = _bind("EVP_CIPHER_CTX_free", None, [vp])
        self._aes_256_gcm = _bind("EVP_aes_256_gcm", vp, [])
        self._ctx_ctrl = _bind("EVP_CIPHER_CTX_ctrl", ci, [vp, ci, ci, vp])
        self._enc_init = _bind("EVP_EncryptInit_ex", ci, [vp, vp, vp, cp, cp])
        self._enc_update = _bind("EVP_EncryptUpdate", ci, [vp, cp, cip, cp, ci])
        self._enc_final = _bind("EVP_EncryptFinal_ex", ci, [vp, cp, cip])
        self._dec_init = _bind("EVP_DecryptInit_ex", ci, [vp, vp, vp, cp, cp])
        self._dec_update = _bind("EVP_DecryptUpdate", ci, [vp, cp, cip, cp, ci])
        self._dec_final = _bind("EVP_DecryptFinal_ex", ci, [vp, cp, cip])

    def encrypt(self, key: bytes, plaintext: bytes, nonce: bytes | None = None) -> bytes:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        iv = nonce if nonce is not None else secrets.token_bytes(12)
        if len(iv) != 12:
            raise ValueError("AES-256-GCM requires a 12-byte nonce")
        ctx = self._ctx_new()
        if not ctx:
            raise RuntimeError("EVP_CIPHER_CTX_new failed")
        try:
            if self._enc_init(ctx, self._aes_256_gcm(), None, None, None) != 1:
                raise RuntimeError("EVP_EncryptInit_ex cipher failed")
            if self._ctx_ctrl(ctx, 0x9, len(iv), None) != 1:
                raise RuntimeError("EVP_CTRL_GCM_SET_IVLEN failed")
            if self._enc_init(ctx, None, None, key, iv) != 1:
                raise RuntimeError("EVP_EncryptInit_ex key/iv failed")
            out = ctypes.create_string_buffer(len(plaintext) + 16)
            outlen = ctypes.c_int(0)
            if self._enc_update(ctx, out, ctypes.byref(outlen), plaintext, len(plaintext)) != 1:
                raise RuntimeError("EVP_EncryptUpdate failed")
            total = outlen.value
            fin = ctypes.create_string_buffer(16)
            finlen = ctypes.c_int(0)
            if self._enc_final(ctx, fin, ctypes.byref(finlen)) != 1:
                raise RuntimeError("EVP_EncryptFinal_ex failed")
            tag = ctypes.create_string_buffer(16)
            if self._ctx_ctrl(ctx, 0x10, 16, tag) != 1:
                raise RuntimeError("EVP_CTRL_GCM_GET_TAG failed")
            return iv + out.raw[:total] + fin.raw[: finlen.value] + tag.raw
        finally:
            self._ctx_free(ctx)

    def decrypt(self, key: bytes, blob: bytes) -> bytes:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        if len(blob) < 28:
            raise ValueError("AES-256-GCM ciphertext too short")
        nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
        ctx = self._ctx_new()
        if not ctx:
            raise RuntimeError("EVP_CIPHER_CTX_new failed")
        try:
            if self._dec_init(ctx, self._aes_256_gcm(), None, None, None) != 1:
                raise RuntimeError("EVP_DecryptInit_ex cipher failed")
            if self._ctx_ctrl(ctx, 0x9, len(nonce), None) != 1:
                raise RuntimeError("EVP_CTRL_GCM_SET_IVLEN failed")
            if self._dec_init(ctx, None, None, key, nonce) != 1:
                raise RuntimeError("EVP_DecryptInit_ex key/iv failed")
            out = ctypes.create_string_buffer(len(ct))
            outlen = ctypes.c_int(0)
            if self._dec_update(ctx, out, ctypes.byref(outlen), ct, len(ct)) != 1:
                raise RuntimeError("EVP_DecryptUpdate failed")
            total = outlen.value
            tag_buf = ctypes.create_string_buffer(tag, len(tag))
            if self._ctx_ctrl(ctx, 0x11, len(tag), tag_buf) != 1:
                raise RuntimeError("EVP_CTRL_GCM_SET_TAG failed")
            fin = ctypes.create_string_buffer(16)
            finlen = ctypes.c_int(0)
            if self._dec_final(ctx, fin, ctypes.byref(finlen)) != 1:
                raise ValueError("AES-256-GCM authentication tag verification failed")
            return out.raw[:total] + fin.raw[: finlen.value]
        finally:
            self._ctx_free(ctx)


_AES_GCM: _LibcryptoAesGcm | None = None
_AES_GCM_LOADED = False


def _get_aes_gcm() -> _LibcryptoAesGcm | None:
    global _AES_GCM, _AES_GCM_LOADED
    if not _AES_GCM_LOADED:
        _AES_GCM_LOADED = True
        try:
            _AES_GCM = _LibcryptoAesGcm()
        except Exception:
            _AES_GCM = None
    return _AES_GCM


def aes_256_gcm_encrypt(key: bytes, plaintext: bytes, nonce: bytes | None = None) -> bytes:
    """Encrypt ``plaintext`` with AES-256-GCM (12-byte nonce + ciphertext + 16-byte tag)."""
    gcm = _get_aes_gcm()
    if gcm is None:
        raise RuntimeError("OpenSSL libcrypto AES-256-GCM is unavailable")
    return gcm.encrypt(key, plaintext, nonce=nonce)


def aes_256_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """Decrypt ``blob`` (12-byte nonce + ciphertext + 16-byte tag) with AES-256-GCM."""
    gcm = _get_aes_gcm()
    if gcm is None:
        raise RuntimeError("OpenSSL libcrypto AES-256-GCM is unavailable")
    return gcm.decrypt(key, blob)


def _ensure_0600_key(key_path: Path) -> bytes:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.is_file():
        raw = key_path.read_bytes()
        if len(raw) == 32:
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return raw
    new_key = secrets.token_bytes(32)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key)
    finally:
        os.close(fd)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return new_key


def _xor_hmac_stream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    stream, counter = bytearray(), 0
    while len(stream) < len(data):
        stream.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream[: len(data)]))


def _seal_secret(key: bytes, value: str) -> str:
    if not value:
        return ""
    gcm, raw = _get_aes_gcm(), value.encode("utf-8")
    if gcm is not None:
        return _ENC_PREFIX + base64.urlsafe_b64encode(gcm.encrypt(key, raw)).decode("ascii")
    nonce = secrets.token_bytes(16)
    ct = _xor_hmac_stream(key, nonce, raw)
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return _FALLBACK_PREFIX + base64.urlsafe_b64encode(nonce + ct + mac).decode("ascii")


def _unseal_secret(key: bytes, stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith(_ENC_PREFIX):
        blob = base64.urlsafe_b64decode(stored[len(_ENC_PREFIX) :].encode("ascii"))
        return aes_256_gcm_decrypt(key, blob).decode("utf-8")
    if stored.startswith(_FALLBACK_PREFIX):
        blob = base64.urlsafe_b64decode(stored[len(_FALLBACK_PREFIX) :].encode("ascii"))
        if len(blob) < 48:
            raise ValueError("Corrupted fallback encrypted secret")
        nonce, ct, mac = blob[:16], blob[16:-32], blob[-32:]
        if not hmac.compare_digest(mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
            raise ValueError("Secret MAC verification failed")
        return _xor_hmac_stream(key, nonce, ct).decode("utf-8")
    return stored


def _credential_fingerprint(access_token: str, refresh_token: str) -> str:
    raw = f"{access_token.strip()}:{refresh_token.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class FeishuUserTokenRecord:
    """One authenticated Feishu user's OAuth token record."""

    open_id: str
    app_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    user_name: str = ""
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    scope: str = ""
    source: str = "hub_oauth"
    updated_at: float = 0.0
    generation: int = 1
    imported_fingerprint: str = field(default="", repr=False)

    def scopes_set(self) -> set[str]:
        return {part for part in self.scope.split() if part}

    def has_scope(self, scope_name: str) -> bool:
        return scope_name in self.scopes_set()

    def is_expired(self, *, now: float | None = None, skew_seconds: float = 300.0) -> bool:
        if self.expires_at <= 0:
            return False
        current = time.time() if now is None else now
        return (current + skew_seconds) >= self.expires_at

    def is_refresh_expired(self, *, now: float | None = None) -> bool:
        if self.refresh_expires_at <= 0:
            return False
        current = time.time() if now is None else now
        return current >= self.refresh_expires_at


class FeishuUserTokenStore:
    """Thread-safe store for Feishu user tokens backed by Hub SQLite storage."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        key_path: Path | None = None,
        lark_cli_dir: Path | None = None,
        lark_cli_data_dir: Path | None = None,
        now: Any = time.time,
    ) -> None:
        runtime_home = resolve_runtime_home()
        self._custom_path = path is not None
        self._path = path or (runtime_home / "secrets" / "feishu_user_tokens.json")
        self._key_path = key_path or (self._path.parent / "feishu_user_tokens.key")
        self._lark_cli_dir = lark_cli_dir
        self._lark_cli_data_dir = lark_cli_data_dir
        self._now = now
        self._lock = threading.RLock()
        self._held_lock_fds: dict[str, int] = {}
        self._async_locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    def _master_key(self) -> bytes:
        return _ensure_0600_key(self._key_path)

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not document_exists(self._path):
            return {}
        try:
            data = read_document(self._path)
        except (FileNotFoundError, ValueError, OSError):
            return {}
        tokens = data.get("tokens") if isinstance(data, Mapping) else None
        if not isinstance(tokens, Mapping):
            return {}
        return {
            k: dict(v) for k, v in tokens.items() if isinstance(k, str) and isinstance(v, Mapping)
        }

    def _save_raw(self, tokens: Mapping[str, dict[str, Any]]) -> None:
        write_document(self._path, {"version": _STORE_VERSION, "tokens": dict(tokens)})

    def _record_to_row(self, record: FeishuUserTokenRecord) -> dict[str, Any]:
        key = self._master_key()
        return {
            "open_id": record.open_id,
            "app_id": record.app_id,
            "user_name": record.user_name,
            "access_token": _seal_secret(key, record.access_token),
            "refresh_token": _seal_secret(key, record.refresh_token),
            "app_secret": _seal_secret(key, record.app_secret),
            "expires_at": record.expires_at,
            "refresh_expires_at": record.refresh_expires_at,
            "scope": _normalize_scopes(record.scope),
            "source": record.source,
            "updated_at": record.updated_at or float(self._now()),
            "generation": max(1, int(record.generation or 1)),
            "imported_fingerprint": record.imported_fingerprint
            or _credential_fingerprint(record.access_token, record.refresh_token),
        }

    def _row_to_record(self, row: Mapping[str, Any]) -> FeishuUserTokenRecord | None:
        open_id = str(row.get("open_id") or "").strip()
        app_id = str(row.get("app_id") or "").strip()
        if not open_id or not app_id:
            return None
        key = self._master_key()
        try:
            access_token = _unseal_secret(key, str(row.get("access_token") or ""))
            refresh_token = _unseal_secret(key, str(row.get("refresh_token") or ""))
            app_secret = _unseal_secret(key, str(row.get("app_secret") or ""))
        except Exception:
            logger.warning("Failed to unseal Feishu user token for %s", open_id)
            return None
        if not access_token:
            return None
        raw_gen = row.get("generation")
        try:
            generation = max(1, int(raw_gen)) if raw_gen is not None else 1
        except (TypeError, ValueError):
            generation = 1
        imported_fp = str(row.get("imported_fingerprint") or "").strip() or _credential_fingerprint(
            access_token, refresh_token
        )
        return FeishuUserTokenRecord(
            open_id=open_id,
            app_id=app_id,
            access_token=access_token,
            refresh_token=refresh_token,
            app_secret=app_secret,
            user_name=str(row.get("user_name") or "").strip(),
            expires_at=_normalize_epoch_seconds(row.get("expires_at")),
            refresh_expires_at=_normalize_epoch_seconds(row.get("refresh_expires_at")),
            scope=_normalize_scopes(row.get("scope")),
            source=str(row.get("source") or "hub_oauth").strip(),
            updated_at=_normalize_epoch_seconds(row.get("updated_at")),
            generation=generation,
            imported_fingerprint=imported_fp,
        )

    @staticmethod
    def _row_key(app_id: str, open_id: str) -> str:
        return f"{app_id.strip()}:{open_id.strip()}"

    def upsert_token(self, record: FeishuUserTokenRecord) -> FeishuUserTokenRecord:
        if (
            not record.open_id.strip()
            or not record.app_id.strip()
            or not record.access_token.strip()
        ):
            raise ValueError("open_id, app_id, and access_token are required")
        normalized = replace(
            record,
            open_id=record.open_id.strip(),
            app_id=record.app_id.strip(),
            access_token=record.access_token.strip(),
            refresh_token=record.refresh_token.strip(),
            app_secret=record.app_secret.strip(),
            user_name=record.user_name.strip(),
            scope=_normalize_scopes(record.scope),
            updated_at=record.updated_at or float(self._now()),
        )
        with self._lock:
            tokens = self._load_raw()
            tokens[self._row_key(normalized.app_id, normalized.open_id)] = self._record_to_row(
                normalized
            )
            self._save_raw(tokens)
        return normalized

    def save_oauth_token(
        self,
        *,
        open_id: str,
        app_id: str,
        access_token: str,
        refresh_token: str = "",
        user_name: str = "",
        expires_in: int | float | None = None,
        refresh_expires_in: int | float | None = None,
        scope: str = "",
        app_secret: str = "",
        source: str = "hub_oauth",
    ) -> FeishuUserTokenRecord:
        with self.refresh_lock(app_id=app_id, open_id=open_id):
            now = float(self._now())
            existing = self.get_token(open_id=open_id, app_id=app_id)
            expires_at = (
                now + float(expires_in)
                if isinstance(expires_in, (int, float)) and expires_in > 0
                else (existing.expires_at if existing else 0.0)
            )
            refresh_expires_at = (
                now + float(refresh_expires_in)
                if isinstance(refresh_expires_in, (int, float)) and refresh_expires_in > 0
                else (existing.refresh_expires_at if existing else 0.0)
            )
            merged_scope = _normalize_scopes(scope) or (existing.scope if existing else "")
            merged_secret = app_secret.strip() or (existing.app_secret if existing else "")
            next_gen = (existing.generation + 1) if existing is not None else 1
            record = FeishuUserTokenRecord(
                open_id=open_id,
                app_id=app_id,
                access_token=access_token,
                refresh_token=refresh_token or (existing.refresh_token if existing else ""),
                app_secret=merged_secret,
                user_name=user_name or (existing.user_name if existing else ""),
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
                scope=merged_scope,
                source=source,
                updated_at=now,
                generation=next_gen,
            )
            return self.upsert_token(record)

    def list_tokens(self) -> list[FeishuUserTokenRecord]:
        with self._lock:
            tokens = self._load_raw()
        records: list[FeishuUserTokenRecord] = []
        for row in tokens.values():
            rec = self._row_to_record(row)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records

    def get_token(
        self,
        open_id: str | None = None,
        *,
        app_id: str | None = None,
    ) -> FeishuUserTokenRecord | None:
        clean_open_id = (open_id or "").strip()
        clean_app_id = (app_id or "").strip()
        for record in self.list_tokens():
            if clean_open_id and record.open_id != clean_open_id:
                continue
            if clean_app_id and record.app_id != clean_app_id:
                continue
            return record
        return None

    def delete_token(self, open_id: str, *, app_id: str | None = None) -> bool:
        clean_open_id = open_id.strip()
        clean_app_id = (app_id or "").strip()
        with self._lock:
            tokens = self._load_raw()
            removed = False
            for key, row in list(tokens.items()):
                if str(row.get("open_id") or "").strip() != clean_open_id:
                    continue
                if clean_app_id and str(row.get("app_id") or "").strip() != clean_app_id:
                    continue
                tokens.pop(key, None)
                removed = True
            if removed:
                self._save_raw(tokens)
            return removed

    def _resolve_default_lark_cli_dirs(self) -> tuple[Path, Path] | None:
        env_dir = os.environ.get("CLAUDE_HUB_LARK_CLI_DIR", "").strip()
        if env_dir:
            base = Path(env_dir)
            return base, base
        # When running with an explicit test path or an isolated CLAUDE_HUB_HOME,
        # do not implicitly read the host user's ~/.lark-cli unless configured.
        if self._custom_path and self._lark_cli_dir is None and self._lark_cli_data_dir is None:
            return None
        runtime_home = resolve_runtime_home()
        default_hub_home = (Path.home() / ".claude_hub").resolve()
        if (
            self._lark_cli_dir is None
            and self._lark_cli_data_dir is None
            and runtime_home.resolve() != default_hub_home
        ):
            isolated_lark = runtime_home / ".lark-cli"
            return isolated_lark, isolated_lark
        cfg_dir = self._lark_cli_dir or (Path.home() / ".lark-cli")
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        default_data_dir = (
            Path(xdg_data) / "lark-cli"
            if xdg_data
            else (Path.home() / ".local" / "share" / "lark-cli")
        )
        keychain_dir = self._lark_cli_data_dir or default_data_dir
        return cfg_dir, keychain_dir

    def _resolve_lark_cli_dirs_with_overrides(
        self,
        *,
        lark_cli_dir: Path | None = None,
        data_dir: Path | None = None,
    ) -> tuple[Path, Path] | None:
        if lark_cli_dir is not None or data_dir is not None:
            cfg_dir = lark_cli_dir or self._lark_cli_dir or (data_dir or Path.home() / ".lark-cli")
            keychain_dir = data_dir or self._lark_cli_data_dir or cfg_dir
            return cfg_dir, keychain_dir
        resolved_dirs = self._resolve_default_lark_cli_dirs()
        if resolved_dirs is not None:
            return resolved_dirs
        if self._lark_cli_dir is None and self._lark_cli_data_dir is None:
            return None
        cfg_dir = self._lark_cli_dir or (self._lark_cli_data_dir or Path.home() / ".lark-cli")
        keychain_dir = self._lark_cli_data_dir or cfg_dir
        return cfg_dir, keychain_dir

    @staticmethod
    def _select_config_candidates(
        config_path: Path,
        *,
        target_app_id: str = "",
        target_open_id: str = "",
        target_profile: str = "",
    ) -> tuple[bool, bool, list[tuple[str, str, str, dict[str, Any]]]]:
        candidates: list[tuple[str, str, str, dict[str, Any]]] = []
        if not config_path.is_file():
            return False, False, candidates
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        apps = cfg.get("apps") if isinstance(cfg, Mapping) else None
        current_app = (
            str(cfg.get("currentApp") or cfg.get("current_app") or "").strip()
            if isinstance(cfg, Mapping)
            else ""
        )
        if not isinstance(apps, list) or not apps:
            return False, False, candidates
        valid_apps = [
            e
            for e in apps
            if isinstance(e, Mapping) and str(e.get("appId") or e.get("app_id") or "").strip()
        ]
        selected: list[Mapping[str, Any]] = []
        if target_profile or target_app_id:
            for e in valid_apps:
                e_id = str(e.get("appId") or e.get("app_id") or "").strip()
                e_name = str(e.get("name") or e.get("profile") or "").strip()
                if target_profile and target_profile not in {e_id, e_name}:
                    continue
                if target_app_id and e_id != target_app_id:
                    continue
                selected.append(e)
            if not selected:
                return True, True, candidates
        elif current_app:
            for e in valid_apps:
                e_id = str(e.get("appId") or e.get("app_id") or "").strip()
                e_name = str(e.get("name") or e.get("profile") or "").strip()
                if current_app in {e_id, e_name}:
                    selected.append(e)
                    break
            if not selected:
                return True, True, candidates
        elif valid_apps:
            selected = [valid_apps[0]]
        for app_entry in selected:
            e_app = str(app_entry.get("appId") or app_entry.get("app_id") or "").strip()
            pref = str(
                app_entry.get("currentUser")
                or app_entry.get("defaultUser")
                or app_entry.get("userOpenId")
                or ""
            ).strip()
            users = app_entry.get("users")
            if isinstance(users, list) and users:
                umaps = [u for u in users if isinstance(u, Mapping)]
                if not target_open_id and pref:
                    umaps.sort(
                        key=lambda u: (
                            0
                            if str(u.get("userOpenId") or u.get("open_id") or "").strip() == pref
                            else 1
                        )
                    )
                for u in umaps:
                    u_open = str(u.get("userOpenId") or u.get("open_id") or "").strip()
                    if u_open and (not target_open_id or u_open == target_open_id):
                        u_name = str(u.get("userName") or u.get("name") or "").strip()
                        candidates.append((e_app, u_open, u_name, {**dict(app_entry), **dict(u)}))
            else:
                u_open = str(app_entry.get("userOpenId") or app_entry.get("open_id") or "").strip()
                if u_open and (not target_open_id or u_open == target_open_id):
                    u_name = str(app_entry.get("userName") or app_entry.get("name") or "").strip()
                    candidates.append((e_app, u_open, u_name, dict(app_entry)))
        return True, False, candidates

    def _select_cli_candidates(
        self,
        *,
        lark_cli_dir: Path | None = None,
        data_dir: Path | None = None,
        target_app_id: str = "",
        target_open_id: str = "",
        target_profile: str = "",
    ) -> tuple[Path, Path, list[tuple[str, str, str, dict[str, Any]]]] | None:
        resolved_dirs = self._resolve_lark_cli_dirs_with_overrides(
            lark_cli_dir=lark_cli_dir, data_dir=data_dir
        )
        if resolved_dirs is None:
            return None
        cfg_dir, keychain_dir = resolved_dirs
        has_config_apps, dangling, candidates = self._select_config_candidates(
            cfg_dir / "config.json",
            target_app_id=target_app_id,
            target_open_id=target_open_id,
            target_profile=target_profile,
        )
        if dangling or (target_profile and not candidates) or (has_config_apps and not candidates):
            return cfg_dir, keychain_dir, []
        allowed_pairs = {(c[0], c[1]) for c in candidates} if has_config_apps else None
        base_meta_by_pair = {(c[0], c[1]): (c[2], c[3]) for c in candidates}
        default_app, default_uid = (
            next(iter(allowed_pairs))
            if allowed_pairs and len(allowed_pairs) == 1
            else (target_app_id, target_open_id)
        )
        for token_filename in ("tokens.json", "user_token.json", "credentials.json"):
            token_file = cfg_dir / token_filename
            if token_file.is_file():
                try:
                    raw_obj = json.loads(token_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                entries = raw_obj if isinstance(raw_obj, list) else [raw_obj]
                for item in entries:
                    if not isinstance(item, Mapping):
                        continue
                    e_app = str(item.get("appId") or item.get("app_id") or default_app).strip()
                    e_uid = str(
                        item.get("userOpenId") or item.get("open_id") or default_uid
                    ).strip()
                    if e_app and e_uid:
                        if target_app_id and e_app != target_app_id:
                            continue
                        if target_open_id and e_uid != target_open_id:
                            continue
                        if allowed_pairs is not None and (e_app, e_uid) not in allowed_pairs:
                            continue
                        b_name, b_meta = base_meta_by_pair.get((e_app, e_uid), ("", {}))
                        merged = {**_normalize_cli_meta(b_meta), **_normalize_cli_meta(item)}
                        e_name = str(merged.get("userName") or b_name).strip()
                        candidates.append((e_app, e_uid, e_name, merged))
        if not candidates and not has_config_apps:
            for search_dir in (keychain_dir, cfg_dir, cfg_dir / "keychain"):
                if not search_dir.is_dir():
                    continue
                for enc_file in sorted(search_dir.glob("cli_*_ou_*.enc")):
                    stem = enc_file.stem
                    idx = stem.find("_ou_")
                    if idx <= 0:
                        continue
                    c_app = stem[:idx]
                    c_uid = stem[idx + 1 :]
                    if target_app_id and c_app != target_app_id:
                        continue
                    if target_open_id and c_uid != target_open_id:
                        continue
                    candidates.append((c_app, c_uid, "", {}))
                if candidates:
                    break
        return cfg_dir, keychain_dir, candidates

    def _resolve_lock_account(
        self,
        *,
        app_id: str = "",
        open_id: str = "",
        profile: str = "",
        prefer_cli_config: bool = False,
        lark_cli_dir: Path | None = None,
        data_dir: Path | None = None,
    ) -> tuple[str, str]:
        """Resolve the target ``(app_id, open_id)`` without locking unrelated CLI profiles."""
        clean_app = app_id.strip()
        clean_open = open_id.strip()
        clean_profile = profile.strip()
        if clean_app and clean_open and not clean_profile and not prefer_cli_config:
            return clean_app, clean_open
        if not prefer_cli_config and not clean_profile and document_exists(self._path):
            rec = self.get_token(open_id=clean_open or None, app_id=clean_app or None)
            if rec is not None:
                clean_app = clean_app or rec.app_id
                clean_open = clean_open or rec.open_id
                if clean_app and clean_open:
                    return clean_app, clean_open
        cli_sel = self._select_cli_candidates(
            lark_cli_dir=lark_cli_dir,
            data_dir=data_dir,
            target_app_id=clean_app,
            target_open_id=clean_open,
            target_profile=clean_profile,
        )
        if cli_sel is not None:
            cfg_dir, keychain_dir, candidates = cli_sel
            if candidates:
                for c_app, c_open, _, _ in candidates:
                    if any(
                        (d / f"{c_app}_{c_open}.enc").is_file()
                        for d in (keychain_dir, cfg_dir, cfg_dir / "keychain")
                    ):
                        return c_app, c_open
                for c_app, c_open, _, meta in candidates:
                    if str(meta.get("accessToken") or meta.get("access_token") or "").strip():
                        return c_app, c_open
                return candidates[0][0], candidates[0][1]
        if prefer_cli_config:
            return "", ""
        return (
            (clean_app, clean_open)
            if (clean_app and clean_open and not clean_profile)
            else ("", "")
        )

    def _lock_paths(
        self,
        *,
        app_id: str = "",
        open_id: str = "",
        profile: str = "",
        create_cli_lock_dir: bool = False,
        resolve_account: bool = True,
    ) -> list[Path]:
        if resolve_account:
            clean_app, clean_open = self._resolve_lock_account(
                app_id=app_id, open_id=open_id, profile=profile
            )
        else:
            clean_app, clean_open = app_id.strip(), open_id.strip()
        lock_paths: list[Path] = []
        resolved_dirs = self._resolve_lark_cli_dirs_with_overrides()
        if resolved_dirs is not None:
            cfg_dir, _ = resolved_dirs
            cli_locks_dir = cfg_dir / "locks"
            if clean_app and clean_open and (create_cli_lock_dir or cli_locks_dir.is_dir()):
                lock_paths.append(cli_locks_dir / f"refresh_{clean_app}_{clean_open}.lock")
        hub_lock = self._key_path.parent / "feishu_user_tokens.refresh.lock"
        if hub_lock not in lock_paths:
            lock_paths.append(hub_lock)
        return lock_paths

    @asynccontextmanager
    async def async_file_lock(
        self,
        *,
        app_id: str = "",
        open_id: str = "",
        profile: str = "",
        create_cli_lock_dir: bool = False,
        resolve_account: bool = True,
        timeout: float = 15.0,
    ) -> AsyncIterator[None]:
        """Acquire inter-process file locks via non-blocking ``LOCK_NB`` polling."""
        lock_paths = self._lock_paths(
            app_id=app_id,
            open_id=open_id,
            profile=profile,
            create_cli_lock_dir=create_cli_lock_dir,
            resolve_account=resolve_account,
        )
        newly_opened: list[tuple[str, int]] = []
        deadline = time.monotonic() + max(0.1, timeout)
        try:
            for lp in lock_paths:
                lp.parent.mkdir(parents=True, exist_ok=True)
                key = str(lp.resolve())
                with self._lock:
                    if key in self._held_lock_fds:
                        continue
                fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o600)
                acquired = False
                try:
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                            with self._lock:
                                self._held_lock_fds[key] = fd
                            newly_opened.append((key, fd))
                            break
                        except (BlockingIOError, PermissionError, OSError) as exc:
                            if not isinstance(exc, BlockingIOError) and getattr(
                                exc, "errno", None
                            ) not in {11, 13, 35}:
                                raise
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    f"Timed out waiting for Feishu token lock {lp}"
                                ) from exc
                            await asyncio.sleep(0.005)
                finally:
                    if not acquired:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            yield
        finally:
            for key, fd in reversed(newly_opened):
                with self._lock:
                    self._held_lock_fds.pop(key, None)
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @asynccontextmanager
    async def async_refresh_lock(
        self,
        *,
        app_id: str = "",
        open_id: str = "",
    ) -> AsyncIterator[None]:
        """Serialize in-process async refreshers without blocking the event loop thread."""
        loop = asyncio.get_running_loop()
        key = (id(loop), app_id.strip(), open_id.strip())
        with self._lock:
            async_lock = self._async_locks.get(key)
            if async_lock is None:
                async_lock = asyncio.Lock()
                self._async_locks[key] = async_lock
        async with async_lock:
            yield

    @contextmanager
    def refresh_lock(
        self,
        *,
        app_id: str = "",
        open_id: str = "",
        create_cli_lock_dir: bool = False,
    ) -> Iterator[None]:
        """Acquire thread and inter-process file locks (including ``lark-cli`` account lock)."""
        lock_paths = self._lock_paths(
            app_id=app_id, open_id=open_id, create_cli_lock_dir=create_cli_lock_dir
        )
        with self._lock:
            newly_opened: list[tuple[str, int]] = []
            try:
                for lp in lock_paths:
                    lp.parent.mkdir(parents=True, exist_ok=True)
                    key = str(lp.resolve())
                    if key in self._held_lock_fds:
                        continue
                    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    except BaseException:
                        os.close(fd)
                        raise
                    self._held_lock_fds[key] = fd
                    newly_opened.append((key, fd))
                yield
            finally:
                for key, fd in reversed(newly_opened):
                    self._held_lock_fds.pop(key, None)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    finally:
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    def _merge_imported_record(self, candidate: FeishuUserTokenRecord) -> FeishuUserTokenRecord:
        """Persist an imported ``lark-cli`` token without rolling back newer rotated credentials."""
        with self.refresh_lock(app_id=candidate.app_id, open_id=candidate.open_id):
            existing = self.get_token(open_id=candidate.open_id, app_id=candidate.app_id)
            cand_fp = _credential_fingerprint(candidate.access_token, candidate.refresh_token)
            if existing is not None:
                existing_fp = _credential_fingerprint(existing.access_token, existing.refresh_token)
                # Same credential snapshot as currently active or previously imported before rotation
                if cand_fp in {existing_fp, existing.imported_fingerprint}:
                    enriched = replace(
                        existing,
                        app_secret=existing.app_secret or candidate.app_secret,
                        user_name=existing.user_name or candidate.user_name,
                        scope=existing.scope or candidate.scope,
                    )
                    if enriched != existing:
                        return self.upsert_token(enriched)
                    return existing
                # Never overwrite a newer rotated token with an older/equal expiry from lark-cli
                if (
                    existing.expires_at > 0
                    and candidate.expires_at > 0
                    and candidate.expires_at <= existing.expires_at
                ):
                    return existing
                next_gen = existing.generation + 1
            else:
                next_gen = max(1, candidate.generation)

            to_save = replace(
                candidate,
                app_secret=candidate.app_secret or (existing.app_secret if existing else ""),
                user_name=candidate.user_name or (existing.user_name if existing else ""),
                scope=candidate.scope or (existing.scope if existing else ""),
                generation=next_gen,
                imported_fingerprint=cand_fp,
                updated_at=float(self._now()),
            )
            return self.upsert_token(to_save)

    def _sync_or_adopt_lark_cli(
        self,
        record: FeishuUserTokenRecord,
        *,
        expected_fingerprints: set[str] | None = None,
    ) -> tuple[bool, FeishuUserTokenRecord | None]:
        """CAS-sync rotated credentials to ``lark-cli`` or adopt a newer external CLI login."""
        resolved_dirs = self._resolve_lark_cli_dirs_with_overrides()
        if resolved_dirs is None:
            return False, None
        cfg_dir, keychain_dir = resolved_dirs

        synced = False
        for search_dir in dict.fromkeys((keychain_dir, cfg_dir, cfg_dir / "keychain")):
            master_key_file = search_dir / "master.key"
            enc_token_path = search_dir / f"{record.app_id}_{record.open_id}.enc"
            if not master_key_file.is_file() or not enc_token_path.is_file():
                continue
            try:
                master_key = master_key_file.read_bytes()
                if len(master_key) != 32:
                    continue
                existing_obj: dict[str, Any] = {}
                try:
                    raw = aes_256_gcm_decrypt(master_key, enc_token_path.read_bytes())
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, Mapping):
                        existing_obj = dict(parsed)
                except Exception:
                    existing_obj = {}

                if expected_fingerprints and existing_obj:
                    cli_at = str(
                        existing_obj.get("accessToken") or existing_obj.get("access_token") or ""
                    ).strip()
                    cli_rt = str(
                        existing_obj.get("refreshToken") or existing_obj.get("refresh_token") or ""
                    ).strip()
                    cli_fp = _credential_fingerprint(cli_at, cli_rt)
                    if cli_at and cli_fp and cli_fp not in expected_fingerprints:
                        # External lark-cli login/refresh updated the shared keychain concurrently;
                        # preserve the newer CLI credentials + authoritative scopes instead of overwriting.
                        cli_scope = _normalize_scopes(existing_obj.get("scope"))
                        adopted = replace(
                            record,
                            access_token=cli_at,
                            refresh_token=cli_rt or record.refresh_token,
                            user_name=str(
                                existing_obj.get("userName")
                                or existing_obj.get("user_name")
                                or record.user_name
                            ).strip(),
                            expires_at=_normalize_epoch_seconds(
                                existing_obj.get("expiresAt") or existing_obj.get("expires_at")
                            )
                            or record.expires_at,
                            refresh_expires_at=_normalize_epoch_seconds(
                                existing_obj.get("refreshExpiresAt")
                                or existing_obj.get("refresh_expires_at")
                            )
                            or record.refresh_expires_at,
                            scope=cli_scope,
                            source="lark_cli",
                            imported_fingerprint=cli_fp,
                        )
                        return False, adopted

                existing_obj.update(
                    {
                        "appId": record.app_id,
                        "userOpenId": record.open_id,
                        "userName": record.user_name,
                        "accessToken": record.access_token,
                        "refreshToken": record.refresh_token,
                        "expiresAt": (
                            int(round(record.expires_at * 1000)) if record.expires_at > 0 else 0
                        ),
                        "refreshExpiresAt": (
                            int(round(record.refresh_expires_at * 1000))
                            if record.refresh_expires_at > 0
                            else 0
                        ),
                        "scope": record.scope,
                    }
                )
                encrypted = aes_256_gcm_encrypt(
                    master_key,
                    json.dumps(existing_obj, ensure_ascii=False).encode("utf-8"),
                )
                tmp_path = enc_token_path.with_suffix(".enc.tmp")
                fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, encrypted)
                finally:
                    os.close(fd)
                os.replace(tmp_path, enc_token_path)
                synced = True
            except Exception:
                logger.warning(
                    "Failed to sync rotated token back to lark-cli keychain %s",
                    enc_token_path,
                    exc_info=True,
                )
        return synced, None

    def sync_to_lark_cli(self, record: FeishuUserTokenRecord) -> bool:
        """Write rotated user credentials back to the shared ``lark-cli`` keychain when present."""
        with self.refresh_lock(
            app_id=record.app_id, open_id=record.open_id, create_cli_lock_dir=True
        ):
            synced, _ = self._sync_or_adopt_lark_cli(record)
            return synced

    def save_refreshed_token(
        self,
        previous: FeishuUserTokenRecord,
        refreshed: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> FeishuUserTokenRecord:
        """Persist a refreshed user token generation and CAS-sync shared ``lark-cli`` storage."""
        current_now = float(self._now()) if now is None else float(now)
        with self.refresh_lock(
            app_id=previous.app_id, open_id=previous.open_id, create_cli_lock_dir=True
        ):
            prev_fp = _credential_fingerprint(previous.access_token, previous.refresh_token)
            existing = self.get_token(open_id=previous.open_id, app_id=previous.app_id)
            if existing is not None:
                exist_fp = _credential_fingerprint(existing.access_token, existing.refresh_token)
                # CAS against the captured `previous` credential generation: if Hub was updated
                # concurrently (e.g. fresh OAuth callback or newly imported CLI login) while the
                # refresh request was in flight, adopt/keep the newer Hub generation instead of
                # rolling it back with the stale refresh result.
                if existing.generation > previous.generation or exist_fp != prev_fp:
                    return existing
            else:
                existing = previous

            new_access = str(refreshed.get("access_token") or "").strip()
            if not new_access:
                raise ValueError("Refreshed token payload is missing access_token")
            new_refresh = str(refreshed.get("refresh_token") or previous.refresh_token).strip()
            expires_in = refreshed.get("expires_in")
            refresh_expires_in = refreshed.get("refresh_expires_in") or refreshed.get(
                "refresh_token_expires_in"
            )
            expires_at = (
                current_now + float(expires_in)
                if isinstance(expires_in, (int, float)) and expires_in > 0
                else previous.expires_at
            )
            refresh_expires_at = (
                current_now + float(refresh_expires_in)
                if isinstance(refresh_expires_in, (int, float)) and refresh_expires_in > 0
                else previous.refresh_expires_at
            )
            next_gen = max(previous.generation, existing.generation) + 1
            expected_fps = {fp for fp in (prev_fp, previous.imported_fingerprint) if fp}
            has_explicit_scope = "scope" in refreshed and refreshed.get("scope") is not None
            authoritative_scope = (
                _normalize_scopes(refreshed.get("scope")) if has_explicit_scope else previous.scope
            )
            updated = replace(
                existing,
                access_token=new_access,
                refresh_token=new_refresh,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
                scope=authoritative_scope,
                updated_at=current_now,
                generation=next_gen,
                imported_fingerprint=previous.imported_fingerprint or prev_fp,
            )
            synced, adopted = self._sync_or_adopt_lark_cli(
                updated, expected_fingerprints=expected_fps
            )
            if adopted is not None:
                return self.upsert_token(adopted)
            if synced:
                updated = replace(
                    updated,
                    imported_fingerprint=_credential_fingerprint(
                        updated.access_token, updated.refresh_token
                    ),
                )
            return self.upsert_token(updated)

    def import_from_lark_cli(
        self,
        *,
        lark_cli_dir: Path | None = None,
        data_dir: Path | None = None,
        app_id: str | None = None,
        open_id: str | None = None,
        profile: str | None = None,
    ) -> FeishuUserTokenRecord | None:
        """Import a user token from ``~/.lark-cli`` and ``~/.local/share/lark-cli``.

        Honors ``lark-cli``'s ``CurrentAppConfig`` resolution order:
        explicit ``profile`` / ``app_id`` > ``currentApp`` (by ``name`` or ``appId``) > ``apps[0]``.
        A dangling ``currentApp`` does not fall back to ``apps[0]``.
        """
        if (
            lark_cli_dir is None
            and data_dir is None
            and self._resolve_default_lark_cli_dirs() is None
        ):
            return None
        cli_sel = self._select_cli_candidates(
            lark_cli_dir=lark_cli_dir,
            data_dir=data_dir,
            target_app_id=(app_id or "").strip(),
            target_open_id=(open_id or "").strip(),
            target_profile=(profile or "").strip(),
        )
        if cli_sel is None:
            return None
        cfg_dir, keychain_dir, candidates = cli_sel
        if not candidates:
            return None

        # Gather only explicitly granted scopes from cache if any exist (never unapproved requested_scope)
        cached_scopes: list[str] = []
        scope_cache_dir = cfg_dir / "cache" / "auth_login_scopes"
        if scope_cache_dir.is_dir():
            for scope_file in sorted(scope_cache_dir.glob("*.json")):
                try:
                    payload = json.loads(scope_file.read_text(encoding="utf-8"))
                    if isinstance(payload, Mapping) and isinstance(
                        payload.get("granted_scope"), str
                    ):
                        cached_scopes.append(payload["granted_scope"])
                except Exception:
                    continue
        fallback_scope = _normalize_scopes(cached_scopes)

        # Try encrypted keychain store if master.key exists
        for search_dir in (keychain_dir, cfg_dir, cfg_dir / "keychain"):
            master_key_file = search_dir / "master.key"
            if not master_key_file.is_file():
                continue
            try:
                master_key = master_key_file.read_bytes()
            except Exception:
                continue
            if len(master_key) != 32:
                continue

            for c_app, c_uid, c_name, meta in candidates:
                enc_token_path = search_dir / f"{c_app}_{c_uid}.enc"
                if not enc_token_path.is_file():
                    continue
                try:
                    decrypted = aes_256_gcm_decrypt(master_key, enc_token_path.read_bytes())
                    token_obj = json.loads(decrypted.decode("utf-8"))
                except Exception:
                    logger.warning("Failed to decrypt lark-cli user token %s", enc_token_path.name)
                    continue
                if not isinstance(token_obj, Mapping):
                    continue
                access_token = str(
                    token_obj.get("accessToken") or token_obj.get("access_token") or ""
                ).strip()
                if not access_token:
                    continue
                refresh_token = str(
                    token_obj.get("refreshToken") or token_obj.get("refresh_token") or ""
                ).strip()
                app_secret = ""
                enc_secret_path = search_dir / f"appsecret_{c_app}.enc"
                if enc_secret_path.is_file():
                    try:
                        raw_sec = (
                            aes_256_gcm_decrypt(master_key, enc_secret_path.read_bytes())
                            .decode("utf-8")
                            .strip()
                        )
                        if raw_sec.startswith("{"):
                            sec_obj = json.loads(raw_sec)
                            if isinstance(sec_obj, Mapping):
                                raw_sec = str(
                                    sec_obj.get("appSecret") or sec_obj.get("app_secret") or ""
                                ).strip()
                        app_secret = raw_sec
                    except Exception:
                        app_secret = ""
                raw_meta_sec = meta.get("appSecret") or meta.get("app_secret")
                if not app_secret and isinstance(raw_meta_sec, str):
                    app_secret = raw_meta_sec.strip()

                scope_str = (
                    _normalize_scopes(token_obj.get("scope") or meta.get("scope")) or fallback_scope
                )
                record = FeishuUserTokenRecord(
                    open_id=str(token_obj.get("userOpenId") or c_uid).strip(),
                    app_id=str(token_obj.get("appId") or c_app).strip(),
                    access_token=access_token,
                    refresh_token=refresh_token,
                    app_secret=app_secret,
                    user_name=str(token_obj.get("userName") or c_name).strip(),
                    expires_at=_normalize_epoch_seconds(
                        token_obj.get("expiresAt") or token_obj.get("expires_at")
                    ),
                    refresh_expires_at=_normalize_epoch_seconds(
                        token_obj.get("refreshExpiresAt") or token_obj.get("refresh_expires_at")
                    ),
                    scope=scope_str,
                    source="lark_cli",
                    updated_at=float(self._now()),
                    imported_fingerprint=_credential_fingerprint(access_token, refresh_token),
                )
                return self._merge_imported_record(record)

        # Fallback: plaintext token fields inside candidates
        for c_app, c_uid, c_name, meta in candidates:
            access_token = str(meta.get("accessToken") or meta.get("access_token") or "").strip()
            if not access_token:
                continue
            refresh_token = str(meta.get("refreshToken") or meta.get("refresh_token") or "").strip()
            raw_secret = meta.get("appSecret") or meta.get("app_secret")
            app_secret = raw_secret.strip() if isinstance(raw_secret, str) else ""
            scope_str = _normalize_scopes(meta.get("scope")) or fallback_scope
            record = FeishuUserTokenRecord(
                open_id=c_uid,
                app_id=c_app,
                access_token=access_token,
                refresh_token=refresh_token,
                app_secret=app_secret,
                user_name=c_name,
                expires_at=_normalize_epoch_seconds(
                    meta.get("expiresAt") or meta.get("expires_at")
                ),
                refresh_expires_at=_normalize_epoch_seconds(
                    meta.get("refreshExpiresAt") or meta.get("refresh_expires_at")
                ),
                scope=scope_str,
                source="lark_cli",
                updated_at=float(self._now()),
                imported_fingerprint=_credential_fingerprint(access_token, refresh_token),
            )
            return self._merge_imported_record(record)

        return None

    def resolve_token(
        self,
        *,
        open_id: str | None = None,
        app_id: str | None = None,
        auto_import_lark_cli: bool = True,
    ) -> FeishuUserTokenRecord | None:
        now_ts = float(self._now())
        record = self.get_token(open_id=open_id, app_id=app_id)
        if record is not None and not record.is_expired(now=now_ts):
            return record
        if auto_import_lark_cli:
            imported = self.import_from_lark_cli(app_id=app_id, open_id=open_id)
            if imported is not None:
                return imported
        return record

    async def aresolve_token(
        self,
        *,
        open_id: str | None = None,
        app_id: str | None = None,
        auto_import_lark_cli: bool = True,
    ) -> FeishuUserTokenRecord | None:
        """Resolve a user token under ``async_file_lock`` without blocking the event loop."""
        now_ts = float(self._now())
        record = self.get_token(open_id=open_id, app_id=app_id)
        if record is not None and not record.is_expired(now=now_ts):
            return record
        if not auto_import_lark_cli:
            return record
        imported = await self.aimport_from_lark_cli(app_id=app_id, open_id=open_id)
        return imported if imported is not None else record

    async def aimport_from_lark_cli(
        self,
        *,
        lark_cli_dir: Path | None = None,
        data_dir: Path | None = None,
        app_id: str | None = None,
        open_id: str | None = None,
        profile: str | None = None,
    ) -> FeishuUserTokenRecord | None:
        """Import a ``lark-cli`` user token under ``async_file_lock`` for the selected account."""
        init_app, init_open = self._resolve_lock_account(
            app_id=app_id or "",
            open_id=open_id or "",
            profile=profile or "",
            prefer_cli_config=True,
            lark_cli_dir=lark_cli_dir,
            data_dir=data_dir,
        )
        async with self.async_file_lock(app_id=init_app, open_id=init_open, resolve_account=False):
            resolved_app, resolved_open = self._resolve_lock_account(
                app_id=app_id or "",
                open_id=open_id or "",
                profile=profile or "",
                prefer_cli_config=True,
                lark_cli_dir=lark_cli_dir,
                data_dir=data_dir,
            )
            if not (resolved_app and resolved_open):
                return None
            async with self.async_file_lock(
                app_id=resolved_app, open_id=resolved_open, resolve_account=False
            ):
                return self.import_from_lark_cli(
                    lark_cli_dir=lark_cli_dir,
                    data_dir=data_dir,
                    app_id=resolved_app,
                    open_id=resolved_open,
                    profile=profile,
                )

    async def asave_oauth_token(
        self,
        *,
        open_id: str,
        app_id: str,
        access_token: str,
        refresh_token: str = "",
        user_name: str = "",
        expires_in: int | float | None = None,
        refresh_expires_in: int | float | None = None,
        scope: str = "",
        app_secret: str = "",
        source: str = "hub_oauth",
    ) -> FeishuUserTokenRecord:
        """Persist an OAuth user token under ``async_file_lock`` without blocking asyncio."""
        async with self.async_file_lock(app_id=app_id, open_id=open_id):
            return self.save_oauth_token(
                open_id=open_id,
                app_id=app_id,
                access_token=access_token,
                refresh_token=refresh_token,
                user_name=user_name,
                expires_in=expires_in,
                refresh_expires_in=refresh_expires_in,
                scope=scope,
                app_secret=app_secret,
                source=source,
            )

    async def asave_refreshed_token(
        self,
        previous: FeishuUserTokenRecord,
        refreshed: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> FeishuUserTokenRecord:
        """Persist a refreshed token under ``async_file_lock`` without blocking asyncio."""
        async with self.async_file_lock(
            app_id=previous.app_id, open_id=previous.open_id, create_cli_lock_dir=True
        ):
            return self.save_refreshed_token(previous, refreshed, now=now)


_DEFAULT_TOKEN_STORE: FeishuUserTokenStore | None = None


def get_feishu_user_token_store() -> FeishuUserTokenStore:
    global _DEFAULT_TOKEN_STORE
    if _DEFAULT_TOKEN_STORE is None:
        _DEFAULT_TOKEN_STORE = FeishuUserTokenStore()
    return _DEFAULT_TOKEN_STORE
