"""Bounded agent-stream attachment image cache.

The cache stores the display image the browser uploads as the "preview": the
original image itself when it fits the per-image cap and dimension bound, or
a near-lossless re-encode otherwise. Data URLs are never stored. The backend
enforces a hard persisted byte cap and dimension bound.

Provider-specific original-image staging
----------------------------------------

* **Claude**: the provider send holds original image bytes in memory only; the
  only on-disk copy is the display image in this cache.
* **Codex**: the native provider requires transient local image files to pass
  to the subprocess. These are written to an app-owned temp directory with
  restrictive (0700) permissions and cleaned up on completion, failure, stop,
  and via age-based orphan GC. These temp files are separate from this cache,
  the event JSON, and the manifest.

Three explicit limits are enforced with least-recently-used (LRU) eviction:

* per-preview byte cap (default 8 MiB)
* per-session total bytes / count (default 1 GiB / 5000)
* global total bytes / count (default 1 GiB / 5000)

Limits are validated for consistency at construction: the per-preview cap may
not exceed either the per-session or the global byte budget, otherwise a
single valid preview could be immediately self-evicted.

Eviction order: session quota first, global quota second. Recency is the
manifest's ``last_used_at``: set on save and refreshed by reads at most once
per ``_LRU_TOUCH_INTERVAL_SECONDS`` so viewing history rarely rewrites the
manifest. Entries without it (older manifests) use ``created_at``.

The event payload stores only an opaque attachment id, mime type, byte size,
and optional dimensions — never a local path or raw bytes. The GET endpoint
validates the opaque id and session ownership; evicted/missing previews are
mapped to a stable placeholder by the API layer.

Storage layout::

    STATE_ROOT/
      agent_stream_attachments_index.json        # global manifest (atomic rewrite)
      agent_stream_attachment_previews/          # dedicated preview root
        <sha256(workspace_id)>/                  # hashed workspace component
          <sha256(session_id)>/                  # hashed session component
            <attachment_id>                      # raw preview bytes (UUID hex)

Security notes
--------------

* ``workspace_id`` and ``session_id`` are **never** interpolated into
  filesystem paths. The on-disk directory components are the lowercase
  hex SHA-256 digest of the raw id. The raw ids appear only in the
  in-memory index and the JSON manifest; a corrupt manifest entry can
  therefore never influence a filesystem path. Every path is resolved and
  confirmed to live under the dedicated preview root before any read or
  delete.
* Only PNG, JPEG, GIF, and WebP previews are accepted. The declared
  ``mime_type`` must match the magic bytes of the supplied preview; a
  mismatch is rejected. Client-supplied ``width``/``height`` are not
  trusted — the actual dimensions are parsed from the image header and
  must fall within ``1..MAX_PREVIEW_EDGE``.
* Directories are created mode 0700 and files mode 0600. Symlinks found
  anywhere under the preview root are unlinked (never followed) during GC.
  Directory names must be exactly 64 lowercase hex characters (a SHA-256
  digest); any other entry is removed.
* Index entries are normalized on read: missing or wrong-type fields,
  out-of-range dimensions, non-whitelisted MIME types, or non-UUID ids
  cause the entry to be dropped before it can affect eviction or serving.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .media import image_headers as _image_headers
from .media import index as _index
from .media import paths as _paths
from .media.dependencies import (
    DirectorySync,
    MediaDependencies,
    OwnerCheck,
    SessionDelete,
    StrictPathDelete,
    StrictSessionDelete,
)
from .media.errors import AttachmentOwnerUnavailable as AttachmentOwnerUnavailable
from .media.image_headers import _magic_mime as _magic_mime
from .media.image_headers import _parse_image_dimensions as _parse_image_dimensions
from .media.index import _lru_key as _lru_key
from .media.paths import _hash_component as _hash_component
from .media.paths import _sync_existing_directory as _sync_existing_directory
from .media.paths import _write_bytes_0600 as _write_bytes_0600
from .media.store import AgentStreamAttachmentStore as _AttachmentStore


def _state_root() -> Path:
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    return Path(wm.STATE_ROOT)


def _require_current_owner(owner_is_current: Optional[Callable[[], bool]]) -> None:
    if owner_is_current is not None and not owner_is_current():
        raise AttachmentOwnerUnavailable("attachment cache owner was deleted or replaced")


def _index_path() -> Path:
    return _state_root() / "agent_stream_attachments_index.json"


def _store_kwargs_from_settings() -> Dict[str, Any]:
    """Return the store limit kwargs from application settings.

    All ``AgentStreamAttachmentStore`` constructions must use these values so
    the cache limits are operationally configurable via environment variables
    (e.g. ``ATTACHMENT_MAX_PREVIEW_BYTES``) rather than hard-coded per call
    site.
    """
    from claude_hub.config import settings

    return {
        "max_preview_bytes": settings.attachment_max_preview_bytes,
        "max_session_count": settings.attachment_max_session_count,
        "max_session_bytes": settings.attachment_max_session_bytes,
        "max_global_count": settings.attachment_max_global_count,
        "max_global_bytes": settings.attachment_max_global_bytes,
        "max_age_seconds": settings.attachment_max_age_seconds,
    }


# Module-level lock serializes all index mutations so concurrent saves cannot
# exceed the global quota. Per-session state is also guarded by this lock
# because session eviction reads the global index.
#
# The backend guarantees a single owning process via ``BackendInstanceLock``
# (acquired at startup, held for the process lifetime), so this in-process
# lock is sufficient to serialize manifest read-modify-write across all
# coroutines.
#
# The lock is created lazily (on first use inside a running loop) because a
# module-level ``asyncio.Lock()`` may bind to an event loop that is later
# closed (e.g. across ``asyncio.run()`` calls in tests), rendering it
# unusable. ``_get_global_lock()`` always returns a lock bound to the
# currently-running loop; if the running loop has changed since the lock was
# last created (and the previous lock is not held), a fresh lock is created.
_global_lock: Optional[asyncio.Lock] = None
_global_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_global_lock() -> asyncio.Lock:
    global _global_lock, _global_lock_loop
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    # Recreate the lock if the running loop changed and the previous lock is
    # not currently held (a held lock must not be replaced mid-critical
    # section). In production the loop never changes, so this is a no-op;
    # it only matters for tests that call asyncio.run() repeatedly.
    if _global_lock is None or (running_loop is not None and running_loop is not _global_lock_loop):
        if _global_lock is None or not _global_lock.locked():
            _global_lock = asyncio.Lock()
            _global_lock_loop = running_loop
    return _global_lock


# ── path derivation via hashed components ───────────────────────────────────

# All attachment previews live under this dedicated root, separate from the
# rest of STATE_ROOT. A corrupt index can never write outside this directory
# because workspace_id / session_id are reduced to fixed-length hex digests
# before being used as path components.
_PREVIEW_ROOT_NAME = "agent_stream_attachment_previews"

# A SHA-256 hex digest is exactly 64 lowercase hex characters. Any directory
# entry under the preview root that does not match this pattern is treated as
# hostile (or stale) and removed during GC.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ── safe filesystem helpers (no symlink traversal) ──────────────────────────


# ── MIME whitelist + magic-byte validation ──────────────────────────────────

_ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Maximum edge length for a persisted preview; it bounds browser decode memory.
# The frontend stores originals within it and re-encodes larger images to fit.
# Client-supplied width/height is untrusted; we parse the actual dimensions
# from the image header and reject anything outside 1..MAX_PREVIEW_EDGE.
MAX_PREVIEW_EDGE = 8192

# Reads refresh LRU recency at most this often per entry.
_LRU_TOUCH_INTERVAL_SECONDS = 3600.0


# ── index entry normalization ───────────────────────────────────────────────


class _LegacyMediaDependencies(MediaDependencies):
    """Resolve legacy symbols at call time without duplicating cache state."""

    @property
    def _state_root(self) -> Callable[[], Path]:
        return _state_root

    @property
    def _attachments_root(self) -> Callable[[], Path]:
        return _attachments_root

    @property
    def _index_path(self) -> Callable[[], Path]:
        return _index_path

    @property
    def _get_global_lock(self) -> Callable[[], asyncio.Lock]:
        return _get_global_lock

    @property
    def _store_kwargs_from_settings(self) -> Callable[[], Dict[str, Any]]:
        return _store_kwargs_from_settings

    @property
    def _hash_component(self) -> Callable[[str], str]:
        return _hash_component

    @property
    def _is_hash_component(self) -> Callable[[str], bool]:
        return _is_hash_component

    @property
    def _session_dir_for(self) -> Callable[[str, str], Path]:
        return _session_dir_for

    @property
    def _ensure_session_dir(self) -> Callable[[str, str], Path]:
        return _ensure_session_dir

    @property
    def _resolve_attachment_path(self) -> Callable[[str, str, str], Path]:
        return _resolve_attachment_path

    @property
    def _write_bytes_0600(self) -> Callable[[Path, bytes], None]:
        return _write_bytes_0600

    @property
    def _session_dir_chain_is_real(self) -> Callable[[str, str], bool]:
        return _session_dir_chain_is_real

    @property
    def _sync_existing_directory(self) -> DirectorySync:
        return _sync_existing_directory

    @property
    def _safe_rmtree(self) -> StrictPathDelete:
        return _safe_rmtree

    @property
    def _safe_clear_session_dir(self) -> StrictSessionDelete:
        return _safe_clear_session_dir

    @property
    def _clear_session_dir_no_follow(self) -> SessionDelete:
        return _clear_session_dir_no_follow

    @property
    def _magic_mime(self) -> Callable[[bytes], Optional[str]]:
        return _magic_mime

    @property
    def _parse_image_dimensions(self) -> Callable[[bytes, str], Tuple[int, int]]:
        return _parse_image_dimensions

    @property
    def _validate_preview_mime(self) -> Callable[[str, bytes], None]:
        return _validate_preview_mime

    @property
    def _normalize_entry(self) -> Callable[[Any], Optional[Dict[str, Any]]]:
        return _normalize_entry

    @property
    def _lru_key(self) -> Callable[[Dict[str, Any]], Tuple[float, float]]:
        return _lru_key

    @property
    def _require_current_owner(self) -> OwnerCheck:
        return _require_current_owner

    @property
    def MAX_PREVIEW_EDGE(self) -> int:
        return MAX_PREVIEW_EDGE

    @property
    def _LRU_TOUCH_INTERVAL_SECONDS(self) -> float:
        return _LRU_TOUCH_INTERVAL_SECONDS

    @property
    def _ALLOWED_MIME_TYPES(self) -> frozenset[str]:
        return _ALLOWED_MIME_TYPES

    @property
    def _HEX64_RE(self) -> re.Pattern[str]:
        return _HEX64_RE

    @property
    def _PREVIEW_ROOT_NAME(self) -> str:
        return _PREVIEW_ROOT_NAME


def _is_hash_component(name: str) -> bool:
    return _paths._is_hash_component(_LegacyMediaDependencies(), name=name)


def _attachments_root() -> Path:
    return _paths._attachments_root(
        _LegacyMediaDependencies(),
    )


def _session_dir_for(workspace_id: str, session_id: str) -> Path:
    return _paths._session_dir_for(
        _LegacyMediaDependencies(), workspace_id=workspace_id, session_id=session_id
    )


def _ensure_session_dir(workspace_id: str, session_id: str) -> Path:
    return _paths._ensure_session_dir(
        _LegacyMediaDependencies(), workspace_id=workspace_id, session_id=session_id
    )


def _resolve_attachment_path(workspace_id: str, session_id: str, attachment_id: str) -> Path:
    return _paths._resolve_attachment_path(
        _LegacyMediaDependencies(),
        workspace_id=workspace_id,
        session_id=session_id,
        attachment_id=attachment_id,
    )


def _session_dir_chain_is_real(workspace_id: str, session_id: str) -> bool:
    return _paths._session_dir_chain_is_real(
        _LegacyMediaDependencies(), workspace_id=workspace_id, session_id=session_id
    )


def _safe_rmtree(path: Path, *, strict: bool = False) -> None:
    return _paths._safe_rmtree(_LegacyMediaDependencies(), path=path, strict=strict)


def _safe_clear_session_dir(workspace_id: str, session_id: str, *, strict: bool = False) -> None:
    return _paths._safe_clear_session_dir(
        _LegacyMediaDependencies(), workspace_id=workspace_id, session_id=session_id, strict=strict
    )


def _clear_session_dir_no_follow(workspace_id: str, session_id: str, *, strict: bool) -> None:
    return _paths._clear_session_dir_no_follow(
        _LegacyMediaDependencies(), workspace_id=workspace_id, session_id=session_id, strict=strict
    )


def _validate_preview_mime(mime_type: str, data: bytes) -> None:
    return _image_headers._validate_preview_mime(
        _LegacyMediaDependencies(), mime_type=mime_type, data=data
    )


def preview_within_cache_bounds(
    mime_type: str, preview_bytes: bytes, *, max_preview_bytes: Optional[int] = None
) -> bool:
    return _image_headers.preview_within_cache_bounds(
        _LegacyMediaDependencies(),
        mime_type=mime_type,
        preview_bytes=preview_bytes,
        max_preview_bytes=max_preview_bytes,
    )


def _normalize_entry(entry: Any) -> Optional[Dict[str, Any]]:
    return _index._normalize_entry(_LegacyMediaDependencies(), entry=entry)


class AgentStreamAttachmentStore(_AttachmentStore):
    """Legacy cache owner with live dependency and static manifest contracts."""

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        *,
        max_preview_bytes: Optional[int] = None,
        max_session_count: Optional[int] = None,
        max_session_bytes: Optional[int] = None,
        max_global_count: Optional[int] = None,
        max_global_bytes: Optional[int] = None,
        max_age_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(
            dependencies=_LegacyMediaDependencies(),
            workspace_id=workspace_id,
            session_id=session_id,
            max_preview_bytes=max_preview_bytes,
            max_session_count=max_session_count,
            max_session_bytes=max_session_bytes,
            max_global_count=max_global_count,
            max_global_bytes=max_global_bytes,
            max_age_seconds=max_age_seconds,
        )

    @staticmethod
    def _read_index(*, strict: bool = False) -> List[Dict[str, Any]]:
        return _index._read_index(_LegacyMediaDependencies(), strict=strict)

    @staticmethod
    def _write_index(entries: List[Dict[str, Any]]) -> None:
        _index._write_index(_LegacyMediaDependencies(), entries)


async def run_global_gc() -> None:
    """Run a single bounded global GC pass over the preview cache.

    Invoked at application startup/recovery to enforce per-session and global
    quotas, the optional age TTL, and to clean up orphan files and stale
    index entries left by a crashed process. The GC operates on the global
    index and the dedicated preview root; it never touches files outside
    ``STATE_ROOT/agent_stream_attachment_previews``.
    """
    # gc() operates on the global index/root regardless of the workspace/
    # session passed to the constructor, so any valid ids suffice.
    store = AgentStreamAttachmentStore("__gc__", "__gc__")
    await store.gc()
