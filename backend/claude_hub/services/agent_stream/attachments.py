"""Bounded agent-stream attachment preview cache.

The cache stores only browser-generated bounded previews — never the original
image bytes or data URLs. The original decoded bytes exist only for the active
provider send and are then released; the frontend is responsible for producing
the bounded preview (max edge 1024px) before upload, and the backend enforces
a hard persisted-preview byte cap.

Provider-specific original-image staging
----------------------------------------

* **Claude**: original image bytes are held in memory only for the duration of
  the provider send; they are never written to disk.
* **Codex**: the native provider requires transient local image files to pass
  to the subprocess. These are written to an app-owned temp directory with
  restrictive (0700) permissions and cleaned up on completion, failure, stop,
  and via age-based orphan GC. They are **never** written to the durable
  preview cache, the event JSON, or the manifest.

Three explicit limits are enforced with oldest-created-first (FIFO) eviction:

* per-preview byte cap (default 512 KiB)
* per-session total bytes / count (default 64 MiB / 200)
* global total bytes / count (default 512 MiB / 2000)

Limits are validated for consistency at construction: the per-preview cap may
not exceed either the per-session or the global byte budget, otherwise a
single valid preview could be immediately self-evicted.

Eviction order: session quota first, global quota second. FIFO uses immutable
creation order, not access time.

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
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _state_root() -> Path:
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    return Path(wm.STATE_ROOT)


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


def _is_hash_component(name: str) -> bool:
    return bool(_HEX64_RE.match(name))


def _attachments_root() -> Path:
    """The dedicated root directory under which all previews must live."""
    return _state_root() / _PREVIEW_ROOT_NAME


def _hash_component(raw: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``raw``.

    Raw workspace/session ids are never used as path components; only their
    digests are. This makes path traversal via a malicious id impossible
    regardless of what characters it contains.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _session_dir_for(workspace_id: str, session_id: str) -> Path:
    """Return the on-disk directory for a session's previews.

    The directory components are the SHA-256 digests of the workspace and
    session ids, so the raw ids never appear in the filesystem path.
    """
    return _attachments_root() / _hash_component(workspace_id) / _hash_component(session_id)


def _ensure_session_dir(workspace_id: str, session_id: str) -> Path:
    """Ensure the expected session directory exists and every ancestor
    component is a real directory (not a symlink).

    Walks from the preview root down to the expected session directory.
    Each component is inspected with ``lstat`` (via ``is_symlink``); if it
    is a symlink it is unlinked. **Unlink failures are not swallowed** —
    we fail closed rather than proceed with a symlinked ancestor that
    could redirect the subsequent write outside the expected session.

    Returns the expected (unresolved) session directory path. The caller
    can safely append an attachment id and write, because no component in
    the chain is a symlink at the time this returns.

    Locking note
    ------------
    The global attachment lock serializes this backend's own coroutines,
    and ``BackendInstanceLock`` guarantees only one backend process runs.
    Neither excludes an external same-user process from re-inserting a
    symlink between this call and the subsequent write. The cache is not a
    security boundary against a malicious process running as the same user;
    these checks prevent accidental traversal and fail closed for symlinks
    observed by this process.
    """
    root = _attachments_root()
    # The dedicated preview root must never be a symlink. If it is, fail
    # closed rather than unlinking it (it may be a legitimate mount point
    # we should not remove).
    if root.is_symlink():
        raise RuntimeError(f"preview root is a symlink: {root}")
    root.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass

    ws_hash = _hash_component(workspace_id)
    sess_hash = _hash_component(session_id)

    current = root
    for name in (ws_hash, sess_hash):
        comp = current / name
        if comp.is_symlink():
            # Attacker-placed symlink. Remove it; fail closed if we can't.
            comp.unlink()
        if comp.exists():
            if not comp.is_dir():
                # A non-directory, non-symlink entry (e.g. a regular file).
                # Remove it so we can create the directory.
                comp.unlink()
                comp.mkdir(mode=0o700)
            # else: real directory, leave it.
        else:
            comp.mkdir(mode=0o700)
        try:
            os.chmod(comp, 0o700)
        except OSError:
            pass
        current = comp

    return current  # == root / ws_hash / sess_hash


def _resolve_attachment_path(workspace_id: str, session_id: str, attachment_id: str) -> Path:
    """Build an attachment file path under the preview root.

    The workspace and session components are hashed; the attachment id is a
    UUID hex string produced by this module and is validated accordingly.

    Unlike the previous implementation, this does **not** call ``.resolve()``,
    which would follow symlinks at the workspace or session hash directory and
    silently redirect the path to another session's directory. The returned
    path is the *expected* path; callers must ensure the ancestor chain is
    real (via ``_ensure_session_dir`` for writes, or ``lstat`` checks for
    reads/deletes) before operating on it.

    Raises ``ValueError`` if the attachment id is not a valid hex UUID or the
    resolved path escapes the preview root.
    """
    # attachment_id is generated by us as uuid.uuid4().hex; validate it.
    try:
        uuid.UUID(attachment_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid attachment_id: {attachment_id!r}")

    root = _attachments_root()
    candidate = root / _hash_component(workspace_id) / _hash_component(session_id) / attachment_id
    # Containment check without following symlinks. All components are
    # fixed-format (64 hex chars for ws/sess, 32 hex for att id), so path
    # traversal is impossible; this is a defensive check.
    root_str = os.path.abspath(str(root))
    cand_str = os.path.abspath(str(candidate))
    if not cand_str.startswith(root_str + os.sep):
        raise ValueError(f"path escapes preview root: {candidate}")
    return candidate


# ── safe filesystem helpers (no symlink traversal) ──────────────────────────


def _write_bytes_0600(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with mode 0600.

    Uses a temp file + ``os.replace`` for atomicity. The temp file is created
    with ``os.open(..., 0o600)`` and then chmod'd to 0600 to defeat umask.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _session_dir_chain_is_real(workspace_id: str, session_id: str) -> bool:
    """Return True if every component from the preview root down to the
    session directory is a real directory (not a symlink, not a file).

    Used by read/delete paths to refuse to follow a symlink placed at the
    workspace or session hash component. A symlink there would redirect
    reads/deletes to another session's directory.
    """
    root = _attachments_root()
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    for name in (_hash_component(workspace_id), _hash_component(session_id)):
        root = root / name
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
    return True


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree without following symlinks.

    The preview layout has no subdirectories under the session directory, so
    we unlink every direct child (file or symlink) and then ``rmdir`` the
    directory. Symlinks are unlinked, never followed.

    Precondition: every ancestor of ``path`` must already be verified as a
    real directory (not a symlink) by the caller. This function does not
    re-check ancestors; it only protects against symlinks *inside* ``path``.
    """
    if not path.exists() or path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or entry.is_file(follow_symlinks=False):
            try:
                os.unlink(entry.path)
            except OSError:
                pass
        elif entry.is_dir(follow_symlinks=False):
            _safe_rmtree(Path(entry.path))
    try:
        os.rmdir(path)
    except OSError:
        pass


