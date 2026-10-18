"""Store responsibilities for attachment previews."""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .garbage_collection import GarbageCollectionMixin


class AgentStreamAttachmentStore(GarbageCollectionMixin):
    """Store behavior on the existing cache owner."""

    @property
    def _session_dir(self) -> Path:
        return self._deps._session_dir_for(self.workspace_id, self.session_id)

    def _session_entries(self, index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            e
            for e in index
            if e.get("session_id") == self.session_id and e.get("workspace_id") == self.workspace_id
        ]

    def has_residual_state(self) -> bool:
        """Whether files or manifest rows still reserve this session identity."""
        if self._session_dir.exists():
            return True
        return bool(self._session_entries(self._read_index()))

    async def save(
        self,
        mime_type: str,
        preview_bytes: bytes,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        source_key: Optional[str] = None,
        owner_is_current: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Persist a bounded preview and return its opaque metadata.

        The caller supplies an original or bounded display image. The backend enforces the
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
        if source_key is not None and re.fullmatch(r"[0-9a-f]{64}", source_key) is None:
            raise ValueError("source_key must be a SHA-256 digest")
        source_digest = hashlib.sha256(preview_bytes).hexdigest() if source_key else None
        if len(preview_bytes) > self.max_preview_bytes:
            raise ValueError(
                f"preview exceeds per-file cap: {len(preview_bytes)} > {self.max_preview_bytes} bytes"
            )
        self._deps._validate_preview_mime(mime_type, preview_bytes)

        # Parse dimensions from the header; never trust the client-supplied
        # width/height. Reject malformed headers and out-of-bounds dimensions.
        parsed_width, parsed_height = self._deps._parse_image_dimensions(preview_bytes, mime_type)
        if not (
            1 <= parsed_width <= self._deps.MAX_PREVIEW_EDGE
            and 1 <= parsed_height <= self._deps.MAX_PREVIEW_EDGE
        ):
            raise ValueError(
                f"preview dimensions {parsed_width}x{parsed_height} outside "
                f"allowed range 1..{self._deps.MAX_PREVIEW_EDGE}"
            )
        if width is not None and width != parsed_width:
            raise ValueError(f"declared width {width} does not match parsed width {parsed_width}")
        if height is not None and height != parsed_height:
            raise ValueError(
                f"declared height {height} does not match parsed height {parsed_height}"
            )

        async with self._deps._get_global_lock():
            self._deps._require_current_owner(owner_is_current)
            index = self._read_index()
            if source_key is not None and self._deps._session_dir_chain_is_real(
                self.workspace_id, self.session_id
            ):
                for existing in self._session_entries(index):
                    if (
                        existing.get("source_key") != source_key
                        or existing.get("source_digest") != source_digest
                    ):
                        continue
                    cached = self._deps._resolve_attachment_path(
                        self.workspace_id, self.session_id, existing["id"]
                    )
                    if cached.is_symlink() or not cached.is_file():
                        continue
                    now = time.time()
                    if (
                        self.max_age_seconds is not None
                        and now - existing["created_at"] >= self.max_age_seconds
                    ):
                        # An expired source is a new capture, not a cache hit.
                        # Continue through normal publication/eviction below.
                        continue
                    if now - existing["last_used_at"] >= self._deps._LRU_TOUCH_INTERVAL_SECONDS:
                        existing["last_used_at"] = now
                        self._write_index(index)
                    return {
                        "id": existing["id"],
                        "mime_type": existing["mime_type"],
                        "bytes": existing["size"],
                        "width": existing["width"],
                        "height": existing["height"],
                    }
            attachment_id = uuid.uuid4().hex
            created_at = time.time()

            # Ensure the expected session directory chain is real (no symlinks
            # at the workspace or session hash component) BEFORE building the
            # target path. ``_ensure_session_dir`` unlinks any symlink it finds
            # and fails closed if unlink fails, so we never write through a
            # symlink into another session's directory.
            session_dir = self._deps._ensure_session_dir(self.workspace_id, self.session_id)
            target = session_dir / attachment_id
            # Containment check (defense in depth; components are hashed so
            # this can only fail if _attachments_root itself is misconfigured).
            root = self._deps._attachments_root()
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
                "last_used_at": created_at,
            }
            if source_key is not None:
                entry.update(source_key=source_key, source_digest=source_digest)

            # Publish the file and the manifest atomically: if the manifest
            # write fails (or anything else raises before the index is
            # persisted), unlink the preview file immediately so we do not
            # leave an orphan that can only be reclaimed at restart. The
            # caller has no id to roll back, so the store must self-clean.
            try:
                self._deps._write_bytes_0600(target, preview_bytes)
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
                changed = False
                if source_key is not None:
                    # A path URL denotes its latest retained contents. Remove
                    # superseded versions so evicting the current one cannot
                    # silently resurrect an older screenshot at that URL.
                    for old in list(self._session_entries(index)):
                        if old.get("source_key") == source_key and old["id"] != attachment_id:
                            self._delete_entry(index, old, persist=False)
                            changed = True
                changed = self._evict_aged(index) or changed
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

    async def discard_source(
        self,
        source_key: str,
        *,
        owner_is_current: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Forget superseded output when its replacement cannot be retained."""
        async with self._deps._get_global_lock():
            self._deps._require_current_owner(owner_is_current)
            index = self._read_index()
            entries = [
                entry
                for entry in self._session_entries(index)
                if entry.get("source_key") == source_key
            ]
            for entry in entries:
                self._delete_entry(index, entry, persist=False)
            if entries:
                self._write_index(index)

    async def read_source(
        self,
        source_key: str,
        *,
        owner_is_current: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bytes, str]:
        """Read the latest retained version of a source in this session.

        Works after the original file is gone. The regular read refreshes
        LRU recency and enforces the same ownership and symlink checks.
        """
        async with self._deps._get_global_lock():
            self._deps._require_current_owner(owner_is_current)
            index = self._read_index()
            entries = [
                entry
                for entry in self._session_entries(index)
                if entry.get("source_key") == source_key
            ]
            if not entries:
                raise KeyError(source_key)
            entry = max(entries, key=lambda entry: entry["created_at"])
            return self._read_entry(index, entry)

    async def read(self, attachment_id: str) -> Tuple[bytes, str]:
        """Return ``(preview_bytes, mime_type)`` for an existing attachment.

        Raises ``KeyError`` if the attachment is missing or evicted. The API
        layer maps this to a stable placeholder response.
        """
        self._validate_id(attachment_id)
        async with self._deps._get_global_lock():
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
            return self._read_entry(index, entry)

    def _read_entry(self, index: List[Dict[str, Any]], entry: Dict[str, Any]) -> Tuple[bytes, str]:
        attachment_id = entry["id"]
        # Refuse to follow a symlink placed at the workspace or session
        # hash component: it would redirect the read to another session's
        # directory. Treat as missing.
        if not self._deps._session_dir_chain_is_real(self.workspace_id, self.session_id):
            raise KeyError(attachment_id)
        # Build the path from hashed components; the raw ids from the
        # index are never interpolated into the filesystem path.
        try:
            path = self._deps._resolve_attachment_path(
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
        data = path.read_bytes()
        now = time.time()
        if now - entry["last_used_at"] >= self._deps._LRU_TOUCH_INTERVAL_SECONDS:
            entry["last_used_at"] = now
            try:
                self._write_index(index)
            except OSError:
                # Recency is advisory; a failed refresh must not fail the read.
                pass
        return data, entry["mime_type"]

    async def _delete_by_id(self, attachment_id: str) -> None:
        """Delete a single preview by id from the index and disk.

        Used by the tailer for cleanup when a turn fails before the
        ``turn_started`` event is durably published. The entry is looked up
        in the global index by id; its path is resolved from the hashed
        workspace/session components stored in the index entry.
        """
        async with self._deps._get_global_lock():
            index = self._read_index()
            entry = next((e for e in index if e.get("id") == attachment_id), None)
            if entry is None:
                return
            # If the workspace/session ancestor chain contains a symlink, do
            # not follow it to delete a file in another session. Just drop the
            # index entry; GC will clean up the symlink.
            if not self._deps._session_dir_chain_is_real(
                entry["workspace_id"], entry["session_id"]
            ):
                index[:] = [e for e in index if e.get("id") != attachment_id]
                self._write_index(index)
                return
            try:
                path = self._deps._resolve_attachment_path(
                    entry["workspace_id"], entry["session_id"], entry["id"]
                )
            except ValueError:
                path = None
            if path is not None:
                try:
                    if path.is_symlink():
                        os.unlink(path)
                    elif path.is_dir():
                        self._deps._safe_rmtree(path)
                    else:
                        path.unlink()
                except FileNotFoundError:
                    pass
            index[:] = [e for e in index if e.get("id") != attachment_id]
            self._write_index(index)

    async def clear(self, *, strict: bool = False) -> None:
        """Remove all previews for this session (called on session deletion).

        The session directory is derived from hashed workspace/session ids,
        so it is safe to remove wholesale. We use ``_safe_clear_session_dir``
        which ``lstat``s every ancestor component (root → workspace hash →
        session hash) and, if any is a symlink, unlinks that symlink itself
        and returns without recursing. Only when every ancestor is a real
        directory does it remove the session directory's contents. This
        prevents a symlink at the workspace or session hash component from
        causing deletion outside the preview root.

        Durable deletion uses ``strict=True``: invalid index data and filesystem
        failures propagate, and index ownership is retained until physical
        removal succeeds. Legacy callers keep the existing best-effort behavior.
        """
        async with self._deps._get_global_lock():
            index = self._read_index(strict=True) if strict else self._read_index()
            index = [
                e
                for e in index
                if not (
                    e.get("session_id") == self.session_id
                    and e.get("workspace_id") == self.workspace_id
                )
            ]
            if strict:
                # Keep manifest ownership when physical deletion fails. The
                # durable retirement ledger must never ACK surviving previews.
                self._deps._safe_clear_session_dir(self.workspace_id, self.session_id, strict=True)
            self._write_index(index)
            if strict:
                fd = os.open(self._deps._index_path(), os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._deps._sync_existing_directory(
                    self._deps._index_path().parent, trusted_root=self._deps._state_root()
                )
            # Remove the entire hashed session directory. We never iterate
            # over manifest-derived attachment ids here.
            if not strict:
                self._deps._safe_clear_session_dir(self.workspace_id, self.session_id)

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
