"""Explicit TTYDManager state and operation contract shared by its mixins."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, AsyncContextManager, Dict, Iterable, List, Optional, Tuple

from ....models import (
    AgentRuntimeStatus,
    AgentType,
    ChatMode,
    ExecutionTarget,
    SessionKind,
    TerminalAgentStatus,
    TerminalTab,
    WorkspaceSessionRole,
)
from ..records import CursorPosition, ScanEntry, _AgentStatusSnapshot, _PendingTabCleanup

if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess
    from ..bindings import RuntimeBindings


class ManagerHost(ABC):
    """State and cross-mixin operations on the single TTYDManager instance."""

    processes: Dict[str, TTYDProcess]
    _next_port: int
    _tab_order: List[str]
    _status_snapshots: Dict[str, _AgentStatusSnapshot]
    _status_cache: Dict[str, TerminalAgentStatus]
    _start_locks: Dict[str, asyncio.Lock]
    _state_error: Optional[str]
    _state_confirmed: bool
    _cleanup_retry_offset: int
    _cleanup_scheduling_stopped: bool
    _fork_locks: Dict[str, asyncio.Lock]

    @cached_property
    @abstractmethod
    def _cleanup_tasks(self) -> Dict[Tuple[str, str], asyncio.Task[bool]]:
        raise NotImplementedError

    @cached_property
    @abstractmethod
    def _pending_stream_cleanup(self) -> Dict[str, _PendingTabCleanup]:
        raise NotImplementedError

    @cached_property
    @abstractmethod
    def _cleanup_locks(self) -> Dict[str, asyncio.Lock]:
        raise NotImplementedError

    @property
    @abstractmethod
    def _runtime(self) -> RuntimeBindings:
        raise NotImplementedError

    @abstractmethod
    def __init__(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _load_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _require_valid_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _persist_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _save_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _load_order(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _save_order(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def set_tab_order(self, tab_ids: List[str]) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _ensure_tab_in_order(self, tab_id: str) -> None:
        raise NotImplementedError()

    @abstractmethod
    def set_tab_workspace_metadata(
        self,
        tab_id: str,
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        workspace_role: Optional[WorkspaceSessionRole],
        managed_session_id: Optional[str] = ...,
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def rename_tab(self, tab_id: str, name: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def set_tab_agent_session_id(self, tab_id: str, agent_session_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def set_tab_chat_mode(self, tab_id: str, mode: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _sync_order_with_processes(self, *, persist: bool = ...) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _get_next_port(self) -> int:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    async def _finish_startup_cleanup(process: TTYDProcess, *, kill_tmux: bool) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def create_tab(
        self,
        name: str,
        shell: Optional[str] = ...,
        cwd: Optional[str] = ...,
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
        cursor_transport: str = ...,
        cursor_data_dir: Optional[str] = ...,
        cursor_cli_version: Optional[str] = ...,
        cursor_transcript_path: Optional[str] = ...,
        cursor_transcript_schema: Optional[str] = ...,
        session_kind: SessionKind = ...,
        chat_mode: ChatMode = ...,
        parent_tab_id: Optional[str] = ...,
        subagent_id: Optional[str] = ...,
    ) -> TerminalTab:
        raise NotImplementedError()

    @abstractmethod
    def _locked_current_tab(self, tab_id: str) -> AsyncContextManager[Optional[TTYDProcess]]:
        raise NotImplementedError()

    @abstractmethod
    async def ensure_tab_tmux_session(self, tab_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def duplicate_tab(self, tab_id: str) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    def _check_live_tab_capacity(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _count_forks(self, source_tab_id: str) -> int:
        raise NotImplementedError()

    @abstractmethod
    def _fork_lock(self, source_tab_id: str) -> asyncio.Lock:
        raise NotImplementedError()

    @abstractmethod
    async def fork_tab(self, tab_id: str, ordinal: int) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def delete_tab(self, tab_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _finish_tab_cleanup(self, record: _PendingTabCleanup) -> None:
        raise NotImplementedError()

    @abstractmethod
    def schedule_pending_stream_cleanup(self, *, limit: int = ...) -> List[asyncio.Task[bool]]:
        raise NotImplementedError()

    @abstractmethod
    async def _run_tab_cleanup(self, record: _PendingTabCleanup) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _tab_cleanup_done(self, key: Tuple[str, str], task: asyncio.Task[bool]) -> None:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    async def _await_cleanup_tasks(tasks: List[asyncio.Task[bool]]) -> List[bool | BaseException]:
        raise NotImplementedError()

    @abstractmethod
    def cleanup_task_lifetime(self) -> AsyncContextManager[None]:
        raise NotImplementedError()

    @abstractmethod
    async def retry_pending_stream_cleanup(self, *, limit: int = ...) -> int:
        raise NotImplementedError()

    @abstractmethod
    async def archive_tab(self, tab_id: str) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def unarchive_tab(self, tab_id: str) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    def list_archived_tabs(self) -> list[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    def get_tab(self, tab_id: str) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def ensure_tab_running(self, tab_id: str) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def _start_missing_tab_identity_safe(self, process: TTYDProcess) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _finish_explicit_codex_recovery_rollback(
        self, process: TTYDProcess, reason: str
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    def list_tabs(self) -> list[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def get_tab_history(self, tab_id: str, lines: int = ...) -> Optional[str]:
        raise NotImplementedError()

    @abstractmethod
    async def get_tab_cursor_position(self, tab_id: str) -> Optional[CursorPosition]:
        raise NotImplementedError()

    @abstractmethod
    def _classify_agent_status(
        self, process: TTYDProcess, output: str, output_hash: str, foreground_command: Optional[str]
    ) -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
        raise NotImplementedError()

    @abstractmethod
    def mark_tab_viewed(self, tab_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def compute_tab_unread(self, process: TTYDProcess) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def get_tab_agent_status(
        self, tab_id: str, use_cache: bool = ..., live_sessions: Optional[set[str]] = ...
    ) -> Optional[TerminalAgentStatus]:
        raise NotImplementedError()

    @abstractmethod
    async def list_tab_agent_statuses(
        self, tab_ids: Optional[Iterable[str]] = ...
    ) -> list[TerminalAgentStatus]:
        raise NotImplementedError()

    @abstractmethod
    async def update_tab(
        self,
        tab_id: str,
        name: Optional[str] = ...,
        shell: Optional[str] = ...,
        cwd: Optional[str] = ...,
        solo_mode: Optional[bool] = ...,
        agent_type: Optional[AgentType] = ...,
        target: Optional[ExecutionTarget] = ...,
        remote_profile_id: Optional[str] = ...,
        remote_cwd: Optional[str] = ...,
        remote_reconnect: Optional[bool] = ...,
        env: Optional[Dict[str, str]] = ...,
    ) -> Optional[TerminalTab]:
        raise NotImplementedError()

    @abstractmethod
    async def switch_env(
        self, tab_id: str, env: Dict[str, str], solo_mode: Optional[bool] = ...
    ) -> TerminalTab:
        raise NotImplementedError()

    @abstractmethod
    def _backfill_agent_session_ids(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _backfill_codex_session_ids(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _quarantine_codex_tab(self, tab: TTYDProcess, reason: str) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _codex_poll_signal(self, tab: TTYDProcess) -> Dict[str, str]:
        raise NotImplementedError()

    @abstractmethod
    def _salvage_pending_codex_signal(self, tab: TTYDProcess) -> Optional[Tuple[str, str]]:
        raise NotImplementedError()

    @abstractmethod
    async def _launch_one_cold_codex_locked(self, tab: TTYDProcess) -> Tuple[str, Optional[str]]:
        raise NotImplementedError()

    @abstractmethod
    def _schedule_codex_discovery(self, process: TTYDProcess, delay: float = ...) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def start_all_tabs(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _prune_orphan_tmux_sessions(self, *, grace_seconds: float = ...) -> list[str]:
        raise NotImplementedError()

    @abstractmethod
    async def _reconcile_codex_phase_r(
        self, launched: List[TTYDProcess], pre_global_scan: Dict[str, ScanEntry]
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def cleanup(self) -> None:
        raise NotImplementedError()
