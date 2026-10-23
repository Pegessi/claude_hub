"""Terminal tab and remote-profile schemas."""

import uuid
from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from claude_hub.models.schema_domains.runtime import (
    CHAT_REMOTE_UNSUPPORTED,
    AgentRuntimeStatus,
    AgentType,
    ChatMode,
    ExecutionTarget,
    RemoteTransport,
    SessionKind,
    WorkspaceSessionRole,
)


class TerminalTabBase(BaseModel):
    """Base schema for TerminalTab."""

    name: str = Field(..., description="Name of the terminal tab")
    shell: Optional[str] = Field(None, description="Shell to use (default: $SHELL)")
    cwd: Optional[str] = Field(None, description="Working directory to start the terminal in")
    solo_mode: bool = Field(False, description="Whether to start in agent solo mode")
    agent_type: AgentType = Field(AgentType.CLAUDE, description="Type of agent to run")
    session_kind: SessionKind = Field(
        SessionKind.TERMINAL,
        description="Fixed UI surface: structured Chat conversation or raw Terminal",
    )
    chat_mode: ChatMode = Field(
        ChatMode.DEFAULT,
        description="Provider execution mode used by the next structured Chat turn",
    )
    target: ExecutionTarget = Field(ExecutionTarget.LOCAL, description="Where to run the tab")
    remote_profile_id: Optional[str] = Field(None, description="Remote profile ID for remote tabs")
    remote_cwd: Optional[str] = Field(None, description="Remote working directory")
    remote_reconnect: bool = Field(True, description="Reconnect SSH automatically for remote tabs")
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the launched terminal or agent",
    )
    cursor_transport: str = Field(
        "terminal",
        description="Cursor transport mode: 'terminal' (raw), 'terminal_transcript', or 'acp'",
    )
    cursor_data_dir: Optional[str] = Field(
        None,
        description="Pinned Cursor data directory for same-pane transcript provenance",
    )
    cursor_cli_version: Optional[str] = Field(
        None,
        description="Pinned Cursor CLI version for same-pane transcript provenance",
    )
    cursor_transcript_path: Optional[str] = Field(
        None,
        description="Pinned absolute transcript path for same-pane transcript provenance",
    )
    cursor_transcript_schema: Optional[str] = Field(
        None,
        description="Pinned transcript schema identifier for same-pane transcript provenance",
    )

    @model_validator(mode="after")
    def validate_session_kind(self) -> "TerminalTabBase":
        if self.agent_type == AgentType.MASO and self.session_kind != SessionKind.CHAT:
            raise ValueError("MASO is an HTTP Chat provider; select a Chat session")
        if self.session_kind == SessionKind.CHAT and self.agent_type == AgentType.TERMINAL:
            raise ValueError(
                "Chat sessions require a Claude, Codex, TraeX, Cursor, or MASO provider"
            )
        if self.session_kind != SessionKind.CHAT and self.chat_mode != ChatMode.DEFAULT:
            raise ValueError("Chat modes are only available for Chat sessions")
        return self


class TerminalTabCreate(TerminalTabBase):
    """Schema for creating a TerminalTab.

    ``agent_session_id`` lets a new tab pin to an existing agent conversation
    at creation time — currently used to resume a specific Codex session via
    ``codex resume <id>``. When omitted, Codex tabs start fresh and discover
    their session id from the rollout file shortly after launch (existing
    behavior).
    """

    agent_session_id: Optional[str] = Field(
        None,
        description=(
            "Existing agent conversation id to resume. For Codex tabs this is "
            "a session UUID from ~/.codex/sessions; the tab launches with "
            "codex resume <id>. Omit for a fresh session."
        ),
    )

    @model_validator(mode="after")
    def _reject_chat_remote(self) -> "TerminalTabCreate":
        if self.agent_type == AgentType.MASO and self.chat_mode != ChatMode.DEFAULT:
            raise ValueError("MASO Chat only supports default mode")
        # Native Chat owns a local ProviderSession. The remote SSH launcher is
        # Terminal-only; combining the two would persist target=remote while
        # still running the model on the Hub host.
        if self.session_kind == SessionKind.CHAT and self.target == ExecutionTarget.REMOTE:
            raise ValueError(CHAT_REMOTE_UNSUPPORTED)
        return self


class SwitchEnvRequest(BaseModel):
    """Payload for switching the environment / model of a live Claude or Codex tab."""

    env: Dict[str, str] = Field(
        ...,
        description="New environment variables to apply (fully replaces existing env)",
    )
    solo_mode: Optional[bool] = Field(
        None,
        description="If set, also toggles solo mode; if omitted, preserves current setting",
    )


