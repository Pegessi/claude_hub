"""Best-effort MASO archive synchronization after local ownership commits.

Managers retain only a derived identity snapshot to detect lifecycle edges. The
live records remain authoritative for sharing and for superseding queued work.
Rotation is explicitly retired by the transport after its adoption journal has
finished, not by observing an intermediate identity write.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote
from weakref import WeakMethod, WeakSet

import httpx
from httpx import AsyncClient

from ..models import AgentType, SessionKind

logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT = 2.0


@dataclass(frozen=True)
class MasoSessionIdentity:
    base_url: str
    session_id: str


def _pinned_base_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        url = httpx.URL(value.rstrip("/"))
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            return None
    except (ValueError, httpx.InvalidURL):
        return None
    return str(url).rstrip("/")


def confirmed_identity(base_url: object, session_id: object) -> MasoSessionIdentity | None:
    """Validate a pinned endpoint; callers must establish provider confirmation."""
    base = _pinned_base_url(base_url)
    if base is None or not isinstance(session_id, str) or not session_id:
        return None
    return MasoSessionIdentity(base, session_id)


class _SessionRecord(Protocol):
    @property
    def agent_type(self) -> AgentType: ...
    @property
    def session_kind(self) -> SessionKind: ...
    @property
    def agent_session_id(self) -> str | None: ...
    @property
    def agent_session_id_verified(self) -> bool: ...
    @property
    def env(self) -> Mapping[str, str]: ...
    @property
    def archived(self) -> bool: ...


@dataclass(frozen=True)
class MasoSessionReference:
    base_url: str
    identity: MasoSessionIdentity | None
    archived: bool


def session_reference(record: _SessionRecord) -> MasoSessionReference | None:
    if record.agent_type != AgentType.MASO or record.session_kind != SessionKind.CHAT:
        return None
    # Retain an unverified local owner too: a confirmed first allocation may
    # exist only in its adoption journal after identity persistence failed.
    base = _pinned_base_url(record.env.get("MASO_BASE_URL", ""))
    if base is None:
        return None
    identity = (
        MasoSessionIdentity(base, record.agent_session_id)
        if record.agent_session_id_verified and record.agent_session_id
        else None
    )
    return MasoSessionReference(base, identity, record.archived)


References = dict[tuple[str, str], MasoSessionReference]


class MasoArchiveSource:
    """One manager's post-commit edges; no network traffic on load or save."""

    def __init__(self, reader: Callable[[], References]) -> None:
        self._reader = WeakMethod(reader)
        self._committed = reader()
        self._coordinator = maso_session_archives
        self._coordinator.sources.add(self)

    def current(self) -> References:
        reader = self._reader()
        return reader() if reader is not None else {}

    def committed(self, *, scope: str | None = None) -> None:
        current = self.current()
        previous = self._committed
        keys = set(previous) | set(current)
        for key in keys:
            if scope is not None and key[0] != scope:
                continue
            old, new = previous.get(key), current.get(key)
            if old == new:
                continue
            if new is None:
                if old is not None:
                    self._schedule_archive_state(key, old, archived=True, permanent=True)
                previous.pop(key, None)
            else:
                # A changed identity is NOT retirement: adoption may still
                # fail while writing its seed/journal. Only restore the new ID.
                self._schedule_archive_state(key, new, archived=new.archived)
                previous[key] = new

    def _journal_identities(
        self, key: tuple[str, str], reference: MasoSessionReference
    ) -> tuple[MasoSessionIdentity | None, MasoSessionIdentity | None, bool]:
        from .agent_stream.store import AgentStreamStore

        path = AgentStreamStore(*key).path.with_suffix(".maso-delivery.json")
        try:
            journal = json.loads(path.read_text())
        except FileNotFoundError:
            return None, None, False
        if not isinstance(journal, dict):
            raise ValueError("Invalid MASO adoption journal")
        candidate_id = journal.get("adopted_session_id")
        candidate = confirmed_identity(journal.get("base_url", ""), candidate_id)
        if candidate is None or candidate.base_url != reference.base_url:
            return None, None, False
        old = confirmed_identity(reference.base_url, journal.get("session_id"))
        return (
            (old if old != candidate else None),
            candidate,
            journal.get("session_id_verified") is True,
        )

    def _schedule_archive_state(
        self,
        key: tuple[str, str],
        reference: MasoSessionReference,
        *,
        archived: bool,
        permanent: bool = False,
    ) -> None:
        identities = {reference.identity} if reference.identity is not None else set()
        try:
            old, candidate, old_verified = self._journal_identities(key, reference)
            if candidate is not None:
                identities.add(candidate)
            # A resumable archive retains the old-ID journal veto. Permanent
            # removal discards that evidence, so capture its proven retirement
            # now; shared owners still veto the eventual background request.
            if permanent and old_verified and old is not None:
                identities.add(old)
        except (OSError, ValueError):
            logger.warning("MASO archive skipped journal target: unreadable adoption evidence")
        for identity in identities:
            self._coordinator.schedule(identity, archived=archived)

    def protects(self, identity: MasoSessionIdentity) -> bool:
        current = self.current()
        # In-memory flags can lead their durable commit (archive awaits stop).
        # Both committed and current live references can veto, never authorize.
        if any(
            ref.identity == identity and not ref.archived
            for ref in [*self._committed.values(), *current.values()]
        ):
            return True
        for key in self._committed.keys() | current.keys():
            refs = [ref for ref in (self._committed.get(key), current.get(key)) if ref is not None]
            ref = current.get(key) or self._committed[key]
            if ref.base_url != identity.base_url:
                continue
            try:
                old, candidate, _old_verified = self._journal_identities(key, ref)
            except (OSError, ValueError):
                logger.warning("MASO archive skipped: unreadable local adoption evidence")
                return True
            # A journal can outlive its process/restart and identity write.
            # Its old ID is held until finalization; a confirmed candidate is
            # held while its owner is live, even if the row is still unverified.
            if old == identity or (
                candidate == identity and any(not item.archived for item in refs)
            ):
                return True
        return False