def _safe_clear_session_dir(workspace_id: str, session_id: str) -> None:
    """Remove a session's preview directory without following symlinks in the
    ancestor chain.

    ``lstat`` each component from the preview root down to the session
    directory. If any ancestor is a symlink, unlink that symlink **itself**
    and return — never recurse into the symlink target. Only when every
    ancestor is a real directory do we call ``_safe_rmtree`` on the session
    directory (which then protects against symlinks inside it).

    This prevents a symlink placed at the workspace hash directory (or the
    session hash directory) from causing ``clear`` to delete files outside
    the preview root.
    """
    root = _attachments_root()
    ws_hash = _hash_component(workspace_id)
    sess_hash = _hash_component(session_id)

    # Check the preview root itself.
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        # The root should never be a symlink; unlink it and stop.
        try:
            os.unlink(root)
        except OSError:
            pass
        return
    if not stat.S_ISDIR(st.st_mode):
        return

    # Check the workspace hash component.
    ws_dir = root / ws_hash
    try:
        st = os.lstat(ws_dir)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        # Workspace dir is a symlink. Unlink the symlink itself; do not
        # follow it into whatever it points at.
        try:
            os.unlink(ws_dir)
        except OSError:
            pass
        return
    if not stat.S_ISDIR(st.st_mode):
        # A non-directory entry (e.g. a file). Remove it.
        try:
            os.unlink(ws_dir)
        except OSError:
            pass
        return

    # Check the session hash component.
    sess_dir = ws_dir / sess_hash
    try:
        st = os.lstat(sess_dir)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        # Session dir is a symlink. Unlink it and return.
        try:
            os.unlink(sess_dir)
        except OSError:
            pass
        return
    if not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(sess_dir)
        except OSError:
            pass
        return

    # All ancestors are real directories. Now safely remove the session
    # directory's contents (unlinking any symlinks found inside).
    _safe_rmtree(sess_dir)


# ── MIME whitelist + magic-byte validation ──────────────────────────────────

_ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Maximum edge length for a persisted preview. The frontend is expected to
# resample to max edge 1024px before upload, but the client-supplied
# width/height is untrusted; we parse the actual dimensions from the image
# header and reject anything outside 1..MAX_PREVIEW_EDGE.
MAX_PREVIEW_EDGE = 1024


