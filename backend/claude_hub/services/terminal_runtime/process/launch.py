"""Process launch responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
import uuid
from typing import Optional, Tuple

from ....config import settings
from ....models import AgentType, ExecutionTarget, SessionKind
from ...runtime_isolation import tmux_socket_args
from ..defaults import _CODEX_NEW_PIN_TS_EARLY_S, _CODEX_NEW_PIN_TS_LATE_S
from .host import ProcessHost

logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ProcessLaunchMixin(ProcessHost):
    _TTYD_CLIENT_OPTIONS: tuple[tuple[str, str], ...] = (
        ("scrollback", "100000"),
        ("fastScrollModifier", "alt"),
        ("macOptionIsMeta", "false"),
        ("cursorBlink", "false"),
        ("rendererType", "canvas"),
        ("allowProposedApi", "true"),
        ("drawBoldTextInBrightColors", "false"),
        ("minimumContrastRatio", "1"),
        # Match the native terminal's crisp monospace rendering. Without an
        # explicit fontFamily, xterm.js falls back to a chunky Courier-style
        # font that makes agent TUIs (Cursor/Claude/Codex) look ugly. ttyd
        # parses each -t value as JSON, so this string must itself be
        # double-quoted; inner double quotes around font names with spaces
        # are escaped per JSON rules.
        (
            "fontFamily",
            '"ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \\"Liberation Mono\\", monospace"',
        ),
        ("fontSize", "14"),
        ("lineHeight", "1.2"),
    )

    def _solo_command(self) -> Optional[str]:
        if self.agent_type == AgentType.CODEX:
            return (
                "codex --ask-for-approval never --sandbox danger-full-access"
                f"{self._codex_model_arg()}"
            )
        if self.agent_type == AgentType.TRAEX:
            # TraeX is a Codex fork and its TUI accepts the same bypass flags.
            return self._traex_launch_command()
        if self.agent_type == AgentType.CLAUDE:
            return (
                "IS_SANDBOX=1 claude --dangerously-skip-permissions"
                f"{self._claude_settings_arg()}{self._claude_bypass_acceptance_arg()}"
                f"{self._claude_model_arg()}"
            )
        if self.agent_type == AgentType.CURSOR:
            # Cursor agent runs in yolo by default; solo_mode toggle is a no-op.
            return "agent"
        if self.agent_type == AgentType.ANTIGRAVITY:
            return self._antigravity_launch_command()
        return None

    def _tmux_shell_command(self, session_exists: bool) -> str:
        if self.session_kind == SessionKind.CHAT:
            # Chat sessions are owned by the native ProviderSession; tmux
            # only hosts an inert shell. Never launch the provider CLI here.
            return self._with_env(self.shell)
        if (
            self.solo_mode
            and not session_exists
            and self.agent_type
            in {
                AgentType.CLAUDE,
                AgentType.CODEX,
                AgentType.TRAEX,
                AgentType.ANTIGRAVITY,
            }
        ):
            user_shell = os.environ.get("SHELL", "/bin/bash")
            solo_command = self._solo_command()
            if solo_command:
                return f"{shlex.quote(user_shell)} -c {shlex.quote(f'{self._with_env(solo_command)}; exec {user_shell}')}"
        if self.agent_type == AgentType.CLAUDE and not session_exists:
            return self._with_env(self._agent_start_command())
        # Non-solo agent tabs still need env var injection.
        return self._with_env(self.shell)

    async def ensure_tmux_session(self) -> bool:
        """Create the backing tmux session before ttyd gets a browser client.

        ttyd starts the tmux command lazily when the WebSocket connects. Workspace
        orchestration needs to send prompts before the user opens the tab, so it
        must ensure the tmux session exists independently.

        Stamps ``self._launch_wall`` / ``self._launch_mono`` immediately BEFORE
        spawning tmux so cold-start codex attribution can anchor its new-pin
        timestamp window to the actual moment the agent process starts. This
        is the correct anchor (not start()): ``process.start()`` awaits
        ``asyncio.sleep(1)`` during which the agent already runs inside tmux,
        so stamping there would systematically under-estimate the codex
        session_meta timestamp by ~1 s.
        """
        if self.is_managed_chat:
            return False
        if await self._runtime._tmux_session_exists_async(self.tmux_session):
            return False

        self._runtime._ensure_tmux_server()
        cmd = self._runtime.tmux_command("new-session", "-d", "-s", self.tmux_session)
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])
        cmd.append("--")
        # Session is guaranteed absent here (we returned early if it existed),
        # so recover whenever this tab was restored from persisted state.
        recover = self._should_recover(session_exists=False)
        if self.session_kind == SessionKind.CHAT:
            # Chat sessions are owned exclusively by the native ProviderSession.
            # tmux hosts only an inert lifecycle shell so the tab/ttyd model
            # still has a pane, but the provider CLI is never launched here.
            # Composer input goes to the native transport, never to this pane.
            cmd.append(self._with_env(self.shell))
        elif self.target == ExecutionTarget.REMOTE:
            cmd.append(shlex.join(self._build_remote_launcher()))
        elif self._shell_explicitly_provided:
            # Caller supplied an explicit shell (e.g. tests that need a plain
            # shell for deterministic scrollback). Run it directly instead of
            # the agent CLI.
            cmd.append(self._with_env(self.shell))
        elif self.solo_mode and self.agent_type in {AgentType.CLAUDE, AgentType.CODEX}:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type in {AgentType.CURSOR, AgentType.TRAEX, AgentType.ANTIGRAVITY}:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type == AgentType.CLAUDE:
            cmd.append(self._with_env(self._agent_start_command(recover=recover)))
        elif self.agent_type == AgentType.CODEX:
            # Always build local Codex launches through the persisted CLI
            # contract.  In particular, ``CODEX_MODEL`` is an internal Hub
            # variable and only selects a real Codex model once it is
            # translated to ``--model`` here.  This also keeps fresh and
            # restored non-solo tabs on the same command path.
            cmd.append(self._with_env(self._codex_launch_command(recover=recover)))
        else:
            cmd.append(self._with_env(self.shell))

        # F1 clock anchor: stamped immediately before tmux new-session so the
        # codex new-pin attribution window lines up with when the agent shell
        # actually starts executing. Both clocks captured in the same awaitable
        # tick to minimize drift.
        self._launch_wall = time.time()
        self._launch_mono = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._runtime._agent_spawn_env(),
        )
        try:
            _, stderr = await proc.communicate()
        except asyncio.CancelledError:
            # This launcher belongs to the absent session checked above. Stop
            # and reap it before removing the session, otherwise a late
            # ``tmux new-session`` could recreate the resource after rollback.
            cleanup_task = asyncio.create_task(
                self._runtime._finish_tmux_launcher_rollback(proc, self.tmux_session)
            )
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            await cleanup_task
            raise
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux new-session failed with code {proc.returncode}")
        return True

    async def start(self) -> None:
        """Start the ttyd process with tmux for session persistence."""
        if self.is_managed_chat:
            self.process = None
            self.is_active = False
            return
        if self.process and self.process.returncode is None:
            logger.warning(f"Process for tab {self.tab_id} already running")
            return

        # Check if tmux session already exists
        session_exists = await self._runtime._tmux_session_exists_async(self.tmux_session)
        if session_exists:
            logger.info(f"tmux session {self.tmux_session} exists, will reattach")
        else:
            # Hot reattach does not launch a pane. Refreshing the server's
            # launch environment for every live tab is unnecessary (and
            # performs many synchronous tmux subprocess calls per tab).
            self._runtime._ensure_tmux_server()
            logger.info(f"tmux session {self.tmux_session} does not exist, will create new")

        cmd = self._build_ttyd_command(session_exists=session_exists)
        logger.info(
            "Starting ttyd for tab %s on port %s with %s custom env vars",
            self.tab_id,
            self.port,
            len(self.env),
        )

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._runtime._agent_spawn_env(),
            )
            self.is_active = True
            logger.info(
                f"ttyd started for tab {self.tab_id} with PID {self.process.pid}, tmux session: {self.tmux_session}"
            )

            # Enable tmux mouse mode and other options
            await self._configure_tmux()

            asyncio.create_task(self._log_stderr())

            await asyncio.sleep(1)

            if self.process.returncode is not None:
                stderr = await self.process.stderr.read() if self.process.stderr else b""
                logger.error(
                    f"ttyd exited immediately with code {self.process.returncode}. Stderr: {stderr.decode()}"
                )
                raise Exception(f"ttyd failed to start: {stderr.decode()}")

        except Exception as e:
            logger.error(f"Failed to start ttyd for tab {self.tab_id}: {e}")
            self.is_active = False
            raise

    def _build_ttyd_command(self, session_exists: bool) -> list[str]:
        # tmux new-session -A: attach if exists, create if not.
        # This is the key to persistence across page refreshes.
        cmd: list[str] = [
            settings.ttyd_path,
            "--port",
            str(self.port),
            "--interface",
            "127.0.0.1",
            "--writable",
        ]
        for key, value in self._TTYD_CLIENT_OPTIONS:
            cmd.extend(["-t", f"{key}={value}"])
        cmd.extend(
            [
                "tmux",
                *tmux_socket_args(),
                "new-session",
                "-A",
                "-s",
                self.tmux_session,
            ]
        )
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])

        recover = self._should_recover(session_exists=session_exists)
        if self.session_kind == SessionKind.CHAT:
            # Chat sessions are owned by the native ProviderSession; tmux
            # only hosts an inert shell. Never launch the provider CLI here.
            cmd.append(self._with_env(self.shell))
        elif self.target == ExecutionTarget.REMOTE:
            cmd.extend(self._build_remote_launcher())
        elif self._shell_explicitly_provided:
            # Caller supplied an explicit shell (e.g. tests that need a plain
            # shell for deterministic scrollback). Run it directly instead of
            # the agent CLI.
            cmd.append(self._with_env(self.shell))
        elif (
            self.solo_mode
            and not session_exists
            and self.agent_type
            in {
                AgentType.CLAUDE,
                AgentType.CODEX,
            }
        ):
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                ]
            )
        elif (
            self.agent_type in {AgentType.CURSOR, AgentType.TRAEX, AgentType.ANTIGRAVITY}
            and not session_exists
        ):
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                ]
            )
        elif self.agent_type == AgentType.CLAUDE and not session_exists:
            cmd.append(self._with_env(self._agent_start_command(recover=recover)))
        elif self.agent_type == AgentType.CODEX and not session_exists:
            # A live tmux session is reattached below without starting a
            # second process.  Every actual local Codex launch, fresh or
            # recovered and solo or non-solo, uses the persisted command
            # builder so ``CODEX_MODEL`` becomes a real ``--model`` flag.
            cmd.append(self._with_env(self._codex_launch_command(recover=recover)))
        else:
            cmd.append(self._with_env(self.shell))

        return cmd

    def _claude_session_arg(self) -> str:
        """Fresh-launch flag pinning this tab's stable conversation id."""
        if self.agent_type != AgentType.CLAUDE:
            return ""
        if not self.agent_session_id:
            # Defensive: a persisted tab may have agent_session_id=None if it
            # was created before pinning shipped and backfill was ambiguous.
            # Generate a fresh id here so future restarts can pin/resume it.
            self.agent_session_id = str(uuid.uuid4())
            logger.info(f"generated defensive agent_session_id for claude tab {self.tab_id}")
        return f" --session-id {shlex.quote(self.agent_session_id)}"

    def _claude_command(self, session_flag: str) -> str:
        if self.solo_mode:
            base = "IS_SANDBOX=1 claude --dangerously-skip-permissions"
        else:
            base = self._runtime.get_default_command()
        return (
            f"{base}{self._claude_settings_arg()}{self._claude_bypass_acceptance_arg()}"
            f"{self._claude_model_arg()}{session_flag}"
        )

    def _should_recover(self, session_exists: bool) -> bool:
        """Recover (resume prior conversation) when:
        1. Relaunching a persisted tab whose tmux session is gone (machine
           reboot), OR
        2. A fresh tab was created with an explicit ``agent_session_id`` to
           resume (e.g. the user selected a specific Codex session in the
           create-tab UI).

        A live tmux session (backend-only restart) reattaches and must not
        resume. Scoped to local agents that expose resume flags.
        """
        if session_exists:
            return False
        if self.target != ExecutionTarget.LOCAL:
            return False
        if self.agent_type not in {AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR}:
            return False
        if self.from_persisted_state:
            return True
        # Fresh tab created with an explicit session id to resume. The
        # launch command will use codex resume <id> / claude --resume <id>.
        # A generated uuid4 placeholder (no explicit id) is for pinning only
        # and must not trigger recovery of a (non-existent) session.
        if self._has_explicit_session_id:
            return True
        return False

    def _discover_codex_session_id(self, launch_epoch: float) -> Optional[str]:
        """Legacy single-tab codex discovery (used by create_tab /
        ensure_tab_tmux_session, which launch ONE codex at a time outside the
        bulk startup path and so don't need the global lock / Phase-R
        reconciliation).

        After a fresh codex launch (single-tab path), we wait ~5s for codex
        to flush session_meta, then scan for a new-pin rollout in this cwd
        whose session_meta.ts falls within [-2s, +8s] of ``launch_epoch``.
        Returns the discovered sid or None if ambiguous/missing.
        """
        if self.agent_type != AgentType.CODEX or not self.cwd:
            return None
        try:
            target_cwd = os.path.realpath(self.cwd)
        except OSError:
            target_cwd = self.cwd
        best: Optional[Tuple[float, str]] = None
        for sid, entry in self._runtime._codex_scan_sessions().items():
            if not entry.cwd or entry.ts is None:
                continue
            try:
                if os.path.realpath(entry.cwd) != target_cwd:
                    continue
            except OSError:
                if entry.cwd != self.cwd:
                    continue
            delta = entry.ts - launch_epoch
            if delta < -_CODEX_NEW_PIN_TS_EARLY_S:
                continue
            if delta > _CODEX_NEW_PIN_TS_LATE_S:
                continue
            if best is None or abs(delta) < abs(best[0]):
                best = (delta, sid)
        if best is None:
            return None
        logger.info(
            "discovered codex session for tab %s -> %s (delta %.1fs from launch)",
            self.tab_id,
            best[1],
            best[0],
        )
        return best[1]

    def _codex_launch_command(self, recover: bool) -> str:
        """Build the codex launch command, preserving solo flags on both branches.

        When ``recover`` is True AND ``self.agent_session_id`` is a VERIFIED
        codex session (rollout exists, active or archived) AND this tab is
        NOT quarantined, recovery targets EXACTLY that session via
        ``codex resume <uuid>`` with a fresh fallback (``|| codex``) so that
        if resume fails for any reason codex starts fresh rather than
        silently attaching to a different session (BUG-3: the old
        ``resume --last`` path was cwd-scoped and cross-wired same-cwd tabs).

        When quarantine is set OR no verified id exists, start fresh — the
        Phase 1C attribution logic will pin the newly-created rollout after
        the fence, and next restart will resume exactly that conversation.

        When ``recover`` is False (fresh tab create, ttyd-attached hot
        restart), start fresh with no resume.

        The solo flags (``--ask-for-approval never --sandbox danger-full-access``)
        MUST be applied to BOTH the ``resume`` branch and the fresh fallback:
        ``codex resume`` accepts them, and omitting them on resume silently
        drops solo mode whenever the resume succeeds.
        """
        flags = " --ask-for-approval never --sandbox danger-full-access" if self.solo_mode else ""
        model_arg = self._codex_model_arg()
        fresh = f"codex{flags}{model_arg}"
        if (
            recover
            and not self.resume_quarantined
            and self.agent_session_id
            # cwd-scoped verification (BUG-3 class): a sid that exists on disk
            # but belongs to a DIFFERENT cwd must NOT be resumed here — that
            # would attach this tab to another workspace's conversation.
            and self._runtime._codex_id_exists(self.agent_session_id, self.cwd)
        ):
            quoted_sid = shlex.quote(self.agent_session_id)
            return f"codex resume {quoted_sid}{flags}{model_arg} || {fresh}"
        # Quarantined / unverified: start fresh; Phase 1C will pin the new sid.
        # DO NOT fall back to `codex resume --last` — that was BUG-3 (cwd-scoped
        # cross-wiring across same-cwd tabs).
        return fresh

    def _traex_launch_command(self) -> str:
        """Build the TraeX TUI launch command (fresh every time).

        TraeX is a Codex fork and accepts the same bypass flags for solo mode.
        Unlike Codex/Claude we do not wire terminal ``resume`` or rollout
        session discovery here: the structured Chat surface persists its
        conversation through the app-server's ``thread/resume``, and a terminal
        tab simply starts a fresh interactive TUI.
        """
        flags = " --ask-for-approval never --sandbox danger-full-access" if self.solo_mode else ""
        return f"traex{flags}"

    def _antigravity_launch_command(self) -> str:
        """Build the Antigravity CLI TUI launch command (fresh every time).

        Conversation persistence in the terminal tab is handled by agy's own
        ``--conversation`` resume on the Chat (native transport) surface; a
        terminal tab simply starts the interactive TUI. The ``agyx`` wrapper
        supplies the local proxy/TLS environment the Google sign-in needs.
        """
        flags = " --dangerously-skip-permissions" if self.solo_mode else ""
        return f"agyx{flags}"

    def _agent_start_command(self, recover: bool = False) -> str:
        if self.agent_type == AgentType.ANTIGRAVITY:
            return self._antigravity_launch_command()
        if self.agent_type == AgentType.TRAEX:
            return self._traex_launch_command()
        if self.agent_type == AgentType.CODEX:
            return self._codex_launch_command(recover=recover)
        if self.agent_type == AgentType.CURSOR:
            if self.cursor_transport == "terminal_transcript":
                # One authoritative process owns both the raw Terminal view
                # and the transcript tailed for Chat. Provenance is validated
                # at construction/load, so never rotate or fall back here.
                assert self.agent_session_id is not None
                flags = " --yolo" if self.solo_mode else ""
                return f"agent --resume {shlex.quote(self.agent_session_id)}{flags}"
            # Cursor CLI supports constructive pinning via `agent --resume <uuid>`:
            # V0 verified that passing an arbitrary uuid causes Cursor to create
            # a fresh store.db immediately (rather than erroring out). A persisted
            # sid is resumed only when its store verifies against this cwd. If it
            # is missing or belongs to another cwd, rotate to a fresh uuid rather
            # than asking Cursor to resolve an unsafe cross-workspace id.
            if not self.agent_session_id:
                self.agent_session_id = str(uuid.uuid4())
            flags = " --yolo" if self.solo_mode else ""
            if (
                recover
                and not self.resume_quarantined
                and self._runtime._cursor_id_exists(self.agent_session_id, self.cwd or "")
            ):
                quoted_sid = shlex.quote(self.agent_session_id)
                return f"agent --resume {quoted_sid}{flags} || agent{flags}"
            if recover:
                self.agent_session_id = str(uuid.uuid4())
                self.resume_quarantined = False
            quoted_sid = shlex.quote(self.agent_session_id)
            return f"agent --resume {quoted_sid}{flags}"
        if self.agent_type == AgentType.TERMINAL:
            return "${SHELL:-/bin/bash} -l"
        # claude: pin a stable --session-id on first launch so a reboot can
        # resume EXACTLY this tab's conversation via --resume. If resume fails
        # (no such session yet, e.g. a legacy tab), fall back to a fresh start
        # that re-pins the same id for next time.
        if recover and self.agent_session_id:
            resume = self._claude_command(f" --resume {shlex.quote(self.agent_session_id)}")
            return f"{resume} || {self._claude_command(self._claude_session_arg())}"
        return self._claude_command(self._claude_session_arg())

    async def stop(self, kill_tmux: bool = False) -> None:
        """Stop the ttyd process. By default, KEEP tmux session alive for persistence.

        Args:
            kill_tmux: If True, also kill the tmux session. Only use this when
                      deleting a tab or rolling back an unpersisted tab startup.
        """
        if self.is_managed_chat:
            # Managed Chat tabs deliberately own no ttyd/tmux lifecycle. Their
            # native provider runtime is stopped by the agent-stream layer.
            self.process = None
            self.is_active = False
            return
        if self.process and self.process.returncode is None:
            logger.info(f"Stopping ttyd for tab {self.tab_id} (PID {self.process.pid})")
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"ttyd for tab {self.tab_id} didn't terminate, killing it")
                self.process.kill()
                await self.process.wait()

        if kill_tmux:
            logger.warning(f"Killing tmux session {self.tmux_session}")
            await self._runtime._tmux_kill_session(self.tmux_session)
        else:
            logger.info(f"Preserving tmux session {self.tmux_session} for future reattachment")

        self.is_active = False
