"""Non-blocking TTL cache for read-only Codebase MR state observations."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from claude_hub.services.demand_patrol.commands import CommandError, CommandRunner, run_command
from claude_hub.services.demand_patrol.daily_digest import _mr_snapshot
from claude_hub.services.demand_patrol.models import MRStateSnapshot

logger = logging.getLogger(__name__)

CODEBASE_MR_URL_RE = re.compile(
    r"https://code\.byted\.org/(?:[A-Za-z0-9_.-]+/)+merge_requests/(\d+)\b"
)
_DOC_EXAMPLE_MR_PREFIX = "https://code.byted.org/byteapi/bytedcli/merge_requests/"

_CACHE_TTL_SECONDS = 180.0
_ERROR_BACKOFF_SECONDS = 120.0
_MAX_CACHE_ENTRIES = 256
_MAX_CONCURRENT_PROBES = 3
_PROBE_TIMEOUT_SECONDS = 10.0


def extract_codebase_mr_urls(
    texts: Sequence[str | None],
    *,
    exclude_doc_examples: bool = True,
) -> list[str]:
    """Extract deduplicated canonical Codebase MR URLs in appearance order."""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in CODEBASE_MR_URL_RE.finditer(text):
            url = match.group(0)
            if exclude_doc_examples and url.startswith(_DOC_EXAMPLE_MR_PREFIX):
                continue
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def mr_short_label(url: str) -> str:
    """Format a canonical MR URL into a compact badge label like ``'!1483'``."""
    match = CODEBASE_MR_URL_RE.search(url or "")
    if match:
        return f"!{match.group(1)}"
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return f"!{tail}" if tail.isdigit() else "MR"


def format_mr_badge_label(urls: Sequence[str]) -> str:
    """Format one or more open MR URLs as ``'MR !1483'`` or ``'MR !1483 +1'``."""
    if not urls:
        return "Review"
    first = mr_short_label(urls[0])
    extra = len(urls) - 1
    return f"MR {first} +{extra}" if extra > 0 else f"MR {first}"


@dataclass
class _CachedMREntry:
    snapshot: MRStateSnapshot | None
    next_refresh_monotonic: float
    last_updated_monotonic: float


class MRStatusCache:
    """Bounded stale-while-refresh cache for Codebase MR statuses.

    ``observe`` never awaits subprocess I/O on HTTP request paths: it returns
    in-memory snapshots immediately and schedules deduplicated background
    refreshes when live polling is enabled.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        error_backoff_seconds: float = _ERROR_BACKOFF_SECONDS,
        allow_in_tests: bool = False,
    ) -> None:
        self._runner = runner
        self._ttl_seconds = ttl_seconds
        self._error_backoff_seconds = error_backoff_seconds
        self._allow_in_tests = allow_in_tests
        self._entries: dict[str, _CachedMREntry] = {}
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._stopped = False

    def _polling_enabled(self) -> bool:
        if self._stopped:
            return False
        if os.environ.get("CLAUDE_HUB_DISABLE_MR_CACHE") == "1":
            return False
        if "pytest" in sys.modules and not self._allow_in_tests:
            return False
        return True

    def seed(self, snapshot: MRStateSnapshot, *, ttl_seconds: float | None = None) -> None:
        """Seed or update a cached MR snapshot directly (for tests or shared callers)."""
        now = time.monotonic()
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        self._entries[snapshot.url] = _CachedMREntry(
            snapshot=snapshot,
            next_refresh_monotonic=now + ttl,
            last_updated_monotonic=now,
        )
        self._trim()

    def clear(self) -> None:
        self._entries.clear()

    def _trim(self) -> None:
        while len(self._entries) > _MAX_CACHE_ENTRIES:
            oldest_url = min(
                self._entries,
                key=lambda k: self._entries[k].last_updated_monotonic,
            )
            self._entries.pop(oldest_url, None)

    def observe(self, urls: Sequence[str]) -> dict[str, MRStateSnapshot]:
        """Return currently known snapshots for ``urls`` and schedule background refreshes."""
        if not urls:
            return {}
        now = time.monotonic()
        result: dict[str, MRStateSnapshot] = {}
        to_refresh: list[str] = []
        for url in dict.fromkeys(urls):
            if not url:
                continue
            entry = self._entries.get(url)
            if entry is not None and entry.snapshot is not None:
                result[url] = entry.snapshot
            if entry is None or now >= entry.next_refresh_monotonic:
                to_refresh.append(url)

        if to_refresh and self._polling_enabled():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                for url in to_refresh:
                    if url in self._in_flight or len(self._in_flight) >= 32:
                        continue
                    self._in_flight.add(url)
                    task = loop.create_task(
                        self._refresh_one(url),
                        name=f"mr-status-refresh:{mr_short_label(url)}",
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        return result

    async def _refresh_one(self, url: str) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
        try:
            async with self._semaphore:
                payload = await self._runner(
                    ["bytedcli", "--json", "codebase", "mr", "get", url],
                    timeout=_PROBE_TIMEOUT_SECONDS,
                )
                snapshot = _mr_snapshot(payload, url)
            now = time.monotonic()
            self._entries[url] = _CachedMREntry(
                snapshot=snapshot,
                next_refresh_monotonic=now + self._ttl_seconds,
                last_updated_monotonic=now,
            )
            self._trim()
        except asyncio.CancelledError:
            raise
        except (CommandError, OSError, TimeoutError, ValueError) as exc:
            logger.debug("MR status refresh unavailable for %s (%s)", url, type(exc).__name__)
            now = time.monotonic()
            previous = self._entries.get(url)
            self._entries[url] = _CachedMREntry(
                snapshot=previous.snapshot if previous else None,
                next_refresh_monotonic=now + self._error_backoff_seconds,
                last_updated_monotonic=now,
            )
            self._trim()
        finally:
            self._in_flight.discard(url)

    async def wait_pending(self) -> None:
        """Await currently scheduled background refreshes (useful in deterministic tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        self._stopped = True
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._in_flight.clear()
        self._semaphore = None


mr_status_cache = MRStatusCache()