def _magic_mime(data: bytes) -> Optional[str]:
    """Return the MIME type implied by the image's magic bytes, or ``None``."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _parse_image_dimensions(data: bytes, mime_type: str) -> Tuple[int, int]:
    """Parse ``(width, height)`` from a supported image's header.

    Raises ``ValueError`` if the header is malformed or the format is
    unsupported. Only the header bytes are inspected; the full image is never
    decoded.
    """
    if mime_type == "image/png":
        # 8-byte signature, then 4-byte length, 4-byte "IHDR", then
        # 4-byte width (BE) and 4-byte height (BE).
        if len(data) < 24:
            raise ValueError("PNG too short to contain IHDR")
        if data[12:16] != b"IHDR":
            raise ValueError("PNG missing IHDR chunk")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height

    if mime_type == "image/gif":
        # 6-byte header ("GIF87a"/"GIF89a"), then 2-byte width (LE),
        # 2-byte height (LE).
        if len(data) < 10:
            raise ValueError("GIF too short to contain dimensions")
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return width, height

    if mime_type == "image/jpeg":
        # Scan for a SOF marker (0xFFC0..0xFFC3, excluding 0xFFC4/0xFFC8
        # which are DHT/DAC). The SOF segment layout is:
        #   0xFF 0xCn  segment_length(2 BE)  precision(1)  height(2 BE)  width(2 BE)
        i = 2  # skip the 0xFFD8 SOI marker
        while i + 9 < len(data):
            if data[i] != 0xFF:
                raise ValueError("JPEG marker byte not found at expected offset")
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xC3:
                # SOF0..SOF3: dimensions follow.
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                return width, height
            # Skip the marker segment: length is at i+2 (2 bytes BE) and
            # includes the length field itself.
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            i += 2 + seg_len
        raise ValueError("JPEG SOF marker not found")

    if mime_type == "image/webp":
        # RIFF header: "RIFF" size(4) "WEBP". Then a chunk with
        # "VP8 "/"VP8L"/"VP8X" fourcc.
        if len(data) < 30:
            raise ValueError("WebP too short to contain dimensions")
        fourcc = data[12:16]
        if fourcc == b"VP8 ":
            # Lossy: bytes 26-27 width (LE, mask 0x3FFF), 28-29 height.
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if fourcc == b"VP8L":
            # Lossless: byte 21 is 0x2F, then 4 bytes (LE) encode
            # width-1 (14 bits) and height-1 (14 bits).
            if data[20] != 0x2F:
                raise ValueError("WebP VP8L signature byte mismatch")
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        if fourcc == b"VP8X":
            # Extended: bytes 24-27 width (24-bit LE), 27-30 height.
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        raise ValueError(f"unsupported WebP chunk: {fourcc!r}")

    raise ValueError(f"cannot parse dimensions for unsupported mime {mime_type!r}")


def _validate_preview_mime(mime_type: str, data: bytes) -> None:
    """Reject non-whitelisted MIME types and magic-byte / declared-MIME mismatches."""
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise ValueError(
            f"unsupported mime type {mime_type!r}; allowed: {sorted(_ALLOWED_MIME_TYPES)}"
        )
    detected = _magic_mime(data)
    if detected is None:
        raise ValueError("preview bytes are not a recognized image (PNG/JPEG/GIF/WebP)")
    if detected != mime_type:
        raise ValueError(
            f"mime mismatch: declared {mime_type!r} but magic bytes indicate {detected!r}"
        )


# ── index entry normalization ───────────────────────────────────────────────


def _normalize_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalize a manifest entry.

    Returns a cleaned dict with all required fields of the correct type and
    range, or ``None`` if the entry is malformed. Malformed entries are
    dropped before they can affect eviction math (``size``/``created_at``)
    or serving (``mime_type``/``width``/``height``).
    """
    if not isinstance(entry, dict):
        return None

    att_id = entry.get("id")
    ws = entry.get("workspace_id")
    sess = entry.get("session_id")
    mime = entry.get("mime_type")
    size = entry.get("size")
    width = entry.get("width")
    height = entry.get("height")
    created_at = entry.get("created_at")

    if not isinstance(att_id, str):
        return None
    try:
        uuid.UUID(att_id)
    except (ValueError, AttributeError, TypeError):
        return None

    if not isinstance(ws, str) or not ws:
        return None
    if not isinstance(sess, str) or not sess:
        return None
    if not isinstance(mime, str) or mime not in _ALLOWED_MIME_TYPES:
        return None
    # ``size`` must be a positive int (not bool, not float).
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not (1 <= width <= MAX_PREVIEW_EDGE)
    ):
        return None
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or not (1 <= height <= MAX_PREVIEW_EDGE)
    ):
        return None
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return None
    # Reject NaN, inf, and negative timestamps so FIFO sorting and TTL
    # eviction cannot be poisoned by a corrupt manifest entry.
    if not math.isfinite(created_at) or created_at < 0:
        return None

    return {
        "id": att_id,
        "workspace_id": ws,
        "session_id": sess,
        "mime_type": mime,
        "size": size,
        "width": width,
        "height": height,
        "created_at": float(created_at),
    }


