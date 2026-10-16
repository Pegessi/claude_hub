"""Local assistant image access and original-byte retention in the shared LRU.

The normal image reader is confined to cwd. Screenshots often live in the OS
temp directory instead: only an explicit Markdown image/link reference in
the canonical assistant stream, or a recognized image-view tool call, grants
access. User prompts and generic tool output never grant access.
No parent directory is added to the reader's allowed roots.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from ...models import AgentStreamEvent, ExecutionTarget, ManagedSession
from .attachments import AgentStreamAttachmentStore, AttachmentOwnerUnavailable
from .attachments import _magic_mime as sniff_image_mime
from .store import AgentStreamStore

logger = logging.getLogger(__name__)
_AGENT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
# Serialize source read + snapshot publication. Without it, an older read can
# finish after a newer one and overwrite the retained version. Weak values
# release idle locks instead of retaining one per image for the process life.
_SOURCE_LOCKS: weakref.WeakValueDictionary[
    Tuple[asyncio.AbstractEventLoop, str, str, str, str], asyncio.Lock
] = weakref.WeakValueDictionary()

_TEMP_IMAGE_ROOTS = (Path(tempfile.gettempdir()), Path("/tmp"))
_IMAGE_REFERENCE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s<>]+?))" r"(?:\s+[\"'][^\n]*?[\"'])?\s*\)"
)


def _temporary_file(raw_path: str) -> Optional[Path]:
    candidate = Path(raw_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    # /tmp is also a common explicit screenshot destination on macOS, where
    # tempfile.gettempdir() instead points into /var/folders.
    for root in _TEMP_IMAGE_ROOTS:
        for alias in {root, root.resolve()}:
            try:
                relative = candidate.relative_to(alias)
                real = candidate.resolve(strict=True)
                # Permit OS aliases (/tmp -> /private/tmp), but no symlink
                # files/directories below the temp root.
                if real == root.resolve() / relative and real.is_file():
                    return real
            except (OSError, ValueError, RuntimeError):
                continue
    return None


def referenced_temporary_image(session: ManagedSession, raw_path: str) -> Optional[Path]:
    """Return a single allowed real path, or None. Call from a worker thread."""
    if session.target != ExecutionTarget.LOCAL or not session.workspace_path:
        return None
    if not raw_path or len(raw_path) > 4096 or any(ord(ch) < 32 for ch in raw_path):
        return None
    try:
        real = _temporary_file(raw_path)
    except (OSError, ValueError, RuntimeError):
        return None
    if real is None:
        return None

    buffers: Dict[Tuple[str, str, str], str] = {}
    try:
        with AgentStreamStore(session.workspace_id, session.id).path.open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("session_id") != session.id:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("role", "assistant") != "assistant":
                    continue
                message_id = str(event.get("message_id") or "")
                if message_id.endswith(":user"):
                    continue
                kind = event.get("type")
                if kind == "tool_call_started":
                    if image_tool_path(payload) == raw_path:
                        return real
                    continue
                if kind == "turn_completed":
                    text = payload.get("assistant_text")
                elif kind == "text_delta":
                    text = payload.get("text")
                else:
                    continue
                if not isinstance(text, str):
                    continue
                if kind == "text_delta":
                    key = (
                        str(event.get("run_epoch")),
                        str(event.get("turn_id")),
                        message_id,
                    )
                    if payload.get("snapshot") is not True:
                        text = buffers.get(key, "") + text
                    # Preserve split destinations without retaining whole long
                    # conversations. No chunks from different messages join.
                    buffers[key] = text[-16384:]
                    if len(buffers) > 64:
                        del buffers[next(iter(buffers))]
                for match in _IMAGE_REFERENCE.finditer(text):
                    source = match.group(1) or match.group(2)
                    if unquote(html.unescape(source)) == raw_path:
                        return real
    except (OSError, UnicodeError):
        return None
    return None


class AgentImageUnavailable(ValueError):
    """Uniform denial of a local image, without exposing filesystem details."""


OwnerIsCurrent = Callable[[], bool]


def _require_current_owner(owner_is_current: Optional[OwnerIsCurrent]) -> None:
    if owner_is_current is not None and not owner_is_current():
        raise AgentImageUnavailable()


def image_tool_path(payload: Dict[str, Any]) -> Optional[str]:
    """Match the frontend's provider-independent image-view tool allowlist."""
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    name = payload.get("name")
    value = (
        args.get("path")
        if name == "view_image"
        else args.get("file_path") if name == "Read" else None
    )
    if not isinstance(value, str) or not value:
        return None
    if name == "Read" and re.search(r"\.(png|jpe?g|gif|webp)$", value, re.I) is None:
        return None
    return value


