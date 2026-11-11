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
import logging
import os
import re
import stat
import tempfile
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from ...models import AgentStreamEvent, AgentType, ExecutionTarget, ManagedSession
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
        events = AgentStreamStore(session.workspace_id, session.id).iter_events()
        for stored_event in events:
            event = stored_event.model_dump(mode="json")
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


# Status recorded on a canonical tool_call_completed event when the tool ran
# successfully; anything else (failed/cancelled/missing) grants nothing.
_COMPLETED_TOOL_STATUS = "completed"

# B1 (threat review): a generic ``status == "completed"`` does not prove the
# tool read an image for every producer — Codex transcript outputs hard-code
# "completed" and MASO omits its success flag. Only (producer, tool) pairs
# whose failure encoding is verified in the native adapter are eligible for
# an out-of-cwd grant; everything else fails closed. Extend this table only
# with adapter-level evidence, never from a status flag alone.
#   * Claude JSONL ``tool_result`` sets failed from ``is_error``
#     (claude_jsonl.py), so a completed Claude image Read is a real success.
_PROVEN_IMAGE_TOOL_SUCCESS: frozenset = frozenset({(AgentType.CLAUDE, "Read")})

# Where an authorized image came from; drives cache/revocation policy.
IMAGE_SOURCE_CWD = "cwd"
IMAGE_SOURCE_TEMPORARY = "temporary"
IMAGE_SOURCE_COMPLETED_TOOL = "completed_tool"


def _is_completed_tool_grant_candidate(event: dict, raw_path: str) -> bool:
    """B1/N6: producer allowlist, assistant role and exact image-tool match."""
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("role", "assistant") != "assistant":
        return False
    producer = event.get("agent_type")
    tool_name = payload.get("name")
    if (producer, tool_name) not in _PROVEN_IMAGE_TOOL_SUCCESS:
        return False
    # image_tool_path enforces the image suffix allowlist and the exact
    # provider-specific path argument.
    return image_tool_path(payload) == raw_path


def _open_regular_image_pinned(path: Path) -> Tuple[bytes, str]:
    """Open *path* with every directory component pinned by a descriptor.

    B3 (confirmed by ``controller-parent-swap-red.json``): checking for
    symlinks and then doing an ordinary ``open`` — even with leaf
    ``O_NOFOLLOW`` — leaves a TOCTOU window where a parent directory is
    replaced by a symlink to another location before the open. Path
    resolution would follow that new parent and serve a different inode.

    This walks the path with ``openat`` relative to the previous
    component's descriptor, using ``O_DIRECTORY | O_NOFOLLOW`` for every
    directory and ``O_NOFOLLOW`` for the final file. Once a component is
    opened its identity is fixed; a later swap of any ancestor cannot
    redirect the walk. The final descriptor is validated with ``fstat``
    (regular file, size cap) before reading. ``O_NONBLOCK`` on the file
    prevents a FIFO/device substitution from blocking before fstat rejects
    it. Raises :class:`AgentImageUnavailable` for every denial.
    """
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if not path.is_absolute() or ".." in path.parts:
        raise AgentImageUnavailable()
    parts = path.parts  # ("/", "a", "b", ...) for an absolute path
    parent_fd: Optional[int] = None
    file_fd: Optional[int] = None
    try:
        # Pin the filesystem root (or the platform's equivalent anchor).
        parent_fd = os.open(parts[0], directory_flags)
        for component in parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
        stat_result = os.fstat(file_fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise AgentImageUnavailable()
        data = os.read(file_fd, _AGENT_IMAGE_MAX_BYTES + 1)
    except AgentImageUnavailable:
        raise
    except OSError:
        raise AgentImageUnavailable() from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    if len(data) > _AGENT_IMAGE_MAX_BYTES:
        raise AgentImageUnavailable()
    mime = sniff_image_mime(data)
    if mime is None:
        raise AgentImageUnavailable()
    return data, mime


def referenced_completed_tool_image(session: ManagedSession, raw_path: str) -> Optional[Path]:
    """Resolve an exact out-of-cwd image authorized by a completed image tool.

    Authorization boundary (narrow, fail-closed; B1/B2): a producer-verified
    native image-view tool must have *started* and *completed successfully*
    for this exact path under the full identity
    ``(session, run_epoch, turn_id, sub_thread, call_id)``. A started-only or
    failed call, another tool, user text, generic shell output, a different
    run/turn/sub-thread (including a colliding call id), a duplicated start,
    or another session never grants access. Derived from the persisted
    canonical stream, so historical successful records work without
    migration; this function only resolves the path — the actual read goes
    through the no-follow opener in :func:`_read_granted_tool_image`.
    """
    if session.target != ExecutionTarget.LOCAL or not session.workspace_path:
        return None
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or len(raw_path) > 4096
        or any(ord(ch) < 32 for ch in raw_path)
    ):
        return None
    candidate = Path(raw_path)
    # Only explicit absolute paths need this grant: relative paths are
    # anchored at the session cwd and handled by normal root containment.
    # No ``~`` expansion and no traversal: the grant names the literal path.
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        # Cheap lexical/stat pre-check; the authoritative symlink/TOCTOU
        # boundary is the descriptor-pinned opener at read time (B3).
        if candidate.is_symlink() or not candidate.is_file():
            return None
    except OSError:
        return None

    # Full start identity -> occurrence count. Any duplicated start makes the
    # provenance ambiguous and denies the whole key (B2).
    starts: Dict[Tuple[Optional[int], Optional[str], Optional[str], str], int] = {}
    try:
        events = AgentStreamStore(session.workspace_id, session.id).iter_events()
        for stored_event in events:
            event = stored_event.model_dump(mode="json")
            if not isinstance(event, dict) or event.get("session_id") != session.id:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")
            call_id = payload.get("tool_call_id") or event.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            identity = (
                event.get("run_epoch"),
                event.get("turn_id"),
                payload.get("subagent_thread"),
                call_id,
            )
            if kind == "tool_call_started":
                if _is_completed_tool_grant_candidate(event, raw_path):
                    starts[identity] = starts.get(identity, 0) + 1
                continue
            if kind != "tool_call_completed":
                continue
            if payload.get("status") != _COMPLETED_TOOL_STATUS:
                continue
            # The completion must be the unique matching start in the same
            # run/turn/sub-thread; ordering is guaranteed by the event log.
            if starts.get(identity) == 1:
                return candidate
    except (OSError, UnicodeError):
        return None
    return None