class AgentStreamAttachmentStore:
    """File-backed bounded preview cache for one agent session."""

    DEFAULT_MAX_PREVIEW_BYTES = 512 * 1024  # 512 KiB
    DEFAULT_MAX_SESSION_COUNT = 200
    DEFAULT_MAX_SESSION_BYTES = 64 * 1024 * 1024  # 64 MiB
    DEFAULT_MAX_GLOBAL_COUNT = 2000
    DEFAULT_MAX_GLOBAL_BYTES = 512 * 1024 * 1024  # 512 MiB

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
        # Limits default to application settings so every construction shares
        # the same operationally-configurable values. Explicit overrides
        # (e.g. in tests) are still honored.
        cfg = _store_kwargs_from_settings()
        if max_preview_bytes is None:
            max_preview_bytes = cfg["max_preview_bytes"]
        if max_session_count is None:
            max_session_count = cfg["max_session_count"]
        if max_session_bytes is None:
            max_session_bytes = cfg["max_session_bytes"]
        if max_global_count is None:
            max_global_count = cfg["max_global_count"]
        if max_global_bytes is None:
            max_global_bytes = cfg["max_global_bytes"]
        # max_age_seconds stays None (disabled) unless set in settings or
        # explicitly passed.
        if max_age_seconds is None:
            max_age_seconds = cfg["max_age_seconds"]

        if max_preview_bytes <= 0:
            raise ValueError("max_preview_bytes must be positive")
        if max_session_count <= 0:
            raise ValueError("max_session_count must be positive")
        if max_session_bytes <= 0:
            raise ValueError("max_session_bytes must be positive")
        if max_global_count <= 0:
            raise ValueError("max_global_count must be positive")
        if max_global_bytes <= 0:
            raise ValueError("max_global_bytes must be positive")
        if max_age_seconds is not None:
            if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
                raise ValueError("max_age_seconds must be a positive finite number or None")

        # Consistency: a single valid preview must fit within both the session
        # and global byte budgets, otherwise it would be immediately
        # self-evicted and ``save`` would return an id that no longer exists.
        if max_preview_bytes > max_session_bytes:
            raise ValueError(
                f"max_preview_bytes ({max_preview_bytes}) must not exceed "
                f"max_session_bytes ({max_session_bytes})"
            )
        if max_preview_bytes > max_global_bytes:
            raise ValueError(
                f"max_preview_bytes ({max_preview_bytes}) must not exceed "
                f"max_global_bytes ({max_global_bytes})"
            )

        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")

        self.workspace_id = workspace_id
        self.session_id = session_id
        self.max_preview_bytes = max_preview_bytes
        self.max_session_count = max_session_count
        self.max_session_bytes = max_session_bytes
        self.max_global_count = max_global_count
        self.max_global_bytes = max_global_bytes
        self.max_age_seconds = max_age_seconds

    @property
    def _session_dir(self) -> Path:
        return _session_dir_for(self.workspace_id, self.session_id)

    # ── index helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _read_index() -> List[Dict[str, Any]]:
        path = _index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        # Normalize every entry; drop anything malformed so it cannot poison
        # eviction math or serving.
        normalized: List[Dict[str, Any]] = []
        for raw in data:
            entry = _normalize_entry(raw)
            if entry is not None:
                normalized.append(entry)
        return normalized

    @staticmethod
    def _write_index(entries: List[Dict[str, Any]]) -> None:
        path = _index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        _write_bytes_0600(path, json.dumps(entries).encode("utf-8"))

    def _session_entries(self, index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            e
            for e in index
            if e.get("session_id") == self.session_id and e.get("workspace_id") == self.workspace_id
        ]

    # ── public API ────────────────────────────────────────────────────────

    async def save(
        self,
        mime_type: str,
        preview_bytes: bytes,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Persist a bounded preview and return its opaque metadata.

        The caller is responsible for supplying the bounded preview (the
        frontend resamples to max edge 1024px). The backend enforces the
        per-preview byte cap, the MIME whitelist, magic-byte consistency, and
        server-side dimension bounds (1..MAX_PREVIEW_EDGE).

        The client-supplied ``width``/``height`` are **not trusted**: the
        actual dimensions are parsed from the image header. If the caller
        supplies dimensions that disagree with the parsed values, the save is
        rejected (spoofing guard).

        Returns ``{id, mime_type, bytes, width, height}`` — no local path.

        Raises ``ValueError`` if the preview is too large, has an unsupported
        MIME type, its magic bytes do not match the declared MIME, its
        dimensions are outside 1..MAX_PREVIEW_EDGE, or the declared
        dimensions disagree with the parsed header.
        """
        if len(preview_bytes) > self.max_preview_bytes:
            raise ValueError(
                f"preview exceeds per-file cap: {len(preview_bytes)} > {self.max_preview_bytes} bytes"
            )
        _validate_preview_mime(mime_type, preview_bytes)

        # Parse dimensions from the header; never trust the client-supplied
        # width/height. Reject malformed headers and out-of-bounds dimensions.
        parsed_width, parsed_height = _parse_image_dimensions(preview_bytes, mime_type)
        if not (1 <= parsed_width <= MAX_PREVIEW_EDGE and 1 <= parsed_height <= MAX_PREVIEW_EDGE):
            raise ValueError(
                f"preview dimensions {parsed_width}x{parsed_height} outside "
                f"allowed range 1..{MAX_PREVIEW_EDGE}"
            )
        if width is not None and width != parsed_width:
            raise ValueError(f"declared width {width} does not match parsed width {parsed_width}")
        if height is not None and height != parsed_height:
            raise ValueError(
                f"declared height {height} does not match parsed height {parsed_height}"
            )

        async with _get_global_lock():
            index = self._read_index()
            attachment_id = uuid.uuid4().hex
            created_at = time.time()

            # Ensure the expected session directory chain is real (no symlinks
            # at the workspace or session hash component) BEFORE building the
            # target path. ``_ensure_session_dir`` unlinks any symlink it finds
            # and fails closed if unlink fails, so we never write through a
            # symlink into another session's directory.
            session_dir = _ensure_session_dir(self.workspace_id, self.session_id)
            target = session_dir / attachment_id
            # Containment check (defense in depth; components are hashed so
            # this can only fail if _attachments_root itself is misconfigured).
            root = _attachments_root()
            if not os.path.abspath(str(target)).startswith(os.path.abspath(str(root)) + os.sep):
                raise ValueError(f"path escapes preview root: {target}")

            entry = {
                "id": attachment_id,
                "session_id": self.session_id,
                "workspace_id": self.workspace_id,
                "mime_type": mime_type,
                "size": len(preview_bytes),
                "width": parsed_width,
                "height": parsed_height,
                "created_at": created_at,
            }

            # Publish the file and the manifest atomically: if the manifest
            # write fails (or anything else raises before the index is
            # persisted), unlink the preview file immediately so we do not
            # leave an orphan that can only be reclaimed at restart. The
            # caller has no id to roll back, so the store must self-clean.
            try:
                _write_bytes_0600(target, preview_bytes)
                index.append(entry)
                self._write_index(index)
            except Exception:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

            # Eviction: age TTL first, then session quota, then global quota.
            #
            # Invariant: on return from save (success or failure), the on-disk
            # state must not have grown beyond the quota. If eviction fails
            # (e.g. victim unlink or manifest rewrite raises), we remove the
            # newly-saved entry and its file, then persist the remaining
            # index. Earlier successfully-evicted victims may stay gone —
            # that is acceptable, the key invariant is no net growth.
            #
            # Performance: ``_evict_aged``, ``_evict_session`` and
            # ``_evict_global`` return whether they removed anything and do
            # not persist the manifest. We write the index once after
            # publication, then again only if eviction actually changed the
            # index. An under-quota save therefore incurs exactly one
            # manifest rewrite. When ``max_age_seconds`` is ``None``,
            # ``_evict_aged`` returns immediately without scanning.
            try:
                changed = self._evict_aged(index)
                changed = self._evict_session(index) or changed
                changed = self._evict_global(index) or changed
                if changed:
                    self._write_index(index)
            except Exception:
                # Roll back the new entry: remove it from the index and disk,
                # then persist the remaining (possibly partially-evicted)
                # index. This guarantees no net growth even if eviction
                # repeatedly fails.
                index[:] = [e for e in index if e.get("id") != attachment_id]
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                self._write_index(index)
                raise

            # Defensive: with consistent limits a single preview can never be
            # self-evicted, but verify anyway so we never return success for an
            # id that no longer exists.
            if not any(e.get("id") == attachment_id for e in index):
                # Should be unreachable given the construction-time consistency
                # check; fail closed rather than returning a dangling id.
                try:
                    target.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    "newly saved preview was immediately evicted; "
                    "increase session/global byte quota relative to max_preview_bytes"
                )

            return {
                "id": attachment_id,
                "mime_type": mime_type,
                "bytes": len(preview_bytes),
                "width": parsed_width,
                "height": parsed_height,
            }

    async def read(self, attachment_id: str) -> Tuple[bytes, str]:
        """Return ``(preview_bytes, mime_type)`` for an existing attachment.

        Raises ``KeyError`` if the attachment is missing or evicted. The API
        layer maps this to a stable placeholder response.
        """
        self._validate_id(attachment_id)
        async with _get_global_lock():
            index = self._read_index()
            entry = next(
                (
                    e
                    for e in index
                    if e.get("id") == attachment_id
                    and e.get("session_id") == self.session_id
                    and e.get("workspace_id") == self.workspace_id
                ),
                None,
            )
            if entry is None:
                raise KeyError(attachment_id)
            # Refuse to follow a symlink placed at the workspace or session
            # hash component: it would redirect the read to another session's
            # directory. Treat as missing.
            if not _session_dir_chain_is_real(self.workspace_id, self.session_id):
                raise KeyError(attachment_id)
            # Build the path from hashed components; the raw ids from the
            # index are never interpolated into the filesystem path.
            try:
                path = _resolve_attachment_path(
                    entry["workspace_id"], entry["session_id"], entry["id"]
                )
            except ValueError:
                # Corrupt index entry (bad attachment id) — drop it and fail
                # closed.
                index = [e for e in index if e.get("id") != attachment_id]
                self._write_index(index)
                raise KeyError(attachment_id)
            if path.is_symlink() or not path.is_file():
                # Index says it exists but the entry is gone, is a symlink,
                # or is an unexpected directory/device. Treat it as stale;
                # preview reads serve regular files only.
                index = [e for e in index if e.get("id") != attachment_id]
                self._write_index(index)
                raise KeyError(attachment_id)
            return path.read_bytes(), entry["mime_type"]

    async def _delete_by_id(self, attachment_id: str) -> None:
        """Delete a single preview by id from the index and disk.

        Used by the tailer for cleanup when a turn fails before the
        ``turn_started`` event is durably published. The entry is looked up
        in the global index by id; its path is resolved from the hashed
        workspace/session components stored in the index entry.
        """
        async with _get_global_lock():
            index = self._read_index()
            entry = next((e for e in index if e.get("id") == attachment_id), None)
            if entry is None:
                return
            # If the workspace/session ancestor chain contains a symlink, do
            # not follow it to delete a file in another session. Just drop the
            # index entry; GC will clean up the symlink.
            if not _session_dir_chain_is_real(entry["workspace_id"], entry["session_id"]):
                index[:] = [e for e in index if e.get("id") != attachment_id]
                self._write_index(index)
                return
            try:
                path = _resolve_attachment_path(
                    entry["workspace_id"], entry["session_id"], entry["id"]
                )
            except ValueError:
                path = None
            if path is not None:
                try:
                    if path.is_symlink():
                        os.unlink(path)
                    elif path.is_dir():
                        _safe_rmtree(path)
                    else:
                        path.unlink()
                except FileNotFoundError:
                    pass
            index[:] = [e for e in index if e.get("id") != attachment_id]
            self._write_index(index)

    async def clear(self) -> None:
        """Remove all previews for this session (called on session deletion).

        The session directory is derived from hashed workspace/session ids,
        so it is safe to remove wholesale. We use ``_safe_clear_session_dir``
        which ``lstat``s every ancestor component (root → workspace hash →
        session hash) and, if any is a symlink, unlinks that symlink itself
        and returns without recursing. Only when every ancestor is a real
        directory does it remove the session directory's contents. This
        prevents a symlink at the workspace or session hash component from
        causing deletion outside the preview root.
        """
        async with _get_global_lock():
            index = self._read_index()
            index = [
                e
                for e in index
                if not (
                    e.get("session_id") == self.session_id
                    and e.get("workspace_id") == self.workspace_id
                )
            ]
            self._write_index(index)
            # Remove the entire hashed session directory. We never iterate
            # over manifest-derived attachment ids here.
            _safe_clear_session_dir(self.workspace_id, self.session_id)

    async def gc(self) -> None:
        """Global garbage collection.

        Deletes orphan files (present on disk but absent from the index) and
        stale index entries (present in the index but absent from disk, or
        with unsafe path components). Enforces both per-session and global
        quotas by evicting oldest-first. Also enforces the optional age TTL.

        A corrupt index entry can never escape the preview root: workspace
        and session ids are reduced to SHA-256 hex digests before being used
        as path components, and every path is resolved under the dedicated
        preview root before any read or delete. Symlinks found under the
        preview root are unlinked (never followed); directory names that are
        not exactly 64 lowercase hex characters are removed.
        """
        async with _get_global_lock():
            index = self._read_index()
            root = _attachments_root()

            # Fail closed on a symlinked preview root. ``lstat`` does not
            # follow symlinks, so we detect a symlink (including a dangling
            # one) at the root itself. If the root is a symlink, unlink the
            # link itself (never its target) and clear the manifest — we
            # must not ``scandir`` the target, which could be anywhere on
            # the filesystem and whose contents we would otherwise delete.
            # A subsequent ``save`` recreates the root as a real directory.
            try:
                root_st = os.lstat(root)
            except FileNotFoundError:
                root_st = None
            if root_st is not None and stat.S_ISLNK(root_st.st_mode):
                try:
                    os.unlink(root)
                except OSError:
                    pass
                self._write_index([])
                return

            # Drop index entries whose on-disk file is missing or whose path
            # would escape the preview root.
            valid: List[Dict[str, Any]] = []
            for entry in index:
                try:
                    path = _resolve_attachment_path(
                        entry["workspace_id"], entry["session_id"], entry["id"]
                    )
                except (ValueError, KeyError):
                    continue
                # Refuse to follow symlinks at the workspace or session hash
                # component: ``path.exists()`` would follow them and mark an
                # external file (reachable through the symlink) as valid.
                # Without this check, the entry would survive in ``valid``
                # and a later ``_delete_entry`` (during quota/age eviction)
                # could unlink the external file if the symlink is
                # re-inserted between GC phases.
                if not _session_dir_chain_is_real(entry["workspace_id"], entry["session_id"]):
                    continue
                if not path.is_symlink() and path.is_file():
                    valid.append(entry)

            # Delete orphan files: files on disk not referenced by any valid
            # index entry. Directory components are SHA-256 hex digests, so we
            # compare (hash(ws), hash(sess), att_id) tuples.
            valid_keys = {
                (
                    _hash_component(e["workspace_id"]),
                    _hash_component(e["session_id"]),
                    e["id"],
                )
                for e in valid
            }
            # ``root`` is now known to be a real directory (or absent);
            # ``exists`` is safe because we already rejected a symlinked root.
            if root.exists():
                self._gc_scan_root(root, valid_keys)

            # Age-based eviction: drop entries older than max_age_seconds
            # (if configured). This runs before quota eviction so aged
            # previews are removed regardless of whether quotas are exceeded.
            # ``_evict_aged`` is a no-op when ``max_age_seconds`` is ``None``.
            # It does not persist; the manifest is written once at the end.
            self._evict_aged(valid)

            # Enforce per-session quotas for every session present in the
            # index, then the global quota. Session eviction runs first so a
            # session that exceeds its own budget is trimmed before we consider
            # the global pool. Both helpers return whether they changed the
            # index and do not persist.
            self._evict_all_sessions(valid)
            self._evict_global(valid)

            # Single final manifest write covering orphan cleanup, age
            # eviction, and quota eviction.
            self._write_index(valid)

    def _gc_scan_root(self, root: Path, valid_keys: set) -> None:
        """Walk the preview root, removing symlinks, non-hash directories,
        and orphan files. Never follows symlinks.
        """
        try:
            ws_entries = list(os.scandir(root))
        except OSError:
            return

        for ws_entry in ws_entries:
            # Unlink symlinks immediately — they could escape the root.
            if ws_entry.is_symlink():
                try:
                    os.unlink(ws_entry.path)
                except OSError:
                    pass
                continue
            if not ws_entry.is_dir(follow_symlinks=False):
                # A stray file at the workspace level — remove it.
                try:
                    os.unlink(ws_entry.path)
                except OSError:
                    pass
                continue
            if not _is_hash_component(ws_entry.name):
                # Not a valid SHA-256 digest directory — remove it entirely.
                _safe_rmtree(Path(ws_entry.path))
                continue

            try:
                sess_entries = list(os.scandir(ws_entry.path))
            except OSError:
                continue

            for sess_entry in sess_entries:
                if sess_entry.is_symlink():
                    try:
                        os.unlink(sess_entry.path)
                    except OSError:
                        pass
                    continue
                if not sess_entry.is_dir(follow_symlinks=False):
                    try:
                        os.unlink(sess_entry.path)
                    except OSError:
                        pass
                    continue
                if not _is_hash_component(sess_entry.name):
                    _safe_rmtree(Path(sess_entry.path))
                    continue

                try:
                    file_entries = list(os.scandir(sess_entry.path))
                except OSError:
                    continue

                for f_entry in file_entries:
                    if f_entry.is_symlink():
                        try:
                            os.unlink(f_entry.path)
                        except OSError:
                            pass
                        continue
                    if f_entry.is_dir(follow_symlinks=False):
                        # Attachment ids name regular files only. Remove an
                        # unexpected directory without following any links it
                        # contains, even if its name matches a valid id.
                        _safe_rmtree(Path(f_entry.path))
                        continue
                    if not f_entry.is_file(follow_symlinks=False):
                        try:
                            os.unlink(f_entry.path)
                        except OSError:
                            pass
                        continue
                    if f_entry.name.endswith(".tmp"):
                        # Leftover temp file from a crashed write.
                        try:
                            os.unlink(f_entry.path)
                        except OSError:
                            pass
                        continue
                    key = (ws_entry.name, sess_entry.name, f_entry.name)
                    if key not in valid_keys:
                        try:
                            os.unlink(f_entry.path)
                        except OSError:
                            pass

                # Remove empty session directories.
                try:
                    os.rmdir(sess_entry.path)
                except OSError:
                    pass

            # Remove empty workspace directories.
            try:
                os.rmdir(ws_entry.path)
            except OSError:
                pass

    # ── eviction ──────────────────────────────────────────────────────────

    def _evict_session(self, index: List[Dict[str, Any]]) -> bool:
        """Evict oldest-first within this session until session quota holds.

        Returns ``True`` if at least one entry was removed, ``False``
        otherwise. Does **not** persist the index; the caller writes the
        manifest once after all eviction passes so an under-quota save
        incurs only the single publication write.
        """
        session_entries = self._session_entries(index)
        session_bytes = sum(e["size"] for e in session_entries)
        # Sort by immutable created_at (FIFO).
        session_entries.sort(key=lambda e: e["created_at"])

        changed = False
        while session_entries and (
            len(session_entries) > self.max_session_count or session_bytes > self.max_session_bytes
        ):
            victim = session_entries.pop(0)
            session_bytes -= victim["size"]
            self._delete_entry(index, victim, persist=False)
            changed = True
        return changed

    def _evict_all_sessions(self, index: List[Dict[str, Any]]) -> bool:
        """Enforce per-session quotas for every session in the index.

        Used by ``gc`` during recovery: the persisted set may have been
        written under different (larger) per-session limits, so we trim each
        session to the current budget. Returns ``True`` if any entry was
        removed. Deletions are batched: the manifest is **not** rewritten
        here; the caller persists once at the end.
        """
        # Group entries by (workspace_id, session_id).
        sessions: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for e in index:
            key = (e["workspace_id"], e["session_id"])
            sessions.setdefault(key, []).append(e)

        changed = False
        for (ws, sess), entries in sessions.items():
            entries.sort(key=lambda e: e["created_at"])
            total = sum(e["size"] for e in entries)
            while entries and (
                len(entries) > self.max_session_count or total > self.max_session_bytes
            ):
                victim = entries.pop(0)
                total -= victim["size"]
                self._delete_entry(index, victim, persist=False)
                changed = True
        return changed

    def _evict_global(self, index: List[Dict[str, Any]]) -> bool:
        """Evict oldest-first globally until global quota holds.

        Returns ``True`` if at least one entry was removed, ``False``
        otherwise. Does not persist the index.
        """
        global_bytes = sum(e["size"] for e in index)
        index.sort(key=lambda e: e["created_at"])

        changed = False
        while index and (
            len(index) > self.max_global_count or global_bytes > self.max_global_bytes
        ):
            victim = index.pop(0)
            global_bytes -= victim["size"]
            self._delete_entry(index, victim, already_removed=True, persist=False)
            changed = True
        return changed

    def _evict_aged(self, index: List[Dict[str, Any]]) -> bool:
        """Evict entries older than ``max_age_seconds``.

        Returns ``True`` if at least one entry was removed, ``False``
        otherwise. When ``max_age_seconds`` is ``None`` (age TTL disabled),
        this is a no-op that returns ``False`` immediately — no index scan,
        no extra manifest writes.

        Does not persist the index; the caller writes the manifest once
        after all eviction passes.
        """
        if self.max_age_seconds is None:
            return False
        now = time.time()
        changed = False
        for e in list(index):
            if now - float(e["created_at"]) > self.max_age_seconds:
                self._delete_entry(index, e, persist=False)
                changed = True
        return changed

    def _delete_entry(
        self,
        index: List[Dict[str, Any]],
        entry: Dict[str, Any],
        *,
        already_removed: bool = False,
        persist: bool = True,
    ) -> None:
        """Remove one entry from disk and (if not already) from the index.

        Fail-closed symlink policy: before any disk operation, verify that
        the workspace → session ancestor chain is composed of real
        directories (no symlinks). If any ancestor is a symlink (or not a
        directory), the resolved path could redirect to another session's
        directory or outside the preview root, so we do **not** touch disk —
        we only drop the index entry. ``_gc_scan_root`` is responsible for
        unlinking the symlink itself.

        When ``persist`` is True (the default), the manifest is rewritten
        after removal. Callers that delete multiple entries in a loop
        (eviction, GC) should pass ``persist=False`` and call
        ``_write_index`` once at the end to avoid O(n^2) manifest rewrites.
        """
        ws = entry["workspace_id"]
        sess = entry["session_id"]
        att_id = entry["id"]
        # Fail closed: if the workspace/session ancestor chain contains a
        # symlink (or is not a real directory), do NOT operate on disk — the
        # path could redirect to another session's directory or outside the
        # preview root. Just drop the index entry; GC will clean up any
        # symlink at the workspace/session level.
        if not _session_dir_chain_is_real(ws, sess):
            if not already_removed:
                index[:] = [e for e in index if e.get("id") != entry["id"]]
            if persist:
                self._write_index(index)
            return
        try:
            path = _resolve_attachment_path(ws, sess, att_id)
        except ValueError:
            # Invalid attachment id — nothing to delete on disk, just drop
            # from the index.
            path = None
        if path is not None:
            try:
                if path.is_symlink():
                    os.unlink(path)
                elif path.is_dir():
                    _safe_rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
        if not already_removed:
            index[:] = [e for e in index if e.get("id") != entry["id"]]
        if persist:
            self._write_index(index)

    # ── validation ────────────────────────────────────────────────────────

    @staticmethod
    def _validate_id(attachment_id: str) -> None:
        """Reject ids that could escape the session directory.

        Valid ids are hex UUIDs (no path separators, no parent references).
        """
        if not attachment_id or "/" in attachment_id or "\\" in attachment_id:
            raise KeyError(attachment_id)
        if ".." in attachment_id:
            raise KeyError(attachment_id)
        # Must be a valid hex UUID (32 hex chars).
        try:
            uuid.UUID(attachment_id)
        except (ValueError, AttributeError):
            raise KeyError(attachment_id)


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
