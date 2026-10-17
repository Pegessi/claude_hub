"""Manager tabs responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Dict, Optional

from ....models import (
    AgentType,
    ChatMode,
    ExecutionTarget,
    SessionKind,
    TerminalTab,
    WorkspaceSessionRole,
)
from ....models.schemas import CHAT_REMOTE_UNSUPPORTED
from ...remote_profiles import reject_unsupported_interactive, remote_profile_manager
from ..records import CursorPosition
from .host import ManagerHost

if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerTabsMixin(ManagerHost):
    def set_tab_workspace_metadata(
        self,
        tab_id: str,
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        workspace_role: Optional[WorkspaceSessionRole],
        managed_session_id: Optional[str] = None,
    ) -> bool:
        """Attach workspace ownership metadata to an existing tab."""
        process = self.processes.get(tab_id)
        if not process:
            return False
        if (
            process.session_kind == SessionKind.CHAT
            and workspace_role is not None
            and not process.is_managed_chat
        ):
            # Metadata cannot tear down an existing direct Chat's terminal runtime.
            raise ValueError("Direct Chat tabs cannot become managed Chat through metadata")
        next_managed_session_id = (
            None if workspace_id is None else managed_session_id or process.managed_session_id
        )
        if (
            process.workspace_id == workspace_id
            and process.workspace_name == workspace_name
            and process.workspace_role == workspace_role
            and process.managed_session_id == next_managed_session_id
        ):
            return True

        process.workspace_id = workspace_id
        process.workspace_name = workspace_name
        process.workspace_role = workspace_role
        process.managed_session_id = next_managed_session_id
        self._save_state()
        return True

    def rename_tab(self, tab_id: str, name: str) -> bool:
        """Rename a tab without restarting its terminal process."""
        process = self.processes.get(tab_id)
        if not process:
            return False

        if process.name == name:
            return True

        process.name = name
        self._save_state()
        return True

    def set_tab_agent_session_id(self, tab_id: str, agent_session_id: str) -> bool:
        """Durably persist the provider conversation id for a tab.

        The captured conversation id (Claude/Codex/Cursor) is stored on the tab
        so a cold restart can resume the same provider conversation via
        ``--resume`` (Claude/Cursor) or ``thread/resume`` (Codex).

        This is only called after the provider has emitted the id in a
        system/init record, so ``agent_session_id_verified`` is set to True
        alongside the id. A cold restart seeds ``_conversation_id_verified``
        from this flag, never from the mere presence of a UUID.
        """
        process = self.processes.get(tab_id)
        if not process:
            return False
        if process.agent_session_id == agent_session_id and process.agent_session_id_verified:
            return True
        process.agent_session_id = agent_session_id
        process.agent_session_id_verified = True
        self._save_state()
        return True

    def set_tab_chat_mode(self, tab_id: str, mode: str) -> bool:
        """Durably persist the mode selected for subsequent Chat turns."""

        process = self.processes.get(tab_id)
        if process is None:
            return False
        if process.workspace_role is not None or process.session_kind != SessionKind.CHAT:
            raise ValueError("Chat mode is available only for direct top-level Chat tabs")
        parsed = ChatMode(mode)
        if process.chat_mode == parsed:
            return True
        process.chat_mode = parsed
        self._save_state()
        return True

    def get_tab(self, tab_id: str) -> Optional[TerminalTab]:
        if tab_id not in self.processes:
            return None
        return self.processes[tab_id].to_schema()

    async def ensure_tab_running(self, tab_id: str) -> Optional[TerminalTab]:
        """Ensure the current, non-archived tab has a live ttyd listener."""
        async with self._locked_current_tab(tab_id) as process:
            if process is None or process.archived:
                return None
            if self._runtime._is_local_port_listening(process.port):
                process.is_active = True
                return process.to_schema()
            if process.process and process.process.returncode is None:
                logger.warning(
                    "Tab %s has a live ttyd process object but port %s is not listening; restarting ttyd",
                    tab_id,
                    process.port,
                )
                await process.stop(kill_tmux=False)
            logger.info(
                "Starting missing ttyd listener for tab %s on port %s", tab_id, process.port
            )
            try:
                await self._start_missing_tab_identity_safe(process)
            except Exception:
                # A backend from an earlier reload can still own the listener briefly.
                if self._runtime._is_local_port_listening(process.port):
                    logger.warning(
                        "Tab %s start failed but port %s is already listening; treating it as available",
                        tab_id,
                        process.port,
                    )
                    process.is_active = True
                    return process.to_schema()
                process.is_active = False
                raise
            return process.to_schema()

    async def _start_missing_tab_identity_safe(self, process: TTYDProcess) -> None:
        """Start an explicitly opened stopped tab without losing agent identity.

        A hot backend restart intentionally leaves persisted tabs with no live
        tmux session stopped.  Opening one is therefore a real cold recovery,
        not merely a missing ttyd listener.  Local Codex recovery must reuse
        the same attribution and reconciliation fence as bulk cold startup;
        Cursor/Claude may update their pinned id while building the recovery
        command, so their resulting state is persisted before returning.
        """
        session_exists = await self._runtime._tmux_session_exists_async(process.tmux_session)
        is_local_codex = (
            not session_exists
            and process.target == ExecutionTarget.LOCAL
            and process.agent_type == AgentType.CODEX
            and not process._shell_explicitly_provided
        )
        if not is_local_codex:
            try:
                await process.start()
            finally:
                # Cursor rotates an unverifiable persisted id while building
                # its recovery command; Claude can defensively allocate a
                # missing legacy id. Persist even if ttyd startup then fails so
                # the next attempt targets the same conversation identity.
                self._save_state()
            return

        # A prior launch in this backend process may have left attribution
        # scratch fields populated even though its tmux was later killed.
        process._launch_wall = None
        process._launch_mono = None
        process._is_new_pin = None
        process._pre_scan = None
        process._pending_quarantine_reason = None

        async with self._runtime.GLOBAL_CODEX_LAUNCH_LOCK:
            pre_global_scan = self._runtime._codex_scan_sessions()
            try:
                outcome, _ = await self._launch_one_cold_codex_locked(process)
                if outcome == "PendingQ":
                    raise RuntimeError(
                        "unresolved Codex ownership signal for explicitly opened "
                        f"tab {process.tab_id}: {process._pending_quarantine_reason}"
                    )
                await self._reconcile_codex_phase_r([process], pre_global_scan)
                if process.resume_quarantined:
                    raise RuntimeError(
                        f"Codex identity reconciliation quarantined tab {process.tab_id}"
                    )
            except asyncio.CancelledError:
                try:
                    await self._finish_explicit_codex_recovery_rollback(
                        process,
                        "explicit cold recovery cancelled before identity commit",
                    )
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "Cancelled Codex explicit recovery could not prove process teardown "
                        f"for tab {process.tab_id}"
                    ) from cleanup_error
                raise
            except Exception as error:
                try:
                    await self._finish_explicit_codex_recovery_rollback(
                        process,
                        f"explicit cold recovery rollback: {error!r}",
                    )
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "Codex explicit recovery could not prove process teardown "
                        f"for tab {process.tab_id}"
                    ) from cleanup_error
                raise

            # Commit the attributed SID before ttyd attaches. If ttyd binding
            # fails, the owned tmux and durable identity remain safe to retry.
            self._save_state()

        await process.start()

    async def _finish_explicit_codex_recovery_rollback(
        self,
        process: TTYDProcess,
        reason: str,
    ) -> None:
        """Shield Codex teardown/state commit from request or reload cancellation."""
        cleanup_task = asyncio.create_task(self._quarantine_codex_tab(process, reason))
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # Repeated shutdown cancellation must not strand an
                    # unattributed Codex writer. The caller re-raises its original
                    # cancellation only after this task proves teardown.
                    continue
            await cleanup_task
        finally:
            # _quarantine_codex_tab deliberately retains its cleanup-failed
            # evidence if tmux teardown cannot be proven. Persist that evidence
            # even when the cleanup task itself raises.
            self._save_state()

    def list_tabs(self) -> list[TerminalTab]:
        # Return tabs in saved order
        ordered_tabs: list[TerminalTab] = []
        # First add tabs in the saved order
        for tab_id in self._tab_order:
            if tab_id in self.processes and not self.processes[tab_id].archived:
                ordered_tabs.append(self.processes[tab_id].to_schema())
        # Then add any tabs not in the order list
        for process in self.processes.values():
            if process.tab_id not in self._tab_order and not process.archived:
                ordered_tabs.append(process.to_schema())
        logger.debug("list_tabs returning %d active tabs", len(ordered_tabs))
        return ordered_tabs

    async def get_tab_history(self, tab_id: str, lines: int = 100000) -> Optional[str]:
        """Get tmux terminal history for a tab (scrollback + visible screen)."""
        process = self.processes.get(tab_id)
        if not process:
            return None
        return await process.capture_history(lines, prefer_remote=True)

    async def get_tab_cursor_position(self, tab_id: str) -> Optional[CursorPosition]:
        """Get tmux cursor position for a tab."""
        process = self.processes.get(tab_id)
        if not process:
            return None
        return await process.capture_cursor_position(prefer_remote=True)

    async def update_tab(
        self,
        tab_id: str,
        name: Optional[str] = None,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: Optional[bool] = None,
        agent_type: Optional[AgentType] = None,
        target: Optional[ExecutionTarget] = None,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: Optional[bool] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Optional[TerminalTab]:
        """Update tab settings. Note: Changing shell/cwd/solo_mode/agent_type requires restarting
        the ttyd process, but the tmux session will be PRESERVED.
        """
        async with self._locked_current_tab(tab_id) as process:
            if process is None:
                return None
            needs_restart = False

            if (
                agent_type is not None
                and agent_type != process.agent_type
                and AgentType.MASO in {agent_type, process.agent_type}
            ):
                raise ValueError("Create a new Chat to change the MASO provider")
            if process.agent_type == AgentType.MASO and env is not None and env != process.env:
                raise ValueError("Use switch-env to change a MASO Chat's model")
            if process.agent_type == AgentType.MASO and cwd is not None and cwd != process.cwd:
                raise ValueError("Create a new MASO Chat to change its working directory")

            # Validate the resulting surface before mutating any existing tab.
            if (agent_type or process.agent_type) == AgentType.MASO and (
                process.session_kind != SessionKind.CHAT
                or (target or process.target) != ExecutionTarget.LOCAL
            ):
                raise ValueError("MASO is only available for local Chat sessions")
            if process.agent_type == AgentType.MASO and env is not None:
                from ...agent_stream.maso_http import maso_base_url

                if maso_base_url(env) != maso_base_url(process.env):
                    raise ValueError("Create a new MASO Chat to change MASO_BASE_URL")

            if name:
                process.name = name
            if shell is not None:
                process.shell = shell
                needs_restart = True
            if cwd is not None:
                process.cwd = cwd
                needs_restart = True
            if solo_mode is not None:
                process.solo_mode = solo_mode
                needs_restart = True
            if agent_type is not None:
                if process.agent_type != agent_type:
                    # Reset agent_session_id when switching agent types: ids are
                    # agent-specific (claude uuids vs codex ULID-uuid hybrids vs
                    # cursor opaque hashes) and a stale id from the wrong agent
                    # will corrupt resume. The new agent's launch path will
                    # generate/pin a fresh id.
                    process.agent_session_id = None
                    if agent_type in (AgentType.CLAUDE, AgentType.CODEX):
                        process.agent_session_id = str(uuid.uuid4())
                    process.cursor_transport = "terminal"
                    process.cursor_data_dir = None
                    process.cursor_cli_version = None
                    process.cursor_transcript_path = None
                    process.cursor_transcript_schema = None
                    logger.info(f"reset agent_session_id for tab {tab_id} due to agent_type change")
                process.agent_type = agent_type
                needs_restart = True
            next_target = target if target is not None else process.target
            if process.session_kind == SessionKind.CHAT and next_target == ExecutionTarget.REMOTE:
                raise ValueError(CHAT_REMOTE_UNSUPPORTED)
            if next_target == ExecutionTarget.REMOTE:
                next_profile_id = (
                    remote_profile_id
                    if remote_profile_id is not None
                    else process.remote_profile_id
                )
                reject_unsupported_interactive(
                    remote_profile_manager.get_profile(next_profile_id or "")
                )
            if target is not None:
                process.target = target
                needs_restart = True
            if remote_profile_id is not None:
                process.remote_profile_id = remote_profile_id
                needs_restart = True
            if remote_cwd is not None:
                process.remote_cwd = remote_cwd
                needs_restart = True
            if remote_reconnect is not None:
                process.remote_reconnect = remote_reconnect
                needs_restart = True
            if env is not None:
                process.env = self._runtime.TTYDProcess._clean_env(env)
                process._prepare_agent_env()
                if process.agent_type != AgentType.CLAUDE:
                    process._setup_tunnel_env()
                needs_restart = True

            if process.cursor_transport == "terminal_transcript" and not (
                process.target == ExecutionTarget.LOCAL
                and self._runtime.cursor_terminal_transcript_provenance_valid(
                    cwd=process.cwd,
                    session_id=process.agent_session_id,
                    cli_version=process.cursor_cli_version,
                    transcript_path=process.cursor_transcript_path,
                    transcript_schema=process.cursor_transcript_schema,
                    data_dir=process.cursor_data_dir,
                    env=process.env,
                )
            ):
                process.cursor_transport = "terminal"
                process.cursor_data_dir = None
                process.cursor_cli_version = None
                process.cursor_transcript_path = None
                process.cursor_transcript_schema = None

            # Enforce the "Chat = native structured" invariant after every field
            # update. An agent_type switch (above) resets cursor_transport to
            # "terminal"; re-promote a local Cursor Chat tab so it does not fail
            # closed in the adapter registry.
            process.cursor_transport = self._runtime._promote_cursor_chat_transport(
                process.session_kind,
                process.agent_type,
                process.target,
                process.cursor_transport,
            )

            if needs_restart and not process.archived:
                logger.info(
                    f"Updating tab {tab_id}, restarting ttyd but preserving tmux session {process.tmux_session}"
                )
                await process.stop(kill_tmux=False)
                await process.start()

            self._save_state()
            return process.to_schema()

    async def switch_env(
        self,
        tab_id: str,
        env: Dict[str, str],
        solo_mode: Optional[bool] = None,
    ) -> TerminalTab:
        """Hot-swap a live tab's environment while preserving its conversation."""
        async with self._locked_current_tab(tab_id) as process:
            if process is None:
                raise KeyError(tab_id)
            if process.archived and process.session_kind != SessionKind.CHAT:
                raise RuntimeError(
                    "tmux session is not running; cannot switch env on a stopped tab"
                )
            await process.switch_env(env, solo_mode=solo_mode)
            self._save_state()
            self._status_cache.pop(tab_id, None)
            return process.to_schema()
