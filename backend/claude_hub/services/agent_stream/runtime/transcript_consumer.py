"""Transcript consumer responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ....models import AgentStreamEventType, ManagedSession
from ..base import NormalizeContext
from ..redaction import redact_event
from .session_state import SessionState

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class TranscriptConsumerMixin(SessionState):
    """Transcript consumer behavior on the sole session owner."""

    async def _poll_once(self) -> None:
        session = self._session_getter()
        if session is None:
            return

        # Chat sessions require a native transport. If it could not be
        # created, fail closed — never use the transcript as a real-time
        # source for an agent session.
        if self._native_error is not None:
            self._hard_failed = True
            self._last_error = self._native_error
            self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
            return

        # Native transport is driven by the push consumer in ``_run_native``,
        # not by this poll loop. There is no transcript to backfill from, so
        # ``poll_once`` is a no-op for native sessions.
        if self._native_transport is not None:
            return

        path = self._deps.discover_source_cached(self.adapter, session)
        if path is None:
            if self._discovery_deadline is None:
                self._discovery_deadline = time.monotonic() + self._deps.DISCOVERY_GRACE_S
            elif time.monotonic() > self._discovery_deadline:
                self._hard_failed = True
                self._last_error = "structured source not found within discovery grace period"
                self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                self._deps.invalidate_source(self.session_id)
            return
        self._discovery_deadline = None
        self._hard_failed = False
        self._deps._HARD_FAILED_SESSION_IDS.discard(self.session_id)
        self._last_error = None
        self._current_source = path
        await self._tail_file(path, session)

    async def _poll_native(self, session: ManagedSession) -> None:
        """Drain one batch of records from the native transport stdout."""
        transport = self._native_transport
        assert transport is not None
        if not transport._started:
            try:
                await transport.start()
            except Exception as exc:
                self._hard_failed = True
                self._last_error = str(exc)
                self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                return
        self._hard_failed = False
        self._deps._HARD_FAILED_SESSION_IDS.discard(self.session_id)
        self._last_error = None

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        # Drain whatever is currently available without blocking forever.
        drained = 0
        while drained < 200:
            try:
                record = await asyncio.wait_for(transport.read_line(), timeout=0.05)
            except asyncio.TimeoutError:
                break
            if record is None:
                # EOF: the provider process exited.
                self._last_error = "native transport process exited"
                break
            drained += 1
            # Let the transport capture any provider conversation id from the
            # raw record (e.g. Claude/Cursor message_start.message.id).
            transport.maybe_capture_conversation_id(record)
            try:
                events = self.adapter.normalize_line(record, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on native record for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for event in events:
                if event.type == AgentStreamEventType.TURN_STARTED:
                    self._run_epoch += 1
                event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    await self._publish(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
        # Drain any coalesced text deltas before marking the session live.
        await self._coalescer.flush_async()
        self._is_live = True

    async def _tail_file(self, path: Path, session: ManagedSession) -> None:
        self._current_source = path
        if not self._loaded_cursor:
            self._load_cursor(path)
            self._loaded_cursor = True
        if self.adapter.supports_snapshot(session):
            await self._tail_snapshot(path, session)
            return
        try:
            lines, new_offset, inode, rotated = await asyncio.to_thread(self._read_new_lines, path)
        except OSError:
            self._deps.invalidate_source(self.session_id)
            return
        if rotated:
            logger.warning(
                "agent_stream tailer: source %s rotated for session %s; rebuilding",
                path,
                self.session_id,
            )
            self._reset_for_rotation(path)
            return
        if not lines:
            await self._coalescer.flush_async()
            self._is_live = True
            return

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        for raw in lines:
            try:
                events = self.adapter.normalize_line(raw, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on a line for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for event in events:
                if event.type == AgentStreamEventType.TURN_STARTED:
                    self._run_epoch += 1
                # Normalizers receive a context for the source row, but a turn
                # can span many rows. Stamp every emitted event with the
                # current epoch so assistant output remains grouped with its
                # preceding user turn.
                event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    await self._publish(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
        # Drain any coalesced text deltas from this backfill batch BEFORE
        # flipping to live. If we flipped first, the trailing timer could
        # fire and fan out historical deltas as if they were live, causing
        # duplicate delivery (subscribers already replay history from the
        # store).
        await self._coalescer.flush_async()
        self._offset = new_offset
        self._inode = inode
        self._is_live = True
        self._save_cursor()

    async def _tail_snapshot(self, path: Path, session: ManagedSession) -> None:
        """Reconcile one bounded authoritative whole-file snapshot.

        The event store and SSE protocol are append-only. A changed snapshot is
        therefore safe to publish only when the previously persisted source
        identities are an exact prefix of the new snapshot. In that case we
        append and fan out only the suffix. A compaction that rewrites history
        cannot be expressed safely to an already-connected client, so it
        fails closed and the UI returns to the raw terminal rather than showing
        duplicated or silently replaced turns.
        """
        try:
            snapshot = await asyncio.to_thread(self.adapter.read_snapshot, path, session)
        except Exception:
            logger.exception(
                "agent_stream snapshot read failed for session %s; failing closed",
                self.session_id,
            )
            self._deps.invalidate_source(self.session_id)
            return

        if snapshot is None:
            return

        if not snapshot.records:
            # A temporary empty file can be observed while a producer rewrites
            # its checkpoint. Keep the last good cursor and retry next poll.
            if self._snapshot_source_ids:
                return
            self._snapshot_digest = snapshot.digest
            await self._coalescer.flush_async()
            self._is_live = True
            self._save_cursor()
            return

        if snapshot.digest == self._snapshot_digest:
            await self._coalescer.flush_async()
            self._is_live = True
            return

        source_ids = [record.source_id for record in snapshot.records]
        prior_count = len(self._snapshot_source_ids)
        if source_ids[:prior_count] == self._snapshot_source_ids:
            pass
        elif (
            prior_count
            and len(self._snapshot_source_kinds) == prior_count
            and self._snapshot_source_kinds[-1] == "turn_ended"
            and source_ids[: prior_count - 1] == self._snapshot_source_ids[: prior_count - 1]
        ):
            # Cursor writes a terminal ``turn_ended`` row, then replaces only
            # that tail marker with the next user row. The completed event was
            # already published and remains valid; treat this exact observed
            # marker replacement as the next append point. Any other rewrite
            # stays fail-closed below.
            prior_count -= 1
        else:
            self._hard_failed = True
            self._last_error = (
                "Cursor transcript rewrote previously published history; "
                "structured view safely returned to the raw terminal"
            )
            self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
            self._deps.invalidate_source(self.session_id)
            return

        new_records = snapshot.records[prior_count:]
        if not new_records:
            self._snapshot_digest = snapshot.digest
            await self._coalescer.flush_async()
            self._is_live = True
            self._save_cursor()
            return

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        run_epoch = self._run_epoch
        for record in new_records:
            try:
                record_events = self.adapter.normalize_line(record.raw, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on snapshot record for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for ev in record_events:
                if ev.type == AgentStreamEventType.TURN_STARTED:
                    run_epoch += 1
                ev.run_epoch = run_epoch
                ev = redact_event(ev)
                try:
                    await self._publish(ev)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for snapshot session %s; failing closed",
                        self.session_id,
                    )
                    self._hard_failed = True
                    self._last_error = (
                        "structured event persistence failed; returned to raw terminal"
                    )
                    self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                    return

        # Drain any coalesced text deltas from this snapshot backfill BEFORE
        # flipping to live, so historical content is not fanned out as live.
        await self._coalescer.flush_async()
        self._run_epoch = run_epoch
        self._snapshot_digest = snapshot.digest
        self._snapshot_source_ids = source_ids
        self._snapshot_source_kinds = [record.source_kind for record in snapshot.records]
        self._is_live = True
        self._save_cursor()

    def _read_new_lines(self, path: Path) -> Tuple[List[Dict[str, Any]], int, int, bool]:
        st = os.stat(path)
        inode = st.st_ino
        size = st.st_size
        if self._inode is not None and inode != self._inode:
            return [], 0, inode, True
        if size < self._offset:
            return [], 0, inode, True
        if size == self._offset:
            return [], self._offset, inode, False
        with open(path, "rb") as f:
            f.seek(self._offset)
            data = f.read(size - self._offset)
        complete = data
        new_offset = self._offset + len(data)
        if data and not data.endswith(b"\n"):
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                complete = b""
                new_offset = self._offset
            else:
                complete = data[: last_nl + 1]
                new_offset = self._offset + len(complete)
        lines: List[Dict[str, Any]] = []
        for chunk in complete.split(b"\n"):
            if not chunk.strip():
                continue
            try:
                record = json.loads(chunk.decode("utf-8", errors="ignore"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                lines.append(record)
        return lines, new_offset, inode, False

    def _load_cursor(self, path: Path) -> None:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("path") != str(path):
            return
        inode = data.get("inode")
        try:
            st = os.stat(path)
        except OSError:
            return
        if inode is not None and inode != st.st_ino:
            return
        try:
            self._offset = int(data.get("offset", 0))
            self._run_epoch = int(data.get("run_epoch", 0))
        except (TypeError, ValueError):
            return
        self._inode = st.st_ino
        snapshot_ids = data.get("snapshot_source_ids")
        snapshot_kinds = data.get("snapshot_source_kinds")
        snapshot_digest = data.get("snapshot_digest")
        if isinstance(snapshot_ids, list) and all(isinstance(item, str) for item in snapshot_ids):
            self._snapshot_source_ids = list(snapshot_ids)
        if isinstance(snapshot_digest, str):
            self._snapshot_digest = snapshot_digest
        if isinstance(snapshot_kinds, list) and all(
            isinstance(item, str) for item in snapshot_kinds
        ):
            self._snapshot_source_kinds = list(snapshot_kinds)
        if self._offset > 0:
            self._is_live = True

    def _save_cursor(self) -> None:
        payload = {
            "path": str(self._current_source) if self._current_source else "",
            "offset": self._offset,
            "inode": self._inode,
            "run_epoch": self._run_epoch,
            "snapshot_source_ids": self._snapshot_source_ids,
            "snapshot_source_kinds": self._snapshot_source_kinds,
            "snapshot_digest": self._snapshot_digest,
        }
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_path.with_suffix(".cursor.json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cursor_path)
        except OSError:
            logger.exception(
                "agent_stream tailer: failed to persist cursor for session %s",
                self.session_id,
            )

    def _reset_for_rotation(self, path: Path) -> None:
        self._offset = 0
        self._inode = None
        self._run_epoch = 0
        self._snapshot_source_ids = []
        self._snapshot_source_kinds = []
        self._snapshot_digest = None
        self._is_live = False
        self._deps.invalidate_source(self.session_id)
