"""Encrypted 0600 credential store and ``~/.larkx/credentials.json`` importer for Feishu web sessions.

Stores Feishu web-client session cookies and CSRF tokens encrypted at rest with AES-256-GCM
(`0600` file permissions) and enforces strict secret redaction across `repr()`, `str()`,
and status/API serialization so live cookies or CSRF tokens are never logged or leaked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_hub.services.feishu_user_token_store import (
    _ensure_0600_key,
    _seal_secret,
    _unseal_secret,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home

logger = logging.getLogger(__name__)

REDACTED_SECRET = "[REDACTED]"


def normalize_web_region(region: str | None) -> str:
    raw = (region or "").strip().lower()
    if raw in {"lark", "larksuite", "larkoffice", "i18n", "overseas", "sg", "us"}:
        return "lark"
    return "feishu"


def infer_region_from_api_base_url(api_base_url: str | None) -> str | None:
    raw = (api_base_url or "").strip().lower()
    if not raw:
        return None
    if "larksuite.com" in raw or "larkoffice.com" in raw:
        return "lark"
    if "feishu.cn" in raw:
        return "feishu"
    return None


def is_web_session_authorized_for_owner(
    session: FeishuWebSession | None,
    owner_open_id: str | None = None,
    *,
    token_record: Any | None = None,
    api_base_url: str | None = None,
) -> tuple[bool, str | None]:
    """Verify whether ``session`` is authorized to act as ``owner_open_id`` / ``token_record``.

    Never infers app-scoped ``ou_...`` equivalence from configuration alone:
    if ``owner_open_id`` (or a non-web ``token_record.open_id``) is an ``ou_...`` OpenID,
    ``session.open_id`` must explicitly match it (or the operator must explicitly switch
    ``owner_open_id`` to the web session's identity).
    """
    if session is None or not session.is_valid_structure():
        return False, "no_web_session"

    sess_region = normalize_web_region(session.region)
    expected_region = infer_region_from_api_base_url(api_base_url)
    if expected_region is not None and sess_region != expected_region:
        return (
            False,
            f"web_session_tenant_mismatch:expected={expected_region},got={sess_region}",
        )

    rec_open = ""
    if (
        token_record is not None
        and str(getattr(token_record, "source", "") or "").strip() != "web_session"
    ):
        rec_open = str(getattr(token_record, "open_id", "") or "").strip()

    target_owner = (owner_open_id or "").strip() or rec_open
    sess_open = (session.open_id or "").strip()
    sess_uid = (session.user_id or "").strip()

    if not target_owner:
        return True, None

    if target_owner.startswith("ou_"):
        if sess_open and sess_open == target_owner:
            if rec_open and rec_open != sess_open:
                return (
                    False,
                    f"web_session_owner_mismatch:expected={rec_open},got={sess_open}",
                )
            return True, None
        return (
            False,
            f"web_session_owner_mismatch:expected={target_owner},got={sess_open or sess_uid or 'unverified'}",
        )

    # target_owner is a numeric web user_id (or custom non-ou_ identifier)
    if sess_open and sess_open != target_owner and sess_uid != target_owner:
        return (
            False,
            f"web_session_owner_mismatch:expected={target_owner},got={sess_open}",
        )
    if rec_open and rec_open != target_owner and sess_open != rec_open:
        return (
            False,
            f"web_session_owner_mismatch:expected={rec_open},got={sess_open or sess_uid or 'unverified'}",
        )
    if sess_uid == target_owner or sess_open == target_owner:
        return True, None
    return (
        False,
        f"web_session_owner_mismatch:expected={target_owner},got={sess_uid or sess_open or 'unverified'}",
    )


@dataclass(frozen=True)
class FeishuWebSession:
    """Signed-in Feishu web client session (compatible with ``lark-desk`` and ``~/.larkx/credentials.json``)."""

    region: str = "feishu"
    cookies: Mapping[str, str] = field(default_factory=dict, repr=False)
    saved_at: float = 0.0
    device_id: str = ""
    csrf_token: str = field(default="", repr=False)
    web_app_id: str = ""
    app_key: str = ""
    user_id: str = ""
    name: str = ""
    open_id: str = ""
    source: str = "qr_login"

    def __repr__(self) -> str:
        return (
            f"FeishuWebSession(region={self.region!r}, user_id={self.user_id!r}, "
            f"name={self.name!r}, open_id={self.open_id!r}, source={self.source!r}, "
            f"cookies={REDACTED_SECRET!r}, csrf_token={REDACTED_SECRET!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def domain(self) -> str:
        return "larksuite.com" if normalize_web_region(self.region) == "lark" else "feishu.cn"

    def accounts_origin(self) -> str:
        return f"https://accounts.{self.domain()}"

    def is_valid_structure(self) -> bool:
        return bool(
            isinstance(self.cookies, Mapping) and str(self.cookies.get("session") or "").strip()
        )

    def effective_device_id(self) -> str:
        return (self.device_id or str(self.cookies.get("passport_web_did") or "")).strip()

    def effective_csrf_token(self) -> str:
        return (self.csrf_token or str(self.cookies.get("swp_csrf_token") or "")).strip()

    def credential_fingerprint(self) -> str:
        sess_cookie = str(self.cookies.get("session") or "").strip()
        if not sess_cookie:
            return ""
        raw = f"{normalize_web_region(self.region)}:{sess_cookie}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def to_redacted_dict(self) -> dict[str, Any]:
        cookie_names = sorted(str(k) for k in (self.cookies or {}).keys() if str(k).strip())
        return {
            "has_session": self.is_valid_structure(),
            "region": normalize_web_region(self.region),
            "domain": self.domain(),
            "user_id": self.user_id or None,
            "name": self.name or None,
            "open_id": self.open_id or None,
            "device_id": self.effective_device_id() or None,
            "web_app_id": self.web_app_id or None,
            "saved_at": self.saved_at,
            "source": self.source,
            "cookie_names": cookie_names,
            "cookies": REDACTED_SECRET if cookie_names else None,
            "csrf_token": REDACTED_SECRET if self.effective_csrf_token() else None,
        }

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        default_source: str = "qr_login",
    ) -> FeishuWebSession | None:
        raw_cookies = data.get("cookies")
        if not isinstance(raw_cookies, Mapping):
            return None
        cookies = {
            str(k).strip(): str(v).strip()
            for k, v in raw_cookies.items()
            if str(k).strip() and v is not None and str(v).strip()
        }
        if not cookies.get("session"):
            return None
        region = normalize_web_region(str(data.get("region") or "feishu"))
        try:
            saved_at = float(data.get("saved_at") or 0.0)
        except (ValueError, TypeError):
            saved_at = 0.0
        device_id = str(data.get("device_id") or cookies.get("passport_web_did") or "").strip()
        csrf_token = str(data.get("csrf_token") or cookies.get("swp_csrf_token") or "").strip()
        return cls(
            region=region,
            cookies=cookies,
            saved_at=saved_at,
            device_id=device_id,
            csrf_token=csrf_token,
            web_app_id=str(data.get("web_app_id") or "").strip(),
            app_key=str(data.get("app_key") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            name=str(data.get("name") or "").strip(),
            open_id=str(data.get("open_id") or "").strip(),
            source=str(data.get("source") or default_source).strip() or default_source,
        )


def _atomic_write_0600(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class FeishuWebSessionStore:
    """Thread-safe encrypted store for the user's Feishu web session."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        key_path: Path | None = None,
        larkx_credentials_path: Path | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        runtime_home = resolve_runtime_home()
        self._custom_path = path is not None
        self._path = path or (runtime_home / "secrets" / "feishu_web_session.json")
        self._key_path = key_path or (self._path.parent / "feishu_web_session.key")
        self._larkx_path = larkx_credentials_path
        self._now = now
        self._lock = threading.RLock()
        self._generation: int = 0
        self._invalidated: bool = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def key_path(self) -> Path:
        return self._key_path

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def is_invalidated(self) -> bool:
        with self._lock:
            return self._invalidated

    def mark_invalid(self, session: FeishuWebSession | None = None) -> bool:
        """Mark the stored session as rejected/expired if it still matches ``session``."""
        with self._lock:
            if session is not None:
                current = self._read_stored_session()
                if current is None:
                    return False
                if current.credential_fingerprint() != session.credential_fingerprint():
                    return False
            self._invalidated = True
            self._generation += 1
            return True

    def _key(self) -> bytes:
        return _ensure_0600_key(self._key_path)

    def _default_larkx_path(self) -> Path | None:
        if self._larkx_path is not None:
            return self._larkx_path
        if self._custom_path:
            return None
        try:
            return Path.home() / ".larkx" / "credentials.json"
        except Exception:
            return None

    def load(self, *, auto_import_larkx: bool = False) -> FeishuWebSession | None:
        with self._lock:
            if self._invalidated:
                return None
            sess = self._read_stored_session()
            if sess is not None:
                return sess
            if auto_import_larkx:
                return self.import_from_larkx()
            return None

    def _read_stored_session(self) -> FeishuWebSession | None:
        if not self._path.is_file():
            return None
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        try:
            raw_obj = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse Feishu web session file %s", self._path.name)
            return None
        if not isinstance(raw_obj, Mapping):
            return None
        if raw_obj.get("invalidated") is True:
            return None

        decrypted_obj = dict(raw_obj)
        cookies_enc = raw_obj.get("cookies_enc")
        if isinstance(cookies_enc, str) and cookies_enc:
            try:
                key = self._key()
                cookies_json = _unseal_secret(key, cookies_enc)
                decrypted_obj["cookies"] = json.loads(cookies_json)
            except Exception:
                logger.warning(
                    "Failed to decrypt Feishu web session cookies in %s",
                    self._path.name,
                )
                return None

        csrf_enc = raw_obj.get("csrf_token_enc")
        if isinstance(csrf_enc, str) and csrf_enc:
            try:
                key = self._key()
                decrypted_obj["csrf_token"] = _unseal_secret(key, csrf_enc)
            except Exception:
                decrypted_obj["csrf_token"] = ""

        return FeishuWebSession.from_mapping(decrypted_obj)

    def save(
        self,
        session: FeishuWebSession | None,
        *,
        expected_generation: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> FeishuWebSession | None:
        with self._lock:
            if expected_generation is not None and self._generation != expected_generation:
                return None
            if session is None or not session.is_valid_structure():
                self.clear()
                return None
            current = self._read_stored_session()
            if expected_fingerprint is not None:
                if current is None or current.credential_fingerprint() != expected_fingerprint:
                    return None
            now_ts = float(self._now())
            normalized = replace(
                session,
                region=normalize_web_region(session.region),
                saved_at=session.saved_at or now_ts,
                device_id=session.effective_device_id(),
                csrf_token=session.effective_csrf_token(),
            )
            credential_changed = (
                current is None
                or current.credential_fingerprint() != normalized.credential_fingerprint()
            )
            key = self._key()
            cookies_json = json.dumps(dict(normalized.cookies), ensure_ascii=False, sort_keys=True)
            doc = {
                "version": 1,
                "region": normalized.region,
                "cookies_enc": _seal_secret(key, cookies_json),
                "csrf_token_enc": _seal_secret(key, normalized.csrf_token),
                "saved_at": normalized.saved_at,
                "device_id": normalized.device_id,
                "web_app_id": normalized.web_app_id,
                "app_key": normalized.app_key,
                "user_id": normalized.user_id,
                "name": normalized.name,
                "open_id": normalized.open_id,
                "source": normalized.source,
            }
            _atomic_write_0600(
                self._path,
                json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            if credential_changed or self._invalidated:
                self._generation += 1
            self._invalidated = False
            return normalized

    def clear(self) -> bool:
        with self._lock:
            self._generation += 1
            self._invalidated = True
            existed = self._path.exists()
            if not existed:
                return False
            try:
                self._path.unlink()
            except FileNotFoundError:
                return False
            except OSError:
                try:
                    self._path.write_bytes(b'{"invalidated": true}')
                except OSError:
                    pass
                raise
            if self._path.exists():
                raise OSError(f"Failed to remove Feishu web session file {self._path}")
            return True

    def import_from_larkx(self, path: Path | str | None = None) -> FeishuWebSession | None:
        """Import a session from ``~/.larkx/credentials.json`` (or explicit ``path``) into the encrypted store."""
        with self._lock:
            target = Path(path).expanduser() if path is not None else self._default_larkx_path()
            if target is None or not target.is_file():
                return None
            try:
                raw_data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Failed to read larkx credentials file %s", target)
                return None
            if not isinstance(raw_data, Mapping):
                return None
            sess = FeishuWebSession.from_mapping(raw_data, default_source="larkx_import")
            if sess is None:
                return None
            sess = replace(sess, source="larkx_import")
            return self.save(sess)


_DEFAULT_WEB_SESSION_STORE: FeishuWebSessionStore | None = None


def get_feishu_web_session_store() -> FeishuWebSessionStore:
    global _DEFAULT_WEB_SESSION_STORE
    if _DEFAULT_WEB_SESSION_STORE is None:
        _DEFAULT_WEB_SESSION_STORE = FeishuWebSessionStore()
    return _DEFAULT_WEB_SESSION_STORE