class TerminalTabUpdate(BaseModel):
    """Schema for updating a TerminalTab."""

    name: Optional[str] = None
    shell: Optional[str] = None
    cwd: Optional[str] = None
    solo_mode: Optional[bool] = None
    agent_type: Optional[AgentType] = None
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    env: Optional[Dict[str, str]] = None


class TerminalTab(TerminalTabBase):
    """Schema for returning a TerminalTab."""

    id: str
    port: int
    created_at: datetime
    is_active: bool
    workspace_id: Optional[str] = Field(None, description="Workspace that created this tab")
    workspace_name: Optional[str] = Field(None, description="Display name of the owning workspace")
    workspace_role: Optional[WorkspaceSessionRole] = Field(
        None,
        description="Managed workspace role for this tab",
    )
    managed_session_id: Optional[str] = Field(
        None,
        description="Managed Workspace session id; distinct from the provider conversation id",
    )
    agent_session_id: Optional[str] = Field(
        None,
        description="Stable agent CLI conversation id (Claude --session-id); used for /resume and diagnostics.",
    )
    agent_session_id_verified: bool = Field(
        False,
        description=(
            "Whether agent_session_id was confirmed by the provider in a "
            "system/init record. Only a verified id may be passed to --resume; "
            "an unverified (constructive) id uses --session-id. Persisted so a "
            "cold restart does not infer verification from the mere presence "
            "of a UUID."
        ),
    )
    forked_from_tab_id: Optional[str] = Field(
        None,
        description=(
            "Source tab id when this tab was forked from a specific turn of "
            "another conversation; None for tabs started from scratch."
        ),
    )
    forked_from_ordinal: Optional[int] = Field(
        None,
        description=(
            "0-based ordinal of the source turn this tab was forked from "
            "(inclusive); None for tabs started from scratch."
        ),
    )
    parent_tab_id: Optional[str] = Field(
        None,
        description=(
            "Tab that invoked this tab as a Hub subagent (claude-hub agent run); "
            "None for ordinary tabs. Archiving/deleting the parent closes it."
        ),
    )
    subagent_id: Optional[str] = Field(
        None,
        description="Subagent record id when this tab is owned by /api/subagents.",
    )
    archived: bool = Field(
        False,
        description=(
            "Soft-deleted flag. Archived tabs are hidden from the default tab "
            "list and have their runtime resources released, but their JSONL "
            "history is retained for restore."
        ),
    )
    archived_at: Optional[datetime] = Field(
        None,
        description="When the tab was archived; None while active.",
    )
    last_viewed_at: Optional[datetime] = Field(
        None,
        description="When the user last opened/viewed this tab; drives the unread flag.",
    )
    is_unread: Optional[bool] = Field(
        None,
        description="True when the tab's latest completed turn is newer than last_viewed_at.",
    )

    model_config = ConfigDict(from_attributes=True)


class TerminalAgentStatus(BaseModel):
    """Status summary for the floating terminal agent panel."""

    tab_id: str
    tab_name: str
    agent_type: AgentType
    status: AgentRuntimeStatus
    status_text: str
    detail: Optional[str] = None
    tmux_session: str
    last_changed_at: Optional[datetime] = None
    sampled_at: datetime


class RemoteProfile(BaseModel):
    """Configured SSH target for remote tabs."""

    id: str
    name: str
    ssh_host: str
    user: Optional[str] = None
    port: int = 22
    default_cwd: Optional[str] = None
    # Deprecated derived flag: True when ``ssh host 'cmd'`` is swallowed. Kept
    # so older API consumers keep working; new code should read ``transport``
    # and ``interactive_supported`` instead.
    stdin_shell: bool = False
    # Explicit transport override. When None the transport is inferred from the
    # host/alias signatures in services.remote_profiles.
    transport: Optional[RemoteTransport] = None
    # Explicit interactive capability override. None/True means Terminal tabs and
    # remote agents are allowed (normal host or PTY gateway). Set False for a
    # truly browse-only target with no drivable PTY.
    interactive: Optional[bool] = None


class ResidentPeriodicTask(BaseModel):
    """A single recurring instruction the resident agent runs every cycle.

    Periodic tasks are the structured replacement for burying recurring work as
    prose inside the free-text ``resident_agent_directive``. Each enabled entry
    is rendered as an explicit numbered checklist item in the resident prompt on
    every wake-up. They all run together on each resident cycle at the workspace
    interval — there is deliberately no per-entry independent schedule.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    enabled: bool = True