def _is_grant_only_path(session: ManagedSession, raw_path: str) -> bool:
    """True when *raw_path* can only be authorized via the completed-tool grant.

    Deliberately lexical (no filesystem existence checks): this runs in the
    retained-LRU fallback after the source has disappeared, so it must still
    classify a deleted path. It only decides retention/revocation policy;
    actual read authorization always goes through the strict grant scan and
    the no-follow opener. cwd-contained paths and temp-root paths keep the
    existing retained-LRU semantics; everything else absolute is grant-only.
    """
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return False
        absolute = os.path.abspath(str(candidate))
        lexical_roots = {os.path.abspath(str(root)) for root in _agent_image_allowed_roots(session)}
        for root in lexical_roots:
            if absolute == root or absolute.startswith(root + os.sep):
                return False
        for temp_root in _TEMP_IMAGE_ROOTS:
            for alias in {
                os.path.abspath(str(temp_root)),
                os.path.abspath(str(temp_root.resolve())),
            }:
                if absolute == alias or absolute.startswith(alias + os.sep):
                    return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _read_granted_tool_image(session: ManagedSession, raw_path: str) -> Tuple[bytes, str]:
    granted = referenced_completed_tool_image(session, raw_path)
    if granted is None:
        raise AgentImageUnavailable()
    # Re-resolve at open time: the descriptor-pinned opener re-validates
    # every component and the inode atomically, so a swap after the scan
    # cannot redirect the grant (B3).
    return _open_regular_image_pinned(granted)


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


def _read_referenced_image(session: ManagedSession, raw_path: str) -> Tuple[bytes, str, str]:
    """Read with the existing cwd/temp/tool-grant order, tagging the source.

    Returns ``(data, mime, source_kind)`` where *source_kind* is one of
    :data:`IMAGE_SOURCE_CWD`, :data:`IMAGE_SOURCE_TEMPORARY` or
    :data:`IMAGE_SOURCE_COMPLETED_TOOL`; the tag drives revocation and cache
    policy without weakening any read validation.
    """
    try:
        data, mime = _read_agent_image(session, raw_path)
        return data, mime, IMAGE_SOURCE_CWD
    except AgentImageUnavailable:
        referenced = referenced_temporary_image(session, raw_path)
        if referenced is not None:
            data, mime = _read_agent_image(session, raw_path, referenced_image=referenced)
            return data, mime, IMAGE_SOURCE_TEMPORARY
        # Last resort: an exact path whose producer-verified image tool
        # completed in this session. Read through the no-follow opener so
        # symlink swaps cannot redirect the grant.
        data, mime = _read_granted_tool_image(session, raw_path)
        return data, mime, IMAGE_SOURCE_COMPLETED_TOOL


