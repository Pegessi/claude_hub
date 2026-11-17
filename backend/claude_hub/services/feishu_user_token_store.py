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

        self._ctx_new = lib.EVP_CIPHER_CTX_new
        self._ctx_new.restype = ctypes.c_void_p
        self._ctx_new.argtypes = []

        self._ctx_free = lib.EVP_CIPHER_CTX_free
        self._ctx_free.restype = None
        self._ctx_free.argtypes = [ctypes.c_void_p]

        self._aes_256_gcm = lib.EVP_aes_256_gcm
        self._aes_256_gcm.restype = ctypes.c_void_p
        self._aes_256_gcm.argtypes = []

        self._ctx_ctrl = lib.EVP_CIPHER_CTX_ctrl
        self._ctx_ctrl.restype = ctypes.c_int
        self._ctx_ctrl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]

        self._enc_init = lib.EVP_EncryptInit_ex
        self._enc_init.restype = ctypes.c_int
        self._enc_init.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]

        self._enc_update = lib.EVP_EncryptUpdate
        self._enc_update.restype = ctypes.c_int
        self._enc_update.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]

        self._enc_final = lib.EVP_EncryptFinal_ex
        self._enc_final.restype = ctypes.c_int
        self._enc_final.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
        ]

        self._dec_init = lib.EVP_DecryptInit_ex
        self._dec_init.restype = ctypes.c_int
        self._dec_init.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]

        self._dec_update = lib.EVP_DecryptUpdate
        self._dec_update.restype = ctypes.c_int
        self._dec_update.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]

        self._dec_final = lib.EVP_DecryptFinal_ex
        self._dec_final.restype = ctypes.c_int
        self._dec_final.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
        ]

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


