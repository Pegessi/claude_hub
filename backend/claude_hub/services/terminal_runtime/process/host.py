"""Explicit TTYDProcess state and operation contract shared by its mixins."""

from __future__ import annotations

import asyncio
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager, Dict, Optional

from ....models import (
    AgentType,
    ChatMode,
    ExecutionTarget,
    RemoteProfile,
    SessionKind,
    TerminalTab,
    WorkspaceSessionRole,
)
from ..records import CursorPosition, ScanEntry

if TYPE_CHECKING:
    from ..bindings import RuntimeBindings


class ProcessHost(ABC):
    """State and cross-mixin operations on the single TTYDProcess instance."""

    tab_id: str
    port: int
    name: str
    cwd: Optional[str]
    solo_mode: bool
    agent_type: AgentType
    session_kind: SessionKind
    chat_mode: ChatMode
    forked_from_tab_id: Optional[str]
    forked_from_ordinal: Optional[int]
    target: ExecutionTarget
    remote_profile_id: Optional[str]
    remote_cwd: Optional[str]
    remote_reconnect: bool
    remote_forward_port: Optional[int]
    workspace_id: Optional[str]
    workspace_name: Optional[str]
    workspace_role: Optional[WorkspaceSessionRole]
    managed_session_id: Optional[str]
    cursor_transport: str
    cursor_data_dir: Optional[str]
    cursor_cli_version: Optional[str]
    cursor_transcript_path: Optional[str]
    cursor_transcript_schema: Optional[str]
    env: Dict[str, str]
    process: Optional[asyncio.subprocess.Process]
    created_at: datetime
    is_active: bool
    archived: bool
    archived_at: Optional[datetime]
    last_viewed_at: Optional[datetime]
    tmux_session: str
    agent_session_id: Optional[str]
    agent_session_id_verified: bool
    _has_explicit_session_id: bool
    from_persisted_state: bool
    resume_quarantined: bool
    _launch_wall: Optional[float]
    _launch_mono: Optional[float]
    _is_new_pin: Optional[bool]
    _pre_scan: Optional[Dict[str, ScanEntry]]
    _pending_quarantine_reason: Optional[str]
    _shell_explicitly_provided: bool
    shell: str
    _tunnel_registry: dict[str, tuple[int, subprocess.Popen]]
    _TUNNEL_SCRIPT_TEMPLATE: str
    _TTYD_CLIENT_OPTIONS: tuple[tuple[str, str], ...]

    @property
    @abstractmethod
    def _runtime(self) -> RuntimeBindings:
        raise NotImplementedError

    @abstractmethod
    def __init__(
        self,
        tab_id: str,
        port: int,
        name: str,
        shell: Optional[str] = ...,
        cwd: Optional[str] = ...,
        created_at: Optional[datetime] = ...,
        solo_mode: bool = ...,
        agent_type: AgentType = ...,
        target: ExecutionTarget = ...,
        remote_profile_id: Optional[str] = ...,
        remote_cwd: Optional[str] = ...,
        remote_reconnect: bool = ...,
        remote_forward_port: Optional[int] = ...,
        workspace_id: Optional[str] = ...,
        workspace_name: Optional[str] = ...,
        workspace_role: Optional[WorkspaceSessionRole] = ...,
        managed_session_id: Optional[str] = ...,
        env: Optional[Dict[str, str]] = ...,
        agent_session_id: Optional[str] = ...,
        agent_session_id_verified: bool = ...,
        from_persisted_state: bool = ...,
        resume_quarantined: bool = ...,
        shell_explicitly_provided: Optional[bool] = ...,
        cursor_transport: str = ...,
        cursor_data_dir: Optional[str] = ...,
        cursor_cli_version: Optional[str] = ...,
        cursor_transcript_path: Optional[str] = ...,
        cursor_transcript_schema: Optional[str] = ...,
        session_kind: SessionKind = ...,
        chat_mode: ChatMode = ...,
        forked_from_tab_id: Optional[str] = ...,
        forked_from_ordinal: Optional[int] = ...,
        archived: bool = ...,
        archived_at: Optional[datetime] = ...,
        last_viewed_at: Optional[datetime] = ...,
    ) -> None:
        raise NotImplementedError()

    @property
    @abstractmethod
    def is_managed_chat(self) -> bool:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _clean_env(env: Dict[str, str]) -> Dict[str, str]:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _default_env_for_agent(agent_type: AgentType) -> Dict[str, str]:
        raise NotImplementedError()

    @abstractmethod
    def _prepare_agent_env(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _normalize_volcengine_coding_plan_model(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _setup_tunnel_env(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _child_env(self) -> Dict[str, str]:
        raise NotImplementedError()

    @abstractmethod
    def _env_shell_prefix(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _env_export_commands(self) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    def _shell_child_env(self) -> Dict[str, str]:
        raise NotImplementedError()

    @abstractmethod
    def _with_env(self, command: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _with_local_env_wrapper(self, command: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _write_launch_settings_file(self) -> Optional[Path]:
        raise NotImplementedError()

    @abstractmethod
    def _claude_settings_arg(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _claude_bypass_acceptance_arg(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _claude_model_arg(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _codex_model_arg(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _solo_command(self) -> Optional[str]:
        raise NotImplementedError()

    @abstractmethod
    def _tmux_shell_command(self, session_exists: bool) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def ensure_tmux_session(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _build_ttyd_command(self, session_exists: bool) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    def _claude_session_arg(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _claude_command(self, session_flag: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _should_recover(self, session_exists: bool) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _discover_codex_session_id(self, launch_epoch: float) -> Optional[str]:
        raise NotImplementedError()

    @abstractmethod
    def _codex_launch_command(self, recover: bool) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _traex_launch_command(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _agent_start_command(self, recover: bool = ...) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def switch_env(self, new_env: Dict[str, str], solo_mode: Optional[bool] = ...) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _remote_ssh_target(self) -> tuple[str, int]:
        raise NotImplementedError()

    @abstractmethod
    def _remote_profile(self) -> RemoteProfile:
        raise NotImplementedError()

    @abstractmethod
    def _remote_is_pty_gateway(self) -> bool:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _remote_path_bootstrap() -> str:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _remote_shell_command(script: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _build_remote_ssh_command(self, remote_command: str) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    def _build_remote_attach_script(self, *, bootstrap_file: Optional[str] = ...) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _build_remote_attach_command(self) -> str:
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def _remote_cwd_command(cls, cwd: str, shell: str) -> str:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _quote_remote_cwd(cwd: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    def _build_pty_gateway_launcher(self) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    def _build_remote_launcher(self) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    async def _log_stderr(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _configure_tmux(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _run_remote_capture_command(self, remote_command: str) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def _capture_remote_history(self, lines: int = ...) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def _capture_local_history(self, lines: int = ...) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def capture_history(self, lines: int = ..., prefer_remote: bool = ...) -> str:
        raise NotImplementedError()

    @abstractmethod
    async def _capture_local_cursor_position(self) -> Optional[CursorPosition]:
        raise NotImplementedError()

    @abstractmethod
    async def _capture_remote_cursor_position(self) -> Optional[CursorPosition]:
        raise NotImplementedError()

    @abstractmethod
    async def capture_cursor_position(self, prefer_remote: bool = ...) -> Optional[CursorPosition]:
        raise NotImplementedError()

    @abstractmethod
    async def capture_foreground_command(self) -> Optional[str]:
        raise NotImplementedError()

    @abstractmethod
    async def stop(self, kill_tmux: bool = ...) -> None:
        raise NotImplementedError()

    @abstractmethod
    def to_dict(self) -> dict:
        raise NotImplementedError()

    @abstractmethod
    def to_schema(self) -> TerminalTab:
        raise NotImplementedError()