async def read_cached_agent_image(
    session: ManagedSession,
    raw_path: str,
    *,
    owner_is_current: Optional[OwnerIsCurrent] = None,
) -> Tuple[bytes, str]:
    data, mime, _source_kind = await read_cached_agent_image_with_policy(
        session, raw_path, owner_is_current=owner_is_current
    )
    return data, mime


async def read_cached_agent_image_with_policy(
    session: ManagedSession,
    raw_path: str,
    *,
    owner_is_current: Optional[OwnerIsCurrent] = None,
) -> Tuple[bytes, str, str]:
    """Like :func:`read_cached_agent_image`, plus the source-kind policy tag.

    The route layer uses the tag to avoid ``immutable`` caching on
    provenance-granted reads, and the retention path uses it for revocation.
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
) -> Tuple[bytes, str, str]:
    store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    _require_current_owner(owner_is_current)
    try:
        data, mime, source_kind = await asyncio.to_thread(_read_referenced_image, session, raw_path)
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
        # B4: a tool-grant-only source (outside cwd, not a temp grant) may
        # use the retained LRU only while its provenance still exists. If the
        # granting turn was truncated/cleared (or the store reset), the
        # retained copy is invalidated instead of resurrecting bytes whose
        # authority is gone. A still-valid grant with a deleted source keeps
        # the normal retained-original behavior. cwd/temp sources are
        # unaffected because their authority is the live session itself.
        if _is_grant_only_path(session, raw_path) and (
            referenced_completed_tool_image(session, raw_path) is None
        ):
            try:
                await _discard_cached_source(
                    store,
                    key,
                    owner_is_current=owner_is_current,
                )
            except (AttachmentOwnerUnavailable, OSError):
                pass
            raise AgentImageUnavailable()
        try:
            data, mime = await store.read_source(key, owner_is_current=owner_is_current)
            # Retained bytes of a grant-only (out-of-cwd, non-temp) source keep
            # the grant's no-store policy; labeling them cwd would let an
            # immutable header bypass replacement/revocation (N1).
            retained_kind = (
                IMAGE_SOURCE_COMPLETED_TOOL
                if _is_grant_only_path(session, raw_path)
                else IMAGE_SOURCE_CWD
            )
            return data, mime, retained_kind
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
    return data, mime, source_kind


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
        # Started image-view calls awaiting completion, keyed by
        # (run epoch, turn id, call id) so a colliding call id in another
        # run/turn stays separate. Entries are removed on completion or when
        # the turn ends; the map is bounded by the live turn count.
        self._pending_image_tools: Dict[Tuple[Optional[int], Optional[str], str], List[str]] = {}

    def paths(self, event: AgentStreamEvent) -> List[str]:
        turn = (str(event.run_epoch), str(event.turn_id))
        if event.type == "turn_completed":
            self._tails = {key: tail for key, tail in self._tails.items() if key[:2] != turn}
            self._pending_image_tools = {
                key: paths
                for key, paths in self._pending_image_tools.items()
                if (str(key[0]), str(key[1])) != turn
            }
        if event.type == "tool_call_started":
            paths = image_paths_from_event(event)
            payload = event.payload
            call_id = payload.get("tool_call_id") or event.call_id
            if paths and isinstance(call_id, str) and call_id:
                self._pending_image_tools[(event.run_epoch, event.turn_id, call_id)] = paths
            return paths
        if event.type == "tool_call_completed":
            # Retain the newly authorized out-of-cwd image only once the tool
            # has successfully completed in this same run/turn. A failed or
            # unattributable completion emits nothing, so no pre-completion
            # fetch can publish a grant and no failed output is retained.
            payload = event.payload
            if payload.get("status") != _COMPLETED_TOOL_STATUS:
                return []
            call_id = payload.get("tool_call_id") or event.call_id
            if not isinstance(call_id, str) or not call_id:
                return []
            pending_key = (event.run_epoch, event.turn_id, call_id)
            return list(self._pending_image_tools.pop(pending_key, ()))
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
