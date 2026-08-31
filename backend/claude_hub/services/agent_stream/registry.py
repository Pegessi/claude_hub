"""Adapter registry and capability negotiation.

Fail-closed contract:

- ``get_adapter(agent_type)`` returns the adapter class for a provider type, or
  ``None`` if no adapter exists.
- ``get_adapter_for_session(session)`` additionally enforces per-session
  transport gating (e.g. Cursor only returns an adapter when its transport is
  ``acp`` or ``terminal_transcript``; otherwise ``None`` → structured=False).
- ``supports_structured(agent_type)`` is a coarse type-level check.
"""

from __future__ import annotations

from typing import Dict, Optional, Type

from ...models import AgentType, ManagedSession
from .base import AgentStreamAdapter
from .claude_jsonl import ClaudeJsonlAdapter
from .codex_jsonl import CodexJsonlAdapter
from .cursor_cli_transcript import CursorCliTranscriptAdapter

_ADAPTERS: Dict[AgentType, Type[AgentStreamAdapter]] = {
    AgentType.CLAUDE: ClaudeJsonlAdapter,
    AgentType.CODEX: CodexJsonlAdapter,
    AgentType.CURSOR: CursorCliTranscriptAdapter,
}


def get_adapter(agent_type: AgentType) -> Optional[Type[AgentStreamAdapter]]:
    """Return the adapter class for ``agent_type`` or ``None``."""
    return _ADAPTERS.get(agent_type)


def supports_structured(agent_type: AgentType) -> bool:
    """Whether an adapter exists for ``agent_type`` at all."""
    return agent_type in _ADAPTERS


def get_adapter_for_session(
    session: ManagedSession,
) -> Optional[AgentStreamAdapter]:
    """Return a configured adapter for ``session`` or ``None`` (fail-closed).

    For Cursor this returns ``None`` unless ``cursor_transport`` is one of the
    supported transports (``acp``, ``terminal_transcript``). Since the default
    transport is ``terminal``, Cursor fails closed for this wave.
    """
    adapter_cls = get_adapter(session.agent_type)
    if adapter_cls is None:
        return None
    if session.agent_type == AgentType.CURSOR:
        transport = getattr(session, "cursor_transport", "terminal")
        if transport not in ("acp", "terminal_transcript"):
            return None
    return adapter_cls()
