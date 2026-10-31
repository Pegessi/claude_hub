"""Manager create responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Dict, List, Optional

from ....config import settings
from ....models import (
    AgentStreamEvent,
    AgentType,
    ChatMode,
    ExecutionTarget,
    SessionKind,
    TerminalTab,
    WorkspaceSessionRole,
)
from ....models.schemas import CHAT_REMOTE_UNSUPPORTED
from ....models.subagent import SUBAGENT_ID_ENV
from ...remote_profiles import reject_unsupported_interactive, remote_profile_manager
from ..defaults import _MAX_TCP_PORT, _MAX_TTYD_BIND_ATTEMPTS
from ..records import TabLimitExceededError, TabStartupTimeoutError
from .host import ManagerHost


def _copied_launch_env(env: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Launch env for a duplicate/fork: an ordinary Chat, never the source's subagent."""
    if env is None:
        return None
    return {key: value for key, value in env.items() if key != SUBAGENT_ID_ENV}


if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerCreateMixin(ManagerHost):
    def _get_next_port(self) -> int:
        while self._next_port <= _MAX_TCP_PORT:
            port = self._next_port
            self._next_port += 1
            if self._runtime._is_local_port_available(port):
                return port
            logger.warning("Skipping occupied ttyd port %s", port)
        raise RuntimeError("No available ttyd ports remain")

    @staticmethod
    async def _finish_startup_cleanup(process: TTYDProcess, *, kill_tmux: bool) -> None:
        """Finish rollback even if shutdown delivers repeated cancellation."""
        cleanup_task = asyncio.create_task(process.stop(kill_tmux=kill_tmux))
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        await cleanup_task

    async def create_tab(
        self,
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: Optional[WorkspaceSessionRole] = None,
        managed_session_id: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        agent_session_id: Optional[str] = None,
        cursor_transport: str = "terminal",
        cursor_data_dir: Optional[str] = None,
        cursor_cli_version: Optional[str] = None,
        cursor_transcript_path: Optional[str] = None,
        cursor_transcript_schema: Optional[str] = None,
        session_kind: SessionKind = SessionKind.TERMINAL,
        chat_mode: ChatMode = ChatMode.DEFAULT,
        parent_tab_id: Optional[str] = None,
        subagent_id: Optional[str] = None,
    ) -> TerminalTab:
        # Global backstop: bound the total number of live tabs so even
        # non-fork creation cannot grow without limit. The per-source fork
        # cap is enforced in fork_tab; this catches everything else.
        self._check_live_tab_capacity()
        self._require_valid_state()
        logger.info(
            f"create_tab called with: name={name}, solo_mode={solo_mode}, shell={shell}, cwd={cwd}, agent_type={agent_type}, session_kind={session_kind}, target={target}, remote_profile_id={remote_profile_id}, remote_forward_port={remote_forward_port}, workspace_id={workspace_id}, workspace_role={workspace_role}, agent_session_id={agent_session_id}"
        )
        if workspace_role is not None and chat_mode != ChatMode.DEFAULT:
            raise ValueError("managed workspace Chat mode must use default")
        # Local import: this module loads during ttyd_manager initialization,
        # before the agent_stream package has finished importing.
        from ...agent_stream.providers.spec import Surface, providers_for_surface

        if (
            workspace_role is not None
            and session_kind == SessionKind.CHAT
            and agent_type not in providers_for_surface(Surface.MANAGED_CHAT)
        ):
            raise ValueError(
                "Managed Chat supports only local Claude, Codex, Cursor, MASO, or Antigravity agents"
            )
        if agent_type == AgentType.MASO and session_kind != SessionKind.CHAT:
            raise ValueError("MASO is an HTTP Chat provider; select a Chat session")
        if (
            workspace_role is not None
            and agent_type == AgentType.ANTIGRAVITY
            and session_kind != SessionKind.CHAT
        ):
            raise ValueError("Antigravity is a Chat provider in workspaces; select a Chat session")
        if agent_type == AgentType.MASO:
            from ...maso_discovery import discover_maso_service

            if chat_mode != ChatMode.DEFAULT:
                raise ValueError("MASO Chat only supports default mode")
            # A persisted opaque session belongs to one service, even when the
            # backend's global default changes before a later restart.
            env = dict(env or {})
            if workspace_role is not None:
                from ...agent_stream.maso_http import maso_base_url

                if not self._runtime._is_loopback_url(maso_base_url(env)):
                    raise ValueError("Managed MASO Chat requires a loopback-local MASO service")
            base_url, _models, _default = await discover_maso_service(env)
            if workspace_role is not None and not self._runtime._is_loopback_url(base_url):
                raise ValueError("Managed MASO Chat requires a loopback-local MASO service")
            env["MASO_BASE_URL"] = base_url
        if session_kind == SessionKind.CHAT and target == ExecutionTarget.REMOTE:
            raise ValueError(CHAT_REMOTE_UNSUPPORTED)
        if target == ExecutionTarget.REMOTE:
            reject_unsupported_interactive(
                remote_profile_manager.get_profile(remote_profile_id or "")
            )
        # session_kind is authoritative: only an explicit SessionKind.CHAT
        # from the caller (direct user Chat tab) gets the native/inert
        # structured surface. Managed task runners (dispatcher, reviewer,
        # worker) created by the workspace orchestrator pass TERMINAL so they
        # keep raw TUI control-plane semantics. We never auto-promote a
        # TERMINAL tab to CHAT based on workspace_id or agent_type.
        cursor_transport = self._runtime._promote_cursor_chat_transport(
            session_kind, agent_type, target, cursor_transport
        )
        for _ in range(16):
            tab_id = str(uuid.uuid4())
            if tab_id not in self.processes and tab_id not in self._pending_stream_cleanup:
                break
        else:
            raise RuntimeError("could not allocate an unreserved tab identity")
        port = self._get_next_port()

        process = self._runtime.TTYDProcess(
            tab_id,
            port,
            name,
            shell,
            cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            session_kind=session_kind,
            chat_mode=chat_mode,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            remote_forward_port=remote_forward_port,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
            managed_session_id=managed_session_id,
            env=env,
            agent_session_id=agent_session_id,
            cursor_transport=cursor_transport,
            cursor_data_dir=cursor_data_dir,
            cursor_cli_version=cursor_cli_version,
            cursor_transcript_path=cursor_transcript_path,
            cursor_transcript_schema=cursor_transcript_schema,
            parent_tab_id=parent_tab_id,
            subagent_id=subagent_id,
        )
        logger.info(
            f"Created TTYDProcess with solo_mode={process.solo_mode}, agent_type={process.agent_type}"
        )
        if process.is_managed_chat:
            # Managed Chat keeps the ordinary tab/session navigation identity,
            # but its native ProviderSession is created lazily by the Chat
            # runtime. Never create a ttyd listener, tmux seat, shell, or TUI.
            self.processes[tab_id] = process
            self._ensure_tab_in_order(tab_id)
            self._save_state()
            return process.to_schema()
        tmux_created = False
        process_stopped_without_tmux = False
        startup_deadline = (
            asyncio.get_running_loop().time() + self._runtime._TAB_CREATE_STARTUP_TIMEOUT_SECONDS
        )

        def remaining_startup_time() -> float:
            return max(0.0, startup_deadline - asyncio.get_running_loop().time())

        try:
            # Stamp launch clocks before starting (ttyd lazy-executes: start()
            # awaits ~1 s during which the agent is already running in tmux).
            # For hot reattaches this is harmless (ensure_tmux_session is a
            # no-op when the session already exists); for cold creates we need
            # the anchor set before start() spawns the new tmux session.
            # Keep the ownership result reliable if cancellation arrives while
            # tmux is being created. Rollback must never kill a session that
            # predated this create request.
            ensure_task = asyncio.create_task(process.ensure_tmux_session())
            ensure_cancel: Optional[asyncio.CancelledError] = None
            while not ensure_task.done():
                try:
                    tmux_created = await asyncio.wait_for(
                        asyncio.shield(ensure_task), timeout=remaining_startup_time()
                    )
                except asyncio.CancelledError as exc:
                    ensure_cancel = ensure_cancel or exc
                except asyncio.TimeoutError as exc:
                    ensure_task.cancel()
                    try:
                        await ensure_task
                    except asyncio.CancelledError:
                        # ensure_tmux_session terminated and reaped its launcher,
                        # then removed only the session it proved absent before
                        # launch. No tmux ownership is inferred here.
                        pass
                    except Exception:
                        # The ensure task could not finish its own rollback.
                        # Retry the uniquely named session cleanup below while
                        # preserving the startup timeout as the caller contract.
                        tmux_created = True
                        logger.exception("Failed to roll back timed out tmux ensure for %s", tab_id)
                    if ensure_cancel is not None:
                        raise ensure_cancel
                    raise TabStartupTimeoutError(
                        f"tab startup timed out after {self._runtime._TAB_CREATE_STARTUP_TIMEOUT_SECONDS:g}s"
                    ) from exc
            if ensure_task.done():
                tmux_created = ensure_task.result()
            if ensure_cancel is not None:
                raise ensure_cancel

            for attempt in range(1, _MAX_TTYD_BIND_ATTEMPTS + 1):
                try:
                    await asyncio.wait_for(process.start(), timeout=remaining_startup_time())
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    raise TabStartupTimeoutError(
                        f"tab startup timed out after {self._runtime._TAB_CREATE_STARTUP_TIMEOUT_SECONDS:g}s"
                    ) from exc
                except Exception:
                    # A port can be claimed after allocation but before ttyd
                    # binds. Stop the failed attempt, then retry only when the
                    # candidate is demonstrably occupied by another process.
                    await process.stop(kill_tmux=False)
                    process_stopped_without_tmux = True
                    if (
                        attempt >= _MAX_TTYD_BIND_ATTEMPTS
                        or self._runtime._is_local_port_available(process.port)
                    ):
                        raise
                    old_port = process.port
                    process.port = self._get_next_port()
                    logger.warning(
                        "Retrying ttyd startup for tab %s after port %s was claimed; using %s",
                        tab_id,
                        old_port,
                        process.port,
                    )
        except asyncio.CancelledError:
            # A reload can cancel creation after ttyd has bound its port but
            # before the tab is persisted. Stop that untracked process so the
            # next create does not collide with an orphan listener.
            try:
                await self._finish_startup_cleanup(process, kill_tmux=tmux_created)
            except Exception:
                logger.exception("Failed to clean up cancelled tab %s", tab_id)
            raise
        except Exception:
            try:
                if tmux_created or not process_stopped_without_tmux:
                    await self._finish_startup_cleanup(process, kill_tmux=tmux_created)
            except Exception:
                logger.exception("Failed to clean up unsuccessful tab %s", tab_id)
            raise

        self.processes[tab_id] = process
        self._ensure_tab_in_order(tab_id)
        self._save_state()

        # Schedule post-start codex session-id discovery so newly created
        # codex tabs get a pinned UUID before the next restart.
        self._schedule_codex_discovery(process)

        return process.to_schema()

    @asynccontextmanager
    async def _locked_current_tab(self, tab_id: str) -> AsyncIterator[Optional[TTYDProcess]]:
        # Missing ids must not grow the lock map; queued callers must re-read the owner.
        if tab_id not in self.processes:
            yield None
            return
        lock = self._start_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            yield self.processes.get(tab_id)

    async def ensure_tab_tmux_session(self, tab_id: str) -> bool:
        async with self._locked_current_tab(tab_id) as process:
            if process is None:
                raise KeyError(tab_id)
            if process.archived:
                return False
            created = await process.ensure_tmux_session()
            if created:
                # Fresh Codex launches need discovery; later ttyd startup only reattaches.
                self._schedule_codex_discovery(process)
            return created

    async def duplicate_tab(self, tab_id: str) -> Optional[TerminalTab]:
        """Create a new tab by copying the source tab's launch configuration."""
        source = self.processes.get(tab_id)
        if not source:
            return None

        return await self.create_tab(
            name=f"{source.name} (copy)",
            shell=source.shell,
            cwd=source.cwd,
            solo_mode=source.solo_mode,
            agent_type=source.agent_type,
            session_kind=source.session_kind,
            chat_mode=source.chat_mode,
            target=source.target,
            remote_profile_id=source.remote_profile_id,
            remote_cwd=source.remote_cwd,
            remote_reconnect=source.remote_reconnect,
            remote_forward_port=source.remote_forward_port,
            env=_copied_launch_env(source.env),
        )

    def _check_live_tab_capacity(self) -> None:
        """Reject a new live tab once ``settings.max_total_tabs`` are live.

        Archived tabs have released their ttyd port, process and tmux session,
        so they do not count toward the limit.
        """
        live = sum(1 for proc in self.processes.values() if not proc.archived)
        if live >= settings.max_total_tabs:
            raise TabLimitExceededError(
                f"live tab limit reached ({settings.max_total_tabs}); "
                "archive or delete a session to create more"
            )

    def _count_forks(self, source_tab_id: str) -> int:
        """Count existing tabs forked from ``source_tab_id``."""
        return sum(
            1 for proc in self.processes.values() if proc.forked_from_tab_id == source_tab_id
        )

    def _fork_lock(self, source_tab_id: str) -> asyncio.Lock:
        """Per-source-tab lock serializing the cap check and tab creation.

        Lazily initialized so managers built via ``TTYDManager.__new__`` (which
        bypasses ``__init__``) still work. Using ``self.__dict__`` avoids
        relying on an attribute that ``__new__``-built instances may not set.
        """
        locks = self.__dict__.get("_fork_locks")
        if locks is None:
            locks = {}
            self._fork_locks = locks
        lock = locks.get(source_tab_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[source_tab_id] = lock
        return lock

    async def fork_tab(self, tab_id: str, ordinal: int) -> Optional[TerminalTab]:
        """Create a new tab by deep-copying the source tab's structured history
        up to and including turn ``ordinal`` (0-based).

        The forked tab carries the source's launch configuration but a fresh
        provider conversation; the structured pane shows the copied history so
        the user can continue independently from that point. ``forked_from``
        provenance is recorded on the forked tab for traceability.

        Raises ``TabLimitExceededError`` if the source already has
        ``settings.max_forks_per_tab`` forks, and ``ValueError`` if the
        ordinal is out of range.
        """
        source = self.processes.get(tab_id)
        if not source:
            return None

        # Deferred import: ``agent_stream`` pulls in the native tailer, which
        # imports this module; importing at call time avoids a circular import.
        from ...agent_stream.store import AgentStreamStore

        # Bound fork growth: a fork loop must not exhaust ttyd ports,
        # processes, and tmux sessions. Hold the per-source lock across the
        # cap check and creation so concurrent forks cannot both pass the
        # check and overshoot the cap.
        async with self._fork_lock(tab_id):
            fork_count = self._count_forks(tab_id)
            if fork_count >= settings.max_forks_per_tab:
                raise TabLimitExceededError(
                    f"tab {tab_id} already has {fork_count} forks; limit is "
                    f"{settings.max_forks_per_tab}"
                )

            # Read the source stream events, paging through large histories.
            source_store = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
            events: List[AgentStreamEvent] = []
            since = -1
            while True:
                page = await source_store.read_since(since, limit=5000)
                events.extend(page.events)
                if not page.has_more:
                    break
                since = page.next_sequence

            # Assign each event a turn ordinal by first appearance of its
            # turn_id, then take the prefix of turns 0..ordinal. This is
            # correct even when turns interleave (turn_ids [A,B,A] ->
            # ordinals [0,1,0]; fork at ordinal 1 keeps all three events).
            ordinals = self._runtime._event_turn_ordinals(events)
            num_turns = (max(ordinals) + 1) if ordinals else 0
            if ordinal < 0 or ordinal >= num_turns:
                raise ValueError(
                    f"ordinal {ordinal} out of range (conversation has " f"{num_turns} turns)"
                )
            cutoff = [ev for ev, o in zip(events, ordinals) if o <= ordinal]

            # Create the forked tab with the source's launch configuration.
            forked = await self.create_tab(
                name=f"{source.name} (fork)",
                shell=source.shell,
                cwd=source.cwd,
                solo_mode=source.solo_mode,
                agent_type=source.agent_type,
                session_kind=source.session_kind,
                chat_mode=source.chat_mode,
                target=source.target,
                remote_profile_id=source.remote_profile_id,
                remote_cwd=source.remote_cwd,
                remote_reconnect=source.remote_reconnect,
                remote_forward_port=source.remote_forward_port,
                env=_copied_launch_env(source.env),
                # Copy the Cursor launch mode so a forked Cursor tab streams the
                # same way, but omit the transcript path/schema: those identify
                # the source conversation, and the fork starts a fresh one.
                cursor_transport=source.cursor_transport,
                cursor_data_dir=source.cursor_data_dir,
                cursor_cli_version=source.cursor_cli_version,
            )

        # Rewrite the copied events to the forked tab's identity and persist
        # them. ``replace_all`` re-sequences from 0 so the forked stream is a
        # clean append-only log starting at the copied history. If anything
        # after create_tab fails, delete the just-created tab so a failed
        # fork leaves no empty orphan "X (fork)" tab behind.
        try:
            forked_store = AgentStreamStore("terminal-tabs", f"terminal-tab-{forked.id}")
            rewritten = [
                ev.model_copy(
                    update={
                        "session_id": f"terminal-tab-{forked.id}",
                        "tab_id": forked.id,
                    }
                )
                for ev in cutoff
            ]
            await forked_store.replace_all(rewritten)

            # Seed the new provider conversation with the copied Q/A text. The
            # forked tab starts a fresh, zero-history native provider session;
            # without this the model cannot resolve references to the visible
            # copied history. The body is handed to the first provider turn via
            # a sidecar the tailer loads when it builds the transport (see
            # agent_stream/fork_seed.py). Text-only; a prefix with no text (e.g.
            # image-only turns) seeds nothing and behaves like a UI-only fork.
            from ...agent_stream.fork_seed import build_seed_body, write_seed_sidecar

            seed_body = build_seed_body(rewritten)
            if seed_body is not None:
                await asyncio.to_thread(
                    write_seed_sidecar,
                    "terminal-tabs",
                    f"terminal-tab-{forked.id}",
                    seed_body,
                )

            # Record fork provenance on the forked tab.
            process = self.processes[forked.id]
            process.forked_from_tab_id = tab_id
            process.forked_from_ordinal = ordinal
            self._save_state()
            return process.to_schema()
        except Exception:
            try:
                await self.delete_tab(forked.id)
            except Exception:
                logger.exception("Failed to clean up orphan fork tab %s after error", forked.id)
            raise