class MasoSessionArchives:
    """Serialize each remote identity without delaying local lifecycle owners."""

    def __init__(self) -> None:
        self.sources: WeakSet[MasoArchiveSource] = WeakSet()
        self._jobs: dict[MasoSessionIdentity, asyncio.Task[None]] = {}
        self._pending: dict[MasoSessionIdentity, bool] = {}
        self._stopped = False

    def schedule(self, identity: MasoSessionIdentity, *, archived: bool) -> None:
        if self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("MASO archive sync skipped: no running event loop")
            return
        self._pending[identity] = archived
        if identity not in self._jobs:
            self._jobs[identity] = loop.create_task(self._sync(identity), name="maso-archive-sync")

    def _can_archive(self, identity: MasoSessionIdentity) -> bool:
        return not any(source.protects(identity) for source in self.sources)

    async def _sync(self, identity: MasoSessionIdentity) -> None:
        try:
            while identity in self._pending:
                requested = self._pending.pop(identity)
                archived = requested and self._can_archive(identity)
                try:
                    async with asyncio.timeout(_REQUEST_TIMEOUT):
                        async with AsyncClient(
                            base_url=identity.base_url, trust_env=False, timeout=_REQUEST_TIMEOUT
                        ) as client:
                            response = await client.post(
                                f"/api/sessions/{quote(identity.session_id, safe='')}/archive",
                                json={"archived": archived},
                            )
                            response.raise_for_status()
                except (httpx.HTTPError, TimeoutError) as exc:
                    # Do not include URLs, credentials or remote response bodies.
                    status = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    )
                    logger.warning(
                        "MASO archive sync failed (%s, status=%s)", type(exc).__name__, status
                    )
                # A restore/delete can commit during HTTP I/O. Serialize the
                # correction after the old request instead of racing true/false.
                if archived and not self._can_archive(identity):
                    self._pending[identity] = False
        except Exception:
            # This detached best-effort boundary must never affect local state.
            logger.exception("MASO archive synchronization failed")
        finally:
            self._jobs.pop(identity, None)
            self._pending.pop(identity, None)

    async def drain(self) -> None:
        """Join current jobs for tests/shutdown, never for a Tab/Task transition."""
        while self._jobs:
            await asyncio.gather(*tuple(self._jobs.values()), return_exceptions=True)

    @asynccontextmanager
    async def lifetime(self) -> AsyncIterator[None]:
        self._stopped = False
        try:
            yield
        finally:
            self._stopped = True
            for job in self._jobs.values():
                job.cancel()
            await self.drain()


maso_session_archives = MasoSessionArchives()


def retire_adopted_session(journal: dict[str, Any]) -> None:
    """Run only after identity, seed and adoption-journal removal succeed."""
    old_id = journal.get("session_id")
    if (
        journal.get("session_id_verified") is not True
        or not isinstance(old_id, str)
        or old_id == journal.get("adopted_session_id")
    ):
        return
    identity = confirmed_identity(journal["base_url"], old_id)
    if identity is not None:
        maso_session_archives.schedule(identity, archived=True)


def retire_unowned_allocation(base_url: str, session_id: str) -> None:
    """A provider-confirmed allocation with no committed adoption journal."""
    identity = confirmed_identity(base_url, session_id)
    if identity is not None:
        maso_session_archives.schedule(identity, archived=True)