def _agent_image_allowed_roots(session: ManagedSession) -> List[Path]:
    """Resolved directories an agent image is permitted to live under.

    Scope is the session/tab's own working area — never the whole disk. For a
    terminal tab this is the launch ``cwd``; for a managed agent launched from
    an isolated feature worktree (``--cwd .``) it is that worktree. Remote
    sessions expose no local root and therefore cannot read through this path.
    """
    if getattr(session, "target", ExecutionTarget.LOCAL) != ExecutionTarget.LOCAL:
        return []
    root_raw = session.workspace_path
    if not root_raw:
        return []
    try:
        root = Path(root_raw).expanduser().resolve()
    except (OSError, ValueError):
        return []
    return [root]


def _read_agent_image(
    session: ManagedSession, raw_path: str, *, referenced_image: Optional[Path] = None
) -> Tuple[bytes, str]:
    """Validate *raw_path* as an agent image and return ``(bytes, mime)``.

    Raises :class:`AgentImageUnavailable` for an empty
    path, traversal/absolute/symlink escape, a missing file, a non-regular
    file, an oversized file, or non-image bytes. All containment checks happen
    after ``Path.resolve(strict=True)`` so symlinks are fully expanded before
    the allowlist comparison.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AgentImageUnavailable()
    # Reject control characters / NULs before touching the filesystem.
    if any(ord(ch) < 0x20 for ch in raw_path):
        raise AgentImageUnavailable()

    roots = _agent_image_allowed_roots(session)
    if not roots:
        raise AgentImageUnavailable()

    try:
        candidate = Path(raw_path).expanduser()
    except (OSError, ValueError):
        raise AgentImageUnavailable() from None

    try:
        is_absolute = candidate.is_absolute()
    except OSError:
        is_absolute = False

    # Relative paths are anchored at the session root only. An absolute path is
    # accepted inside cwd or as an exact temporary file already authorized
    # from this session's assistant output. Never authorize its parent root.
    anchors = [candidate] if is_absolute else [root / candidate for root in roots]

    resolved: Optional[Path] = None
    for anchor in anchors:
        try:
            real = anchor.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            continue
        # Only regular files (resolve() follows symlinks, so a link that points
        # outside the root fails the relative_to check that follows).
        if not real.is_file():
            continue
        if referenced_image is not None and real == referenced_image:
            resolved = real
            break
        for root in roots:
            try:
                real.relative_to(root)
            except ValueError:
                continue
            resolved = real
            break
        if resolved is not None:
            break

    if resolved is None:
        raise AgentImageUnavailable()

    try:
        with open(resolved, "rb") as handle:
            data = handle.read(_AGENT_IMAGE_MAX_BYTES + 1)
    except OSError:
        raise AgentImageUnavailable() from None

    if len(data) > _AGENT_IMAGE_MAX_BYTES:
        raise AgentImageUnavailable()

    mime = sniff_image_mime(data)
    if mime is None:
        raise AgentImageUnavailable()
    return data, mime


def _source_key(session: ManagedSession, raw_path: str) -> str:
    if session.target != ExecutionTarget.LOCAL or not session.workspace_path:
        raise AgentImageUnavailable()
    if not raw_path.strip() or len(raw_path) > 4096 or any(ord(ch) < 32 for ch in raw_path):
        raise AgentImageUnavailable()
    try:
        path = Path(raw_path).expanduser()
        if ".." in path.parts:
            raise AgentImageUnavailable()
        absolute = path if path.is_absolute() else Path(session.workspace_path).expanduser() / path
        # Lexical identity survives deletion; filesystem validation happens on
        # every source read. The store separately scopes the hash by session.
        return hashlib.sha256(os.path.abspath(absolute).encode("utf-8")).hexdigest()
    except (OSError, ValueError, UnicodeError):
        raise AgentImageUnavailable() from None


def _source_exists(session: ManagedSession, raw_path: str) -> bool:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(session.workspace_path or "") / path
    return os.path.lexists(path)


def _read_referenced_image(session: ManagedSession, raw_path: str) -> Tuple[bytes, str]:
    try:
        return _read_agent_image(session, raw_path)
    except AgentImageUnavailable:
        referenced = referenced_temporary_image(session, raw_path)
        if referenced is None:
            raise
        return _read_agent_image(session, raw_path, referenced_image=referenced)


async def read_cached_agent_image(
    session: ManagedSession,
    raw_path: str,
    *,
    owner_is_current: Optional[OwnerIsCurrent] = None,
) -> Tuple[bytes, str]:
    """Preserve original bytes in the existing attachment LRU, never re-encode.

    Live files take precedence so overwriting a screenshot does not freeze an
    old image. A missing source falls back to the latest retained version.
    Originals exceeding the configured storage bounds remain source-only;
    they are never silently downsampled to squeeze into the cache.
    """
    _require_current_owner(owner_is_current)
    key = _source_key(session, raw_path)
    identity = (
        asyncio.get_running_loop(),
        session.workspace_id,
        session.id,
        session.created_at.isoformat(),
        key,
    )
    lock = _SOURCE_LOCKS.setdefault(identity, asyncio.Lock())
    async with lock:
        _require_current_owner(owner_is_current)
        return await _read_cached_image_locked(
            session,
            raw_path,
            key,
            owner_is_current=owner_is_current,
        )


async def _read_cached_image_locked(
    session: ManagedSession,
    raw_path: str,
    key: str,
    *,
    owner_is_current: Optional[OwnerIsCurrent],
) -> Tuple[bytes, str]:
    store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    _require_current_owner(owner_is_current)
    try:
        data, mime = await asyncio.to_thread(_read_referenced_image, session, raw_path)
    except AgentImageUnavailable:
        _require_current_owner(owner_is_current)
        if await asyncio.to_thread(_source_exists, session, raw_path):
            # A replacement that is oversized, invalid or denied must not
            # resurrect the previous picture after that replacement is gone.
            try:
                await _discard_cached_source(
                    store,
                    key,
                    owner_is_current=owner_is_current,
                )
            except AttachmentOwnerUnavailable:
                raise AgentImageUnavailable() from None
            raise
        try:
            return await store.read_source(key, owner_is_current=owner_is_current)
        except (AttachmentOwnerUnavailable, KeyError, ValueError, OSError):
            raise AgentImageUnavailable() from None
    _require_current_owner(owner_is_current)
    try:
        await store.save(
            mime,
            data,
            source_key=key,
            owner_is_current=owner_is_current,
        )
    except AttachmentOwnerUnavailable:
        raise AgentImageUnavailable() from None
    except (ValueError, OSError):
        # Retention is best effort; a full-size source can still be viewed
        # when its dimensions/bytes exceed the existing cache's per-file cap.
        logger.debug("Agent image is source-only for session %s", session.id, exc_info=True)
        try:
            await _discard_cached_source(
                store,
                key,
                owner_is_current=owner_is_current,
            )
        except AttachmentOwnerUnavailable:
            raise AgentImageUnavailable() from None
    _require_current_owner(owner_is_current)
    return data, mime


async def _discard_cached_source(
    store: AgentStreamAttachmentStore,
    key: str,
    *,
    owner_is_current: Optional[OwnerIsCurrent],
) -> None:
    try:
        await store.discard_source(key, owner_is_current=owner_is_current)
    except OSError:
        # Storage failure must not hide an original already read successfully
        # or change a denied source into a server error.
        logger.debug("Could not invalidate retained agent image", exc_info=True)


def image_paths_from_event(event: AgentStreamEvent) -> List[str]:
    """Select explicit assistant image references without touching disk.

    Only canonical assistant text and recognized image-view calls participate.
    User prompts, generic shell output and other tools never grant access.
    """
    payload = event.payload
    if payload.get("role", "assistant") != "assistant" or (event.message_id or "").endswith(
        ":user"
    ):
        return []
    paths: List[str] = []
    if event.type == "tool_call_started":
        path = image_tool_path(payload)
        if path:
            paths.append(path)
    elif event.type in {"turn_completed", "text_delta"}:
        text = payload.get("assistant_text" if event.type == "turn_completed" else "text")
        if isinstance(text, str):
            return _image_paths_from_text(text)
    return list(
        dict.fromkeys(path for path in paths if re.search(r"\.(png|jpe?g|gif|webp)$", path, re.I))
    )


def _image_paths_from_text(text: str, *, after: int = 0) -> List[str]:
    paths = [
        unquote(html.unescape(match.group(1) or match.group(2)))
        for match in _IMAGE_REFERENCE.finditer(text)
        if match.end() > after
    ]
    return list(
        dict.fromkeys(path for path in paths if re.search(r"\.(png|jpe?g|gif|webp)$", path, re.I))
    )


class ImageReferenceBuffer:
    """Find complete references in persisted deltas, including split links.

    Completion summaries may be truncated, so capture as links become complete.
    Keep only bounded tails per message and avoid re-emitting old matches on
    every subsequent delta. Different messages/turns never share a prefix.
    """

    def __init__(self) -> None:
        self._tails: Dict[Tuple[str, str, str], str] = {}

    def paths(self, event: AgentStreamEvent) -> List[str]:
        turn = (str(event.run_epoch), str(event.turn_id))
        if event.type == "turn_completed":
            self._tails = {key: tail for key, tail in self._tails.items() if key[:2] != turn}
        if event.type != "text_delta":
            return image_paths_from_event(event)
        payload = event.payload
        if payload.get("role", "assistant") != "assistant" or (event.message_id or "").endswith(
            ":user"
        ):
            return []
        text = payload.get("text")
        if not isinstance(text, str):
            return []
        key = (*turn, str(event.message_id))
        prefix = "" if payload.get("snapshot") is True else self._tails.get(key, "")
        combined = prefix + text
        self._tails[key] = combined[-16384:]
        if len(self._tails) > 64:
            del self._tails[next(iter(self._tails))]
        return _image_paths_from_text(combined, after=len(prefix))


async def cache_event_images(
    session: ManagedSession,
    event: AgentStreamEvent,
    *,
    owner_is_current: Optional[OwnerIsCurrent] = None,
) -> None:
    """Capture live image output before temporary files can be cleaned up."""
    await cache_image_paths(
        session,
        image_paths_from_event(event),
        owner_is_current=owner_is_current,
    )


async def cache_image_paths(
    session: ManagedSession,
    paths: List[str],
    *,
    owner_is_current: Optional[OwnerIsCurrent] = None,
) -> None:
    """Cache references selected from this session's persisted assistant output."""
    for path in paths:
        try:
            await read_cached_agent_image(
                session,
                path,
                owner_is_current=owner_is_current,
            )
        except (AgentImageUnavailable, OSError, ValueError):
            continue
