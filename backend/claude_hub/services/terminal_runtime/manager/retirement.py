"""Manager retirement responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from functools import partial
from typing import AsyncIterator, List, Optional, Tuple

from ....models import TerminalTab
from ..records import TabCleanupPendingError, _PendingTabCleanup, _TabStateCommitError
from .host import ManagerHost

logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerRetirementMixin(ManagerHost):
    async def delete_tab(self, tab_id: str) -> bool:
        """Retire the tab and durably reserve its artifacts until cleanup succeeds."""
        from ...agent_stream.tailer import fence_session_stream, validate_session_stream_cleanup

        self._require_valid_state()
        expected = self.processes.get(tab_id)
        expected_cleanup = self._pending_stream_cleanup.get(tab_id)
        if expected is None and expected_cleanup is None:
            return False
        expected_created_at = expected.created_at if expected is not None else None
        lock = self._cleanup_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            pending = self._pending_stream_cleanup.get(tab_id)
            current = self.processes.get(tab_id)
            if pending is not None:
                if (
                    current is not None
                    or (expected_cleanup is not None and pending != expected_cleanup)
                    or (
                        expected_created_at is not None
                        and pending.created_at != expected_created_at.isoformat()
                    )
                ):
                    raise TabCleanupPendingError(
                        "cleanup identity conflicts with a live or newer owner"
                    )
                await self._finish_tab_cleanup(pending)
                return True
            if expected_cleanup is not None:
                if current is not None:
                    raise TabCleanupPendingError("cleanup identity conflicts with a live owner")
                return True  # Another caller completed this exact retired obligation.
            if (
                current is not expected
                or current is None
                or current.created_at != expected_created_at
            ):
                return False

            # Check captured identity BEFORE any fence: a stale waiter must not
            # fence a replacement. Creation cannot reuse this live/reserved id.
            provider_fenced = await fence_session_stream(f"terminal-tab-{tab_id}")
            async with self._locked_current_tab(tab_id) as process:
                if (
                    process is None
                    or process is not expected
                    or process.created_at != expected_created_at
                ):
                    return False
                await process.stop(kill_tmux=True)
                validate_session_stream_cleanup(
                    "terminal-tabs",
                    f"terminal-tab-{tab_id}",
                    acknowledged_call_ids=set(),
                    provider_fenced=provider_fenced,
                )
                record = _PendingTabCleanup(
                    tab_id, process.created_at.isoformat(), uuid.uuid4().hex
                )
                old_order = list(self._tab_order)
                self.processes.pop(tab_id)
                self._pending_stream_cleanup[tab_id] = record
                self._tab_order = [item for item in self._tab_order if item != tab_id]
                try:
                    self._persist_state()
                except Exception as exc:
                    if not isinstance(exc, _TabStateCommitError) or not exc.replaced:
                        self.processes[tab_id] = process
                        self._pending_stream_cleanup.pop(tab_id)
                        self._tab_order = old_order
                        raise TabCleanupPendingError(
                            "tab retirement was not committed; retry DELETE"
                        ) from exc
                    # The replaced snapshot is authoritative in memory too. No
                    # ordinary writer may resurrect the tab/drop its obligation.
                    raise TabCleanupPendingError(
                        "tab retired; cleanup commit unconfirmed; retry DELETE"
                    ) from exc
                self._save_order()
            await self._finish_tab_cleanup(record)
            return True

    async def _finish_tab_cleanup(self, record: _PendingTabCleanup) -> None:
        """Run under the per-id cleanup lock; keep the reservation through ACK."""
        from ...agent_stream.tailer import (
            discard_retired_session_artifacts,
            session_stream_has_owner,
        )

        def require_owner() -> None:
            self._require_valid_state()
            if (
                self._pending_stream_cleanup.get(record.tab_id) != record
                or record.tab_id in self.processes
                or session_stream_has_owner(f"terminal-tab-{record.tab_id}")
            ):
                raise TabCleanupPendingError(
                    "cleanup identity conflicts with a live or newer owner"
                )

        require_owner()
        try:
            if not self._state_confirmed:
                self._persist_state()
            await discard_retired_session_artifacts(
                "terminal-tabs", f"terminal-tab-{record.tab_id}"
            )
            require_owner()
            self._pending_stream_cleanup.pop(record.tab_id)
            try:
                self._persist_state()
            except Exception:
                # Even an ACK that reached replace but failed directory fsync
                # keeps its obligation in memory. Ordinary saves preserve it.
                self._pending_stream_cleanup[record.tab_id] = record
                self._state_confirmed = False
                raise
        except TabCleanupPendingError:
            raise
        except Exception as exc:
            raise TabCleanupPendingError(
                "tab retired; artifact cleanup pending; retry DELETE"
            ) from exc

    def schedule_pending_stream_cleanup(self, *, limit: int = 8) -> List[asyncio.Task[bool]]:
        """Start bounded finite jobs without blocking provider startup or the monitor."""
        self._require_valid_state()
        capacity = min(limit, 8 - len(self._cleanup_tasks))
        records = list(self._pending_stream_cleanup.values())
        if self._cleanup_scheduling_stopped or not records or capacity <= 0:
            return []
        offset = self._cleanup_retry_offset % len(records)
        started: List[asyncio.Task[bool]] = []
        for index, record in enumerate(records[offset:] + records[:offset]):
            self._cleanup_retry_offset = (offset + index + 1) % len(records)
            key = (record.tab_id, record.token)
            lock = self._cleanup_locks.setdefault(record.tab_id, asyncio.Lock())
            if key in self._cleanup_tasks or lock.locked():
                continue
            task = asyncio.create_task(
                self._run_tab_cleanup(record), name=f"retired-tab-cleanup-{record.tab_id}"
            )
            self._cleanup_tasks[key] = task
            task.add_done_callback(partial(self._tab_cleanup_done, key))
            started.append(task)
            if len(started) >= capacity:
                break
        return started

    async def _run_tab_cleanup(self, record: _PendingTabCleanup) -> bool:
        async with self._cleanup_locks.setdefault(record.tab_id, asyncio.Lock()):
            if self._pending_stream_cleanup.get(record.tab_id) != record:
                return False
            await self._finish_tab_cleanup(record)
            return True

    def _tab_cleanup_done(self, key: Tuple[str, str], task: asyncio.Task[bool]) -> None:
        if self._cleanup_tasks.get(key) is task:
            self._cleanup_tasks.pop(key)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "Retired tab cleanup remains pending tab_id=%s", key[0], exc_info=error
                )

    @staticmethod
    async def _await_cleanup_tasks(tasks: List[asyncio.Task[bool]]) -> List[bool | BaseException]:
        joined = asyncio.gather(*tasks, return_exceptions=True)
        cancellation: Optional[asyncio.CancelledError] = None
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        results = joined.result()
        if cancellation is not None:
            raise cancellation
        return results

    @asynccontextmanager
    async def cleanup_task_lifetime(self) -> AsyncIterator[None]:
        """Keep every submitted file operation inside backend runtime ownership."""
        self._cleanup_scheduling_stopped = False
        try:
            yield
        finally:
            self._cleanup_scheduling_stopped = True
            tasks = list(self._cleanup_tasks.values())
            for task in tasks:
                task.cancel()
            # Cancellation cannot stop a disk thread. Join its owning job before
            # BackendInstanceLock is released, including startup failure paths.
            await self._await_cleanup_tasks(tasks)

    async def retry_pending_stream_cleanup(self, *, limit: int = 8) -> int:
        """Drain a scheduled batch explicitly; background callers only schedule."""
        tasks = self.schedule_pending_stream_cleanup(limit=limit)
        results = await self._await_cleanup_tasks(tasks)
        return sum(result is True for result in results)

    async def archive_tab(self, tab_id: str) -> Optional[TerminalTab]:
        """Release a tab's runtime while retaining its record and structured history."""
        async with self._locked_current_tab(tab_id) as process:
            if process is None:
                return None
            from ...restart_continuation import cancel_pending_continuation

            cancel_pending_continuation(tab_id, None, veto_capture=True)
            if not process.archived:
                process.archived = True
                process.archived_at = self._runtime.datetime.now()
            logger.warning(
                "Archiving tab %s (%s); releasing runtime, keeping history", tab_id, process.name
            )
            await process.stop(kill_tmux=True)
            # Serialize restore with the complete stop, including the native transport.
            try:
                from ...agent_stream.tailer import stop_session_stream

                await stop_session_stream(f"terminal-tab-{tab_id}")
            except Exception:
                logger.exception("Failed to stop stream tailer for archived tab %s", tab_id)
            process.is_active = False
            self._save_state()
            return process.to_schema()

    async def unarchive_tab(self, tab_id: str) -> Optional[TerminalTab]:
        """Restore an archived record, then best-effort start its runtime."""
        async with self._locked_current_tab(tab_id) as process:
            if process is None:
                return None
            if process.archived:
                self._check_live_tab_capacity()
                process.archived = False
                process.archived_at = None
            logger.info("Unarchiving tab %s (%s)", tab_id, process.name)
            self._save_state()

        # ensure_tab_running acquires this same lock; do not recursively hold it.
        try:
            await self.ensure_tab_running(tab_id)
        except Exception:
            logger.exception(
                "Best-effort restart after unarchive failed for tab %s; cold recovery will retry on open",
                tab_id,
            )
        # Deletion may have won while the best-effort restart was waiting.
        return self.get_tab(tab_id)

    def list_archived_tabs(self) -> list[TerminalTab]:
        """List archived tabs, most recently archived first."""
        archived = [p for p in self.processes.values() if p.archived]
        archived.sort(
            key=lambda p: p.archived_at or self._runtime.datetime.min,
            reverse=True,
        )
        return [p.to_schema() for p in archived]
