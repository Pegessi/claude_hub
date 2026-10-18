"""Finite, typed and immutable runtime dependency bindings."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .binding_calls import (
    AgentSpawnEnvCall,
    ClaudeProjectDirForCwdCall,
    CodexCandidatesForCwdCall,
    CodexHomeDirCall,
    CodexIdExistsCall,
    CodexRootsCall,
    CodexScanSessionsCall,
    CodexTitleFromLinesCall,
    CursorCliVersionFromExecutableCall,
    CursorDataDirForEnvCall,
    CursorIdExists,
    CursorTerminalTranscriptPathCall,
    CursorTerminalTranscriptProvenanceValidCall,
    DiffScansCall,
    EnsureTmuxServerCall,
    EventTurnOrdinalsCall,
    FinishTmuxLauncherRollbackCall,
    GetAgentCommandCall,
    GetDefaultCommandCall,
    GetTabNativeRuntimeSnapshotCall,
    IsLocalPortAvailableCall,
    IsLocalPortListeningCall,
    IsLoopbackUrlCall,
    JsonlStartEpochCall,
    NativeProviderBinaryAvailableCall,
    ParseOptionalIsoCall,
    ParseSessionMetaCall,
    ParseTabsSnapshotCall,
    PickBackfillSessionCall,
    PromoteCursorChatTransportCall,
    RefreshTmuxServerEnvironmentCall,
    TmuxCommand,
    TmuxKillSessionCall,
    TmuxListSessionCreatedCall,
    TmuxListSessionsCall,
    TmuxServerRunningCall,
    TmuxSessionCreatedCall,
    TmuxSessionExistsAsyncCall,
    TmuxSessionNameCall,
    WritePrivateLaunchFileCall,
    WriteTabsSnapshotCall,
)

if TYPE_CHECKING:
    from ..ttyd_manager import TTYDProcess


@dataclass(frozen=True)
class RuntimeBindings:
    """Read current named compatibility dependencies without copying owner state."""

    read_GLOBAL_CODEX_LAUNCH_LOCK: Callable[[], asyncio.Lock]
    read_LAUNCH_ENV_DIR: Callable[[], Path]
    read_ORDER_FILE: Callable[[], Path]
    read_STATE_FILE: Callable[[], Path]
    read_TTYDProcess: Callable[[], type[TTYDProcess]]
    read__CODEX_TITLE_SCAN_MAX_LINES: Callable[[], int]
    read__TAB_CREATE_STARTUP_TIMEOUT_SECONDS: Callable[[], float]
    read__agent_spawn_env: Callable[[], AgentSpawnEnvCall]
    read__claude_project_dir_for_cwd: Callable[[], ClaudeProjectDirForCwdCall]
    read__codex_candidates_for_cwd: Callable[[], CodexCandidatesForCwdCall]
    read__codex_home_dir: Callable[[], CodexHomeDirCall]
    read__codex_id_exists: Callable[[], CodexIdExistsCall]
    read__codex_roots: Callable[[], CodexRootsCall]
    read__codex_scan_sessions: Callable[[], CodexScanSessionsCall]
    read__codex_title_from_lines: Callable[[], CodexTitleFromLinesCall]
    read__cursor_id_exists: Callable[[], CursorIdExists]
    read__diff_scans: Callable[[], DiffScansCall]
    read__ensure_tmux_server: Callable[[], EnsureTmuxServerCall]
    read__event_turn_ordinals: Callable[[], EventTurnOrdinalsCall]
    read__finish_tmux_launcher_rollback: Callable[[], FinishTmuxLauncherRollbackCall]
    read__is_local_port_available: Callable[[], IsLocalPortAvailableCall]
    read__is_local_port_listening: Callable[[], IsLocalPortListeningCall]
    read__is_loopback_url: Callable[[], IsLoopbackUrlCall]
    read__jsonl_start_epoch: Callable[[], JsonlStartEpochCall]
    read__parse_optional_iso: Callable[[], ParseOptionalIsoCall]
    read__parse_session_meta: Callable[[], ParseSessionMetaCall]
    read__parse_tabs_snapshot: Callable[[], ParseTabsSnapshotCall]
    read__pick_backfill_session: Callable[[], PickBackfillSessionCall]
    read__promote_cursor_chat_transport: Callable[[], PromoteCursorChatTransportCall]
    read__refresh_tmux_server_environment: Callable[[], RefreshTmuxServerEnvironmentCall]
    read__tmux_kill_session: Callable[[], TmuxKillSessionCall]
    read__tmux_list_session_created: Callable[[], TmuxListSessionCreatedCall]
    read__tmux_list_sessions: Callable[[], TmuxListSessionsCall]
    read__tmux_server_running: Callable[[], TmuxServerRunningCall]
    read__tmux_session_created: Callable[[], TmuxSessionCreatedCall]
    read__tmux_session_exists_async: Callable[[], TmuxSessionExistsAsyncCall]
    read__tmux_session_name: Callable[[], TmuxSessionNameCall]
    read__write_private_launch_file: Callable[[], WritePrivateLaunchFileCall]
    read__write_tabs_snapshot: Callable[[], WriteTabsSnapshotCall]
    read_cursor_cli_version_from_executable: Callable[[], CursorCliVersionFromExecutableCall]
    read_cursor_data_dir_for_env: Callable[[], CursorDataDirForEnvCall]
    read_cursor_terminal_transcript_path: Callable[[], CursorTerminalTranscriptPathCall]
    read_cursor_terminal_transcript_provenance_valid: Callable[
        [], CursorTerminalTranscriptProvenanceValidCall
    ]
    read_datetime: Callable[[], type[datetime]]
    read_get_agent_command: Callable[[], GetAgentCommandCall]
    read_get_default_command: Callable[[], GetDefaultCommandCall]
    read_get_tab_native_runtime_snapshot: Callable[[], GetTabNativeRuntimeSnapshotCall]
    read_native_provider_binary_available: Callable[[], NativeProviderBinaryAvailableCall]
    read_tmux_command: Callable[[], TmuxCommand]

    @property
    def GLOBAL_CODEX_LAUNCH_LOCK(self) -> asyncio.Lock:
        return self.read_GLOBAL_CODEX_LAUNCH_LOCK()

    @property
    def LAUNCH_ENV_DIR(self) -> Path:
        return self.read_LAUNCH_ENV_DIR()

    @property
    def ORDER_FILE(self) -> Path:
        return self.read_ORDER_FILE()

    @property
    def STATE_FILE(self) -> Path:
        return self.read_STATE_FILE()

    @property
    def TTYDProcess(self) -> type[TTYDProcess]:
        return self.read_TTYDProcess()

    @property
    def _TAB_CREATE_STARTUP_TIMEOUT_SECONDS(self) -> float:
        return self.read__TAB_CREATE_STARTUP_TIMEOUT_SECONDS()

    @property
    def _agent_spawn_env(self) -> AgentSpawnEnvCall:
        return self.read__agent_spawn_env()

    @property
    def _claude_project_dir_for_cwd(self) -> ClaudeProjectDirForCwdCall:
        return self.read__claude_project_dir_for_cwd()

    @property
    def _codex_candidates_for_cwd(self) -> CodexCandidatesForCwdCall:
        return self.read__codex_candidates_for_cwd()

    @property
    def _codex_home_dir(self) -> CodexHomeDirCall:
        return self.read__codex_home_dir()

    @property
    def _codex_id_exists(self) -> CodexIdExistsCall:
        return self.read__codex_id_exists()

    @property
    def _codex_roots(self) -> CodexRootsCall:
        return self.read__codex_roots()

    @property
    def _codex_scan_sessions(self) -> CodexScanSessionsCall:
        return self.read__codex_scan_sessions()

    @property
    def _codex_title_from_lines(self) -> CodexTitleFromLinesCall:
        return self.read__codex_title_from_lines()

    @property
    def _cursor_id_exists(self) -> CursorIdExists:
        return self.read__cursor_id_exists()

    @property
    def _diff_scans(self) -> DiffScansCall:
        return self.read__diff_scans()

    @property
    def _ensure_tmux_server(self) -> EnsureTmuxServerCall:
        return self.read__ensure_tmux_server()

    @property
    def _event_turn_ordinals(self) -> EventTurnOrdinalsCall:
        return self.read__event_turn_ordinals()

    @property
    def _finish_tmux_launcher_rollback(self) -> FinishTmuxLauncherRollbackCall:
        return self.read__finish_tmux_launcher_rollback()

    @property
    def _is_local_port_available(self) -> IsLocalPortAvailableCall:
        return self.read__is_local_port_available()

    @property
    def _is_local_port_listening(self) -> IsLocalPortListeningCall:
        return self.read__is_local_port_listening()

    @property
    def _is_loopback_url(self) -> IsLoopbackUrlCall:
        return self.read__is_loopback_url()

    @property
    def _jsonl_start_epoch(self) -> JsonlStartEpochCall:
        return self.read__jsonl_start_epoch()

    @property
    def _parse_optional_iso(self) -> ParseOptionalIsoCall:
        return self.read__parse_optional_iso()

    @property
    def _parse_session_meta(self) -> ParseSessionMetaCall:
        return self.read__parse_session_meta()

    @property
    def _parse_tabs_snapshot(self) -> ParseTabsSnapshotCall:
        return self.read__parse_tabs_snapshot()

    @property
    def _pick_backfill_session(self) -> PickBackfillSessionCall:
        return self.read__pick_backfill_session()

    @property
    def _promote_cursor_chat_transport(self) -> PromoteCursorChatTransportCall:
        return self.read__promote_cursor_chat_transport()

    @property
    def _refresh_tmux_server_environment(self) -> RefreshTmuxServerEnvironmentCall:
        return self.read__refresh_tmux_server_environment()

    @property
    def _tmux_kill_session(self) -> TmuxKillSessionCall:
        return self.read__tmux_kill_session()

    @property
    def _tmux_list_session_created(self) -> TmuxListSessionCreatedCall:
        return self.read__tmux_list_session_created()

    @property
    def _tmux_list_sessions(self) -> TmuxListSessionsCall:
        return self.read__tmux_list_sessions()

    @property
    def _tmux_server_running(self) -> TmuxServerRunningCall:
        return self.read__tmux_server_running()

    @property
    def _tmux_session_created(self) -> TmuxSessionCreatedCall:
        return self.read__tmux_session_created()

    @property
    def _tmux_session_exists_async(self) -> TmuxSessionExistsAsyncCall:
        return self.read__tmux_session_exists_async()

    @property
    def _tmux_session_name(self) -> TmuxSessionNameCall:
        return self.read__tmux_session_name()

    @property
    def _write_private_launch_file(self) -> WritePrivateLaunchFileCall:
        return self.read__write_private_launch_file()

    @property
    def _write_tabs_snapshot(self) -> WriteTabsSnapshotCall:
        return self.read__write_tabs_snapshot()

    @property
    def cursor_cli_version_from_executable(self) -> CursorCliVersionFromExecutableCall:
        return self.read_cursor_cli_version_from_executable()

    @property
    def cursor_data_dir_for_env(self) -> CursorDataDirForEnvCall:
        return self.read_cursor_data_dir_for_env()

    @property
    def cursor_terminal_transcript_path(self) -> CursorTerminalTranscriptPathCall:
        return self.read_cursor_terminal_transcript_path()

    @property
    def cursor_terminal_transcript_provenance_valid(
        self,
    ) -> CursorTerminalTranscriptProvenanceValidCall:
        return self.read_cursor_terminal_transcript_provenance_valid()

    @property
    def datetime(self) -> type[datetime]:
        return self.read_datetime()

    @property
    def get_agent_command(self) -> GetAgentCommandCall:
        return self.read_get_agent_command()

    @property
    def get_default_command(self) -> GetDefaultCommandCall:
        return self.read_get_default_command()

    @property
    def get_tab_native_runtime_snapshot(self) -> GetTabNativeRuntimeSnapshotCall:
        return self.read_get_tab_native_runtime_snapshot()

    @property
    def native_provider_binary_available(self) -> NativeProviderBinaryAvailableCall:
        return self.read_native_provider_binary_available()

    @property
    def tmux_command(self) -> TmuxCommand:
        return self.read_tmux_command()

    @property
    def _CODEX_TITLE_SCAN_MAX_LINES(self) -> int:
        return self.read__CODEX_TITLE_SCAN_MAX_LINES()
