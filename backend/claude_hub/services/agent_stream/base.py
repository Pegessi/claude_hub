"""Base adapter contract and shared helpers for the structured observation plane.

Each provider adapter normalizes one agent CLI's transcript into a stream of
:class:`~claude_hub.models.AgentStreamEvent` records. The raw terminal (Layer C)
is always the fail-closed fallback when no adapter can source a transcript.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    StreamCapabilities,
)

# Negative-cache window for failed source discovery. Keeps the fail-closed
# capabilities path cheap when an agent hasn't written its transcript yet.
_DISCOVERY_NEGATIVE_TTL_S = 5.0


@dataclass
class NormalizeContext:
    """Per-line normalization context carrying session identity and a clock."""

    session_id: str
    tab_id: str
    agent_type: AgentType
    run_epoch: Optional[int]
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def event(
        self,
        type: AgentStreamEventType,
        payload: Dict[str, Any],
        *,
        call_id: Optional[str] = None,
    ) -> AgentStreamEvent:
        """Build an event pre-populated with this context's identity fields."""
        return AgentStreamEvent(
            stream_sequence=0,  # assigned by the store on append
            session_id=self.session_id,
            tab_id=self.tab_id,
            agent_type=self.agent_type,
            type=type,
            run_epoch=self.run_epoch,
            call_id=call_id,
            payload=payload,
            created_at=self.now,
        )


class AgentStreamAdapter:
    """Abstract base for provider transcript adapters.

    Subclasses set ``adapter_id``, ``schema_version``, ``supports_approval_ui``,
    and ``supports_tool_timeline``; implement :meth:`discover_source` and
    :meth:`normalize_line`; and may override :meth:`supports_snapshot` /
    :meth:`read_snapshot` for snapshot-style sources (Cursor ACP).
    """

    adapter_id: str = "base"
    schema_version: int = 0
    supports_approval_ui: bool = False
    supports_tool_timeline: bool = False

    def capabilities(self, session: ManagedSession) -> StreamCapabilities:
        """Return the capabilities this adapter advertises for ``session``.

        Defaults to ``structured=True`` when a source can be discovered;
        subclasses may override (e.g. Codex waits for its rollout to exist).
        """
        source = discover_source_cached(self, session)
        return StreamCapabilities(
            structured=source is not None,
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            sources=[str(source)] if source else [],
            supports_approval_ui=self.supports_approval_ui,
            supports_tool_timeline=self.supports_tool_timeline,
        )

    # ── source discovery ─────────────────────────────────────────────────────

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        """Locate the transcript file for ``session`` or return ``None``."""
        raise NotImplementedError

    # ── normalization ────────────────────────────────────────────────────────

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        """Normalize one raw transcript line into zero or more events.

        Must be pure and defensive: skip anything unparseable, never raise.
        """
        raise NotImplementedError

    # ── snapshot support (optional) ──────────────────────────────────────────

    def supports_snapshot(self) -> bool:
        """Whether this adapter reads a full snapshot instead of tailing lines."""
        return False

    def read_snapshot(self, session: ManagedSession) -> List[AgentStreamEvent]:
        """Return the full event list for snapshot-style sources."""
        return []


# ── shared discovery helpers ─────────────────────────────────────────────────


@dataclass
class _DiscoveryCacheEntry:
    path: Optional[Path]
    expires_at: float


_discovery_cache: Dict[Tuple[str, str], _DiscoveryCacheEntry] = {}


def discover_source_cached(adapter: AgentStreamAdapter, session: ManagedSession) -> Optional[Path]:
    """Cache :meth:`discover_source` results (including ``None``) briefly."""
    key = (adapter.adapter_id, session.id)
    entry = _discovery_cache.get(key)
    now = time.monotonic()
    if entry is not None and entry.expires_at > now:
        return entry.path
    try:
        path = adapter.discover_source(session)
    except Exception:
        path = None
    _discovery_cache[key] = _DiscoveryCacheEntry(
        path=path, expires_at=now + _DISCOVERY_NEGATIVE_TTL_S
    )
    return path


def invalidate_source(session_id: str) -> None:
    """Drop all cached discovery results for ``session_id`` (e.g. on rotation)."""
    keys = [k for k in _discovery_cache if k[1] == session_id]
    for k in keys:
        _discovery_cache.pop(k, None)


def resolve_process_hint(session: ManagedSession) -> Tuple[str, Optional[str]]:
    """Return ``(cwd, agent_session_id)`` from the live ttyd process, if any."""
    try:
        from ..ttyd_manager import ttyd_manager

        proc = ttyd_manager.processes.get(session.tab_id)
    except Exception:
        proc = None
    cwd = session.workspace_path or ""
    agent_session_id: Optional[str] = None
    if proc is not None:
        proc_cwd = getattr(proc, "cwd", None)
        if proc_cwd:
            cwd = proc_cwd
        proc_session_id = getattr(proc, "agent_session_id", None)
        if proc_session_id:
            agent_session_id = proc_session_id
    if not agent_session_id:
        agent_session_id = session.agent_session_id
    return cwd, agent_session_id


def resolve_cwd(session: ManagedSession) -> str:
    """Prefer the live process cwd; fall back to the session's workspace path."""
    cwd, _ = resolve_process_hint(session)
    return cwd or session.workspace_path or ""
