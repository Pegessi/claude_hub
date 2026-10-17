"""Garbage collection responsibilities for attachment previews."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .store_state import AttachmentState


class GarbageCollectionMixin(AttachmentState):
    """Garbage collection behavior on the existing cache owner."""

    async def gc(self) -> None:
        """Global garbage collection.

        Deletes orphan files (present on disk but absent from the index) and
        stale index entries (present in the index but absent from disk, or
        with unsafe path components). Enforces both per-session and global
        quotas by evicting least-recently-used first. Also enforces the optional age TTL.

        A corrupt index entry can never escape the preview root: workspace
        and session ids are reduced to SHA-256 hex digests before being used
        as path components, and every path is resolved under the dedicated
        preview root before any read or delete. Symlinks found under the
        preview root are unlinked (never followed); directory names that are
        not exactly 64 lowercase hex characters are removed.
        """
        async with self._deps._get_global_lock():
            index = self._read_index()
            root = self._deps._attachments_root()

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
                    path = self._deps._resolve_attachment_path(
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
                if not self._deps._session_dir_chain_is_real(
                    entry["workspace_id"], entry["session_id"]
                ):
                    continue
                if not path.is_symlink() and path.is_file():
                    valid.append(entry)

            # Delete orphan files: files on disk not referenced by any valid
            # index entry. Directory components are SHA-256 hex digests, so we
            # compare (hash(ws), hash(sess), att_id) tuples.
            valid_keys = {
                (
                    self._deps._hash_component(e["workspace_id"]),
                    self._deps._hash_component(e["session_id"]),
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
            if not self._deps._is_hash_component(ws_entry.name):
                # Not a valid SHA-256 digest directory — remove it entirely.
                self._deps._safe_rmtree(Path(ws_entry.path))
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
                if not self._deps._is_hash_component(sess_entry.name):
                    self._deps._safe_rmtree(Path(sess_entry.path))
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
                        self._deps._safe_rmtree(Path(f_entry.path))
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

    def _evict_session(self, index: List[Dict[str, Any]]) -> bool:
        """Evict least-recently-used first within this session until its quota holds.

        Returns ``True`` if at least one entry was removed, ``False``
        otherwise. Does **not** persist the index; the caller writes the
        manifest once after all eviction passes so an under-quota save
        incurs only the single publication write.
        """
        session_entries = self._session_entries(index)
        session_bytes = sum(e["size"] for e in session_entries)
        session_entries.sort(key=self._deps._lru_key)

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
            entries.sort(key=self._deps._lru_key)
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
        """Evict least-recently-used first globally until the global quota holds.

        Returns ``True`` if at least one entry was removed, ``False``
        otherwise. Does not persist the index.
        """
        global_bytes = sum(e["size"] for e in index)
        index.sort(key=self._deps._lru_key)

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
        if not self._deps._session_dir_chain_is_real(ws, sess):
            if not already_removed:
                index[:] = [e for e in index if e.get("id") != entry["id"]]
            if persist:
                self._write_index(index)
            return
        try:
            path = self._deps._resolve_attachment_path(ws, sess, att_id)
        except ValueError:
            # Invalid attachment id — nothing to delete on disk, just drop
            # from the index.
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
        if not already_removed:
            index[:] = [e for e in index if e.get("id") != entry["id"]]
        if persist:
            self._write_index(index)