def _seal_secret(key: bytes, value: str) -> str:
    if not value:
        return ""
    gcm = _get_aes_gcm()
    raw = value.encode("utf-8")
    if gcm is not None:
        blob = gcm.encrypt(key, raw)
        return _ENC_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")
    nonce = secrets.token_bytes(16)
    stream = bytearray()
    counter = 0
    while len(stream) < len(raw):
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    ct = bytes(a ^ b for a, b in zip(raw, stream[: len(raw)]))
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
        expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("Secret MAC verification failed")
        stream = bytearray()
        counter = 0
        while len(stream) < len(ct):
            block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            stream.extend(block)
            counter += 1
        return bytes(a ^ b for a, b in zip(ct, stream[: len(ct)])).decode("utf-8")
    # Plaintext migration compatibility
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
        if not isinstance(data, Mapping):
            return {}
        tokens = data.get("tokens")
        if not isinstance(tokens, Mapping):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for k, v in tokens.items():
            if isinstance(k, str) and isinstance(v, Mapping):
                out[k] = dict(v)
        return out

    def _save_raw(self, tokens: Mapping[str, dict[str, Any]]) -> None:
        write_document(
            self._path,
            {
                "version": _STORE_VERSION,
                "tokens": dict(tokens),
            },
        )

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
        clean_app = app_id.strip()
        clean_open = open_id.strip()
        lock_paths: list[Path] = []
        resolved_dirs = self._resolve_lark_cli_dirs_with_overrides()
        if resolved_dirs is not None and clean_app and clean_open:
            cfg_dir, _ = resolved_dirs
            cli_locks_dir = cfg_dir / "locks"
            if create_cli_lock_dir or cli_locks_dir.is_dir():
                lock_paths.append(cli_locks_dir / f"refresh_{clean_app}_{clean_open}.lock")
        hub_lock_name = (
            f"refresh_{clean_app}_{clean_open}.lock"
            if (clean_app and clean_open)
            else "feishu_user_tokens.refresh.lock"
        )
        hub_lock = self._key_path.parent / hub_lock_name
        if hub_lock not in lock_paths:
            lock_paths.append(hub_lock)

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
                    except Exception:
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
                        # preserve the newer CLI credentials + scopes instead of overwriting them.
                        cli_scope = _normalize_scopes(existing_obj.get("scope")) or record.scope
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

                merged_scope = _normalize_scopes(
                    [record.scope, str(existing_obj.get("scope") or "")]
                )
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
                        "scope": merged_scope or record.scope,
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
            existing = self.get_token(open_id=previous.open_id, app_id=previous.app_id) or previous
            new_access = str(refreshed.get("access_token") or "").strip()
            if not new_access:
                raise ValueError("Refreshed token payload is missing access_token")
            new_refresh = str(
                refreshed.get("refresh_token") or existing.refresh_token or previous.refresh_token
            ).strip()
            expires_in = refreshed.get("expires_in")
            refresh_expires_in = refreshed.get("refresh_expires_in") or refreshed.get(
                "refresh_token_expires_in"
            )
            expires_at = (
                current_now + float(expires_in)
                if isinstance(expires_in, (int, float)) and expires_in > 0
                else existing.expires_at
            )
            refresh_expires_at = (
                current_now + float(refresh_expires_in)
                if isinstance(refresh_expires_in, (int, float)) and refresh_expires_in > 0
                else existing.refresh_expires_at
            )
            next_gen = max(previous.generation, existing.generation) + 1
            prev_fp = _credential_fingerprint(previous.access_token, previous.refresh_token)
            exist_fp = _credential_fingerprint(existing.access_token, existing.refresh_token)
            expected_fps = {
                fp
                for fp in (
                    prev_fp,
                    previous.imported_fingerprint,
                    exist_fp,
                    existing.imported_fingerprint,
                )
                if fp
            }
            updated = replace(
                existing,
                access_token=new_access,
                refresh_token=new_refresh,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
                scope=_normalize_scopes(refreshed.get("scope")) or existing.scope,
                updated_at=current_now,
                generation=next_gen,
                imported_fingerprint=existing.imported_fingerprint or prev_fp,
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
        if lark_cli_dir is not None or data_dir is not None:
            cfg_dir = lark_cli_dir or self._lark_cli_dir or (data_dir or Path.home() / ".lark-cli")
            keychain_dir = data_dir or self._lark_cli_data_dir or cfg_dir
        else:
            resolved_dirs = self._resolve_default_lark_cli_dirs()
            if resolved_dirs is None:
                return None
            cfg_dir, keychain_dir = resolved_dirs

        target_app_id = (app_id or "").strip()
        target_open_id = (open_id or "").strip()
        target_profile = (profile or "").strip()

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

        # Read ~/.lark-cli/config.json if available
        candidates: list[tuple[str, str, str, dict[str, Any]]] = []
        config_path = cfg_dir / "config.json"
        has_config_apps = False
        if config_path.is_file():
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
            if isinstance(apps, list) and apps:
                has_config_apps = True
                valid_apps: list[Mapping[str, Any]] = [
                    entry
                    for entry in apps
                    if isinstance(entry, Mapping)
                    and str(entry.get("appId") or entry.get("app_id") or "").strip()
                ]
                selected_apps: list[Mapping[str, Any]] = []
                if target_profile or target_app_id:
                    selector = target_profile or target_app_id
                    for entry in valid_apps:
                        e_id = str(entry.get("appId") or entry.get("app_id") or "").strip()
                        e_name = str(entry.get("name") or entry.get("profile") or "").strip()
                        if target_app_id and e_id == target_app_id:
                            selected_apps.append(entry)
                        elif target_profile and selector in {e_id, e_name}:
                            selected_apps.append(entry)
                elif current_app:
                    for entry in valid_apps:
                        e_id = str(entry.get("appId") or entry.get("app_id") or "").strip()
                        e_name = str(entry.get("name") or entry.get("profile") or "").strip()
                        if current_app in {e_id, e_name}:
                            selected_apps.append(entry)
                            break
                    # Dangling currentApp must NOT fall back to apps[0]
                    if not selected_apps:
                        return None
                elif valid_apps:
                    selected_apps = [valid_apps[0]]

                for app_entry in selected_apps:
                    entry_app_id = str(
                        app_entry.get("appId") or app_entry.get("app_id") or ""
                    ).strip()
                    preferred_user = str(
                        app_entry.get("currentUser")
                        or app_entry.get("defaultUser")
                        or app_entry.get("userOpenId")
                        or ""
                    ).strip()
                    users = app_entry.get("users")
                    if isinstance(users, list) and users:
                        user_mappings = [u for u in users if isinstance(u, Mapping)]
                        if not target_open_id and preferred_user:
                            user_mappings.sort(
                                key=lambda u: (
                                    0
                                    if str(u.get("userOpenId") or u.get("open_id") or "").strip()
                                    == preferred_user
                                    else 1
                                )
                            )
                        for user_entry in user_mappings:
                            u_open_id = str(
                                user_entry.get("userOpenId") or user_entry.get("open_id") or ""
                            ).strip()
                            if not u_open_id:
                                continue
                            if target_open_id and u_open_id != target_open_id:
                                continue
                            u_name = str(
                                user_entry.get("userName") or user_entry.get("name") or ""
                            ).strip()
                            merged = {**dict(app_entry), **dict(user_entry)}
                            candidates.append((entry_app_id, u_open_id, u_name, merged))
                    else:
                        u_open_id = str(
                            app_entry.get("userOpenId") or app_entry.get("open_id") or ""
                        ).strip()
                        if u_open_id and (not target_open_id or u_open_id == target_open_id):
                            u_name = str(
                                app_entry.get("userName") or app_entry.get("name") or ""
                            ).strip()
                            candidates.append((entry_app_id, u_open_id, u_name, dict(app_entry)))

        # Check standalone plaintext token files in cfg_dir (tokens.json / user_token.json)
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
                    e_app = str(item.get("appId") or item.get("app_id") or target_app_id).strip()
                    e_uid = str(
                        item.get("userOpenId") or item.get("open_id") or target_open_id
                    ).strip()
                    if e_app and e_uid:
                        if target_app_id and e_app != target_app_id:
                            continue
                        if target_open_id and e_uid != target_open_id:
                            continue
                        e_name = str(item.get("userName") or item.get("user_name") or "").strip()
                        candidates.append((e_app, e_uid, e_name, dict(item)))

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

            # If no config.json apps were defined, scan *.enc files
            if not candidates and not has_config_apps:
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
                if not app_secret and isinstance(meta.get("appSecret"), str):
                    app_secret = meta["appSecret"].strip()

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
            imported = self.import_from_lark_cli(
                app_id=app_id or (record.app_id if record else None),
                open_id=open_id or (record.open_id if record else None),
            )
            if imported is not None:
                return imported
        return record


_DEFAULT_TOKEN_STORE: FeishuUserTokenStore | None = None


def get_feishu_user_token_store() -> FeishuUserTokenStore:
    global _DEFAULT_TOKEN_STORE
    if _DEFAULT_TOKEN_STORE is None:
        _DEFAULT_TOKEN_STORE = FeishuUserTokenStore()
    return _DEFAULT_TOKEN_STORE
