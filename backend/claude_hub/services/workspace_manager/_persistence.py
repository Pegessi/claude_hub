"""State persistence, durable-state loading, quarantine and backup."""

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from ..workspace_snapshot import render_workspace_snapshot
from ._base import _WorkspaceManagerBase
from ._constants import *  # noqa: F401,F403

# Rotating single backup of every durable JSON store. A truncated last write
# falls back to this complete previous payload.
_STATE_BACKUP_SUFFIX = ".bak"
# Persistent fail-closed marker describing quarantine event(s) for one store.
_QUARANTINE_MARKER_SUFFIX = ".quarantined"
# Corrupt primaries are moved, never overwritten: ``<file>.corrupt-<UTC ts>``.
_QUARANTINE_SUFFIX = ".corrupt-"


@dataclass(frozen=True)
class _DurableLoadResult:
    """Outcome of loading one durable JSON store."""

    data: Any | None
    # "primary": parsed primary; "backup": recovered from the rotating backup;
    # "empty": missing primary or quarantined without a good backup.
    source: str


class _PersistenceMixin(_WorkspaceManagerBase):
    def _task_dump_for_state(self, task: WorkspaceTask) -> dict[str, Any]:
        """Serialize a task for durable state without null optional metadata keys."""

        payload = task.model_dump(mode="json")
        if task.reporter_key_hash is not None:
            payload["reporter_key_hash"] = task.reporter_key_hash
        if task.execution_call_fingerprints:
            payload["execution_call_fingerprints"] = dict(task.execution_call_fingerprints)
        for name in ("creation_request_key", "creation_actor_key", "creation_fingerprint"):
            value = getattr(task, name)
            if value is not None:
                payload[name] = value
        if payload.get("agent_tag") is None:
            payload.pop("agent_tag", None)
        return payload

    def _fsync_directory(self, path: Path) -> None:
        """Best-effort directory fsync so rename/create metadata survives a crash."""

        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        """Atomically write text to ``path`` via a temp file + os.replace.

        This ensures readers never see a partially-written state file even
        if the process crashes mid-write.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            self._fsync_directory(path.parent)
        except Exception:
            # Clean up the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _atomic_save_state_text(self, path: Path, text: str) -> None:
        """Persist a durable JSON store and maintain its rotating backup.

        The previous complete primary is copied to ``<path>.bak`` (temp file
        + replace, so an interrupted copy never corrupts the existing backup)
        before the new payload replaces the primary. A torn primary write can
        therefore always fall back to the last complete state.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup_path = self._backup_path_for(path)
            fd, tmp_backup = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=f".{backup_path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    with open(path, "rb") as primary:
                        shutil.copyfileobj(primary, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_backup, backup_path)
            except OSError:
                # Unreadable primary or copy failure: keep the prior backup
                # untouched instead of replacing it with a partial copy.
                logger.exception(
                    "Could not rotate state backup before save; keeping previous backup: %s",
                    backup_path,
                )
                try:
                    os.unlink(tmp_backup)
                except OSError:
                    pass
        self._atomic_write_text(path, text)

    def _workspace_state_payload(self, workspace_id: str) -> dict[str, Any]:
        """Serialize one workspace's durable mutable state."""

        payload = {
            "tasks": [
                self._task_dump_for_state(item)
                for item in self.tasks.values()
                if item.workspace_id == workspace_id
            ],
            "sessions": [
                item.model_dump(mode="json")
                for item in self.sessions.values()
                if item.workspace_id == workspace_id
            ],
            "reports": [
                item.model_dump(mode="json")
                for item in self.reports.values()
                if item.workspace_id == workspace_id
            ],
        }
        payload.update(self.task_mailbox.to_dict(workspace_id))
        return payload

    def _save_workspace_state(self, workspace_id: str) -> None:
        """Write one workspace without rewriting the index or other workspaces.

        The state.json replacement is authoritative; its Markdown snapshot
        remains derived output. Callers own mutation locks and rollback policy.
        An exception alone does not identify whether replacement occurred.

        The Workspace row itself lives only in the index, which this entry does
        not write. A transaction that also edits ``self.workspaces[...]`` needs
        the full-state entry, even though the rollback snapshot restores it.
        """
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        payload = self._workspace_state_payload(workspace_id)
        state_text = json.dumps(payload, indent=2)
        self._atomic_save_state_text(self._workspace_state_file(workspace_id), state_text)
        self._refresh_snapshot_best_effort(workspace_id, state_text=state_text)

    def _refresh_snapshot_best_effort(
        self, workspace_id: str, *, state_text: str | None = None
    ) -> None:
        try:
            self._write_snapshot(workspace_id, state_text=state_text)
        except Exception:
            logger.exception(
                "Committed state.json is authoritative; failed to refresh derived snapshot "
                "workspace_id=%s",
                workspace_id,
            )

    def _save_state(self) -> None:
        """Write the index and all workspaces, without cross-workspace atomicity."""
        self._runtime_state_root.mkdir(parents=True, exist_ok=True)
        index_payload = {
            "workspaces": [self._workspace_index_item(item) for item in self.workspaces.values()]
        }
        self._atomic_save_state_text(INDEX_FILE, json.dumps(index_payload, indent=2))
        for workspace in self.workspaces.values():
            workspace_dir = self._workspace_dir(workspace.id)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            self._save_workspace_state(workspace.id)

    def _write_snapshot(self, workspace_id: str, *, state_text: str | None = None) -> None:
        """Render a committed payload; direct/cold-start calls read the authoritative file."""
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            return
        if state_text is None:
            state_text = self._workspace_state_file(workspace_id).read_text(encoding="utf-8")
        snapshot = render_workspace_snapshot(
            workspace.model_dump(mode="json"), state_text, self._runtime_now()
        )
        self._atomic_write_text(self.snapshot_path(workspace_id), snapshot)

    # ------------------------------------------------------------------
    # Durable-state load: quarantine + backup recovery
    # ------------------------------------------------------------------

    @staticmethod
    def _backup_path_for(path: Path) -> Path:
        return path.with_name(path.name + _STATE_BACKUP_SUFFIX)

    @staticmethod
    def _quarantine_marker_path(path: Path) -> Path:
        return path.with_name(path.name + _QUARANTINE_MARKER_SUFFIX)

    @staticmethod
    def _quarantine_timestamp(now: datetime | None = None) -> str:
        moment = now or datetime.now(timezone.utc)
        return moment.strftime("%Y%m%dT%H%M%SZ")

    def _unique_quarantine_path(self, path: Path, timestamp: str) -> Path:
        """Return a non-existing quarantine name with a counter suffix."""

        candidate = path.with_name(path.name + _QUARANTINE_SUFFIX + timestamp)
        counter = 1
        while candidate.exists():
            candidate = path.with_name(path.name + _QUARANTINE_SUFFIX + timestamp + f"-{counter}")
            counter += 1
        return candidate

    def _move_aside_for_quarantine(self, path: Path, timestamp: str) -> tuple[Path | None, bool]:
        """Move a corrupt file/directory aside; never overwrite existing evidence."""

        destination = self._unique_quarantine_path(path, timestamp)
        try:
            os.rename(path, destination)
            return destination, True
        except FileNotFoundError:
            return None, False
        except OSError:
            logger.exception(
                "Could not move corrupt state file aside for quarantine: %s -> %s",
                path,
                destination,
            )
            return destination, False

    def _append_quarantine_marker(self, path: Path, event: dict[str, Any]) -> Path:
        """Persist (append) a quarantine event next to the affected store."""

        marker = self._quarantine_marker_path(path)
        events: list[dict[str, Any]] = []
        if marker.exists():
            try:
                existing = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("events"), list):
                    events = [item for item in existing["events"] if isinstance(item, dict)]
                else:
                    raise ValueError("quarantine marker is not an events document")
            except (OSError, ValueError) as exc:
                # The marker itself is damaged: preserve it as evidence and
                # start a fresh marker instead of overwriting it.
                stale_marker, _ = self._move_aside_for_quarantine(
                    marker, self._quarantine_timestamp()
                )
                logger.error(
                    "Existing quarantine marker %s was unreadable (%s); moved to %s",
                    marker,
                    exc,
                    stale_marker,
                )
        events.append(event)
        self._atomic_write_text(
            marker,
            json.dumps({"store_path": str(path), "events": events}, indent=2, ensure_ascii=False),
        )
        return marker

    def _try_load_backup(
        self,
        path: Path,
        timestamp: str,
        validator: Callable[[Any], Any] | None,
    ) -> tuple[Any | None, str | None]:
        """Validate and restore ``<path>.bak``; quarantine the backup if bad."""

        backup_path = self._backup_path_for(path)
        if not backup_path.exists():
            return None, None
        try:
            backup_text = backup_path.read_text(encoding="utf-8")
            backup_data: Any = json.loads(backup_text)
            if validator is not None:
                backup_data = validator(backup_data)
        except (OSError, ValueError, TypeError) as exc:
            bad_backup, moved = self._move_aside_for_quarantine(backup_path, timestamp)
            logger.error(
                "Backup state file %s is also unusable (%s); moved aside to %s",
                backup_path,
                exc,
                bad_backup if moved else "place (could not move)",
            )
            return None, f"backup unusable: {exc}"
        # Restore the primary from the last complete payload. The backup is
        # kept in place as additional evidence.
        self._atomic_write_text(path, backup_text)
        logger.warning("Restored primary state %s from backup %s", path, backup_path)
        return backup_data, None

    def _quarantine_and_recover(
        self,
        path: Path,
        *,
        label: str,
        reason: str,
        original_bytes: bytes | None,
        timestamp: str,
        validator: Callable[[Any], Any] | None,
    ) -> Any | None:
        """Move the corrupt primary aside, record a marker, fall back to backup.

        Returns the backup payload (restored as the primary) or ``None`` when
        the manager must start this store from empty.
        """

        quarantine_path, moved = self._move_aside_for_quarantine(path, timestamp)
        digest = hashlib.sha256(original_bytes or b"").hexdigest()

        backup_data, backup_error = self._try_load_backup(path, timestamp, validator)
        recovered = backup_data is not None

        event: dict[str, Any] = {
            "label": label,
            "reason": reason,
            "detected_at": timestamp,
            "original_sha256": digest if original_bytes is not None else None,
            "quarantine_file": quarantine_path.name if quarantine_path is not None else None,
            "quarantine_moved": moved,
            "recovered_from_backup": recovered,
            "backup_file": self._backup_path_for(path).name if recovered else None,
            "backup_error": backup_error,
        }
        marker = self._append_quarantine_marker(path, event)
        self._state_recovery_warnings.append(
            f"State store {label!r} at {path} was quarantined ({reason}); " f"see {marker}"
        )
        logger.error(
            "Quarantined corrupt durable state store %s (%s): %s. Original moved to %s. "
            "Recovered from backup: %s. Marker: %s. The Hub started with %s. Inspect the "
            "quarantined payload and reconcile state before removing the marker; subsequent "
            "saves preserve the quarantined file but the manager will not merge it back.",
            label,
            path,
            reason,
            quarantine_path if moved else "(could not move; left in place)",
            "yes: " + str(self._backup_path_for(path)) if recovered else "no",
            marker,
            "the last complete backup" if recovered else "EMPTY state",
        )
        return backup_data

    def _load_durable_json(
        self,
        path: Path,
        *,
        label: str,
        validator: Callable[[Any], Any] | None = None,
    ) -> _DurableLoadResult:
        """Load a durable JSON store with fail-closed corruption handling.

        Missing file is the normal empty case. On JSON decode error,
        structural validation failure or OS error the primary is quarantined,
        the rotating backup is restored when valid, and the store otherwise
        starts empty with a persistent marker and an ERROR log.

        ``validator`` receives the decoded payload and may return a normalized
        payload; raising ValueError/TypeError marks the store structurally
        corrupt. It runs against both the primary and (if needed) the backup.
        """

        if not path.exists():
            backup_path = self._backup_path_for(path)
            if backup_path.exists():
                # Primary vanished while a rotating backup remains (external
                # deletion or a lost replace): recover instead of starting
                # empty, which would otherwise leave a stale backup that a
                # later corruption event could "restore" incorrectly.
                timestamp = self._quarantine_timestamp()
                data, backup_error = self._try_load_backup(path, timestamp, validator)
                marker = self._append_quarantine_marker(
                    path,
                    {
                        "label": label,
                        "reason": (
                            "primary state file missing; recovered from backup"
                            if data is not None
                            else "primary state file missing and backup unusable"
                        ),
                        "detected_at": timestamp,
                        "original_sha256": None,
                        "quarantine_file": None,
                        "quarantine_moved": False,
                        "recovered_from_backup": data is not None,
                        "backup_file": backup_path.name if data is not None else None,
                        "backup_error": backup_error,
                    },
                )
                if data is not None:
                    self._state_recovery_warnings.append(
                        f"State store {label!r} at {path} was missing on startup; "
                        f"recovered from {backup_path}; see {marker}"
                    )
                    logger.warning(
                        "Primary state store %s was missing; recovered from backup %s; marker %s",
                        path,
                        backup_path,
                        marker,
                    )
                    return _DurableLoadResult(data=data, source="backup")
                self._state_recovery_warnings.append(
                    f"State store {label!r} at {path} was missing on startup and its backup "
                    f"was unusable; started empty; see {marker}"
                )
                logger.error(
                    "Primary state store %s was missing and its backup %s was unusable (%s); "
                    "started empty; marker %s",
                    path,
                    backup_path,
                    backup_error,
                    marker,
                )
            return _DurableLoadResult(data=None, source="empty")
        timestamp = self._quarantine_timestamp()
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            data = self._quarantine_and_recover(
                path,
                label=label,
                reason=f"unreadable state file: {exc}",
                original_bytes=None,
                timestamp=timestamp,
                validator=validator,
            )
            return _DurableLoadResult(data=data, source="backup" if data is not None else "empty")
        try:
            payload: Any = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            data = self._quarantine_and_recover(
                path,
                label=label,
                reason=f"invalid JSON: {exc}",
                original_bytes=raw_bytes,
                timestamp=timestamp,
                validator=validator,
            )
            return _DurableLoadResult(data=data, source="backup" if data is not None else "empty")
        if validator is not None:
            try:
                payload = validator(payload)
            except (ValueError, TypeError) as exc:
                data = self._quarantine_and_recover(
                    path,
                    label=label,
                    reason=f"structural validation failed: {exc}",
                    original_bytes=raw_bytes,
                    timestamp=timestamp,
                    validator=validator,
                )
                return _DurableLoadResult(
                    data=data, source="backup" if data is not None else "empty"
                )
        return _DurableLoadResult(data=payload, source="primary")

    def _quarantine_marker_paths(self) -> list[Path]:
        """All known quarantine markers under the current runtime state root."""

        root = self._runtime_state_root
        markers = [
            self._quarantine_marker_path(INDEX_FILE),
            self._quarantine_marker_path(self._runtime_scheduled_tasks_file),
        ]
        if root.exists():
            markers.extend(sorted(root.glob("*/state.json" + _QUARANTINE_MARKER_SUFFIX)))
        return [marker for marker in markers if marker.exists()]

    @staticmethod
    def _summarize_quarantine_marker(marker: Path) -> str | None:
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"Unreadable quarantine marker present: {marker}"
        events = doc.get("events") if isinstance(doc, dict) else None
        if not events:
            return f"Quarantine marker present: {marker}"
        latest = events[-1]
        return (
            f"Quarantine marker present: {marker} "
            f"({len(events)} event(s); latest reason: {latest.get('reason')!r}; "
            f"recovered_from_backup={latest.get('recovered_from_backup')})"
        )

    def state_recovery_warnings(self) -> list[str]:
        """Persistent startup-health warnings from state quarantine markers."""

        warnings = list(self._state_recovery_warnings)
        # The in-memory entry above and the marker summary below describe the
        # same marker; dedup by marker path so /health lists each event once.
        mentioned_markers = {
            tail.strip()
            for entry in self._state_recovery_warnings
            for tail in [entry.rpartition("see ")[2]]
            if tail
        }
        for marker in self._quarantine_marker_paths():
            if str(marker) in mentioned_markers:
                continue
            summary = self._summarize_quarantine_marker(marker)
            if summary is not None and summary not in warnings:
                warnings.append(summary)
        return warnings
