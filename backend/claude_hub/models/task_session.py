"""Task worker/reviewer session launch configuration."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .schemas import AgentType, ExecutionTarget, SessionKind


class ExecutorCapabilities(BaseModel):
    """Public capability snapshot for managed Task session executors."""

    available: bool
    supports_spawn: bool = False
    supports_send: bool = False
    supports_followup: bool = False
    supports_interrupt: bool = False
    durable_status: bool = False
    supported_agent_types: List[AgentType] = Field(default_factory=list)
    supported_session_kinds: List[SessionKind] = Field(default_factory=list)
    chat_supported_agent_types: List[AgentType] = Field(default_factory=list)
    model_configurable_agent_types: List[AgentType] = Field(default_factory=list)
    unavailable_reason: Optional[str] = None


class ManagedExecutorConfig(BaseModel):
    """Launch configuration for a managed Task worker/reviewer session.

    ``agent_type`` selects the Hub CLI integration (Claude Code, Codex, or
    Cursor). ``model`` is translated to the launch mechanism that the
    selected CLI supports; arbitrary command strings are not accepted.
    """

    agent_type: AgentType = AgentType.CLAUDE
    session_kind: SessionKind = SessionKind.TERMINAL
    model: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    solo_mode: bool = True
    target: Optional[ExecutionTarget] = None
    cwd: Optional[str] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
