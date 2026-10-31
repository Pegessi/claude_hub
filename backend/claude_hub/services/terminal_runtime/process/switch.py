"""Process switch responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Dict, Optional

from ....models import AgentType, ExecutionTarget, SessionKind
from .host import ProcessHost


class ProcessSwitchMixin(ProcessHost):
    async def switch_env(self, new_env: Dict[str, str], solo_mode: Optional[bool] = None) -> None:
        """Hot-swap the launch env and/or solo mode of a live local Claude or Codex tab.

        Rewrites the on-disk launch wrapper (and settings.json for Claude), then
        uses ``tmux respawn-pane -k`` to relaunch the agent with resume flags so
        conversation history is preserved. Pane scrollback is preserved; the
        running agent process is killed and replaced.

        For Claude: resumes via ``--resume <agent_session_id>`` (falling back to
        ``--session-id`` so the conversation id is re-pinned).
        For Codex: resumes via ``codex resume <uuid>`` only when the id is
        verified for this cwd, otherwise starts fresh.

        Raises:
            ValueError: if the tab is not a local supported agent tab.
            RuntimeError: if the tmux session is not alive or respawn fails.
        """
        if self.session_kind == SessionKind.CHAT:
            # Chat sessions own their provider via the native transport. Model
            # /env switching for Codex and its TraeX fork is supported (the
            # native transport reads session.env and injects the model via the
            # collaboration-mode channel). Terminal-side tmux respawn (below)
            # is not wired for TraeX, so it is admitted on this path only.
            if self.agent_type not in {
                AgentType.CLAUDE,
                AgentType.CODEX,
                AgentType.CURSOR,
                AgentType.TRAEX,
                AgentType.MASO,
                AgentType.ANTIGRAVITY,
            }:
                raise ValueError("switch_env is not supported for this chat provider")
            if self.agent_type == AgentType.MASO:
                from ...agent_stream.maso_http import maso_base_url

                new_env = dict(new_env)
                if not new_env.get("MASO_BASE_URL"):
                    new_env["MASO_BASE_URL"] = maso_base_url(self.env)
                if maso_base_url(new_env) != maso_base_url(self.env):
                    raise ValueError("Create a new MASO Chat to change MASO_BASE_URL")
            # The native transport reads ``self.session.env`` on every turn
            # (via ``_build_env`` / ``_build_command``), so updating the
            # persisted env here is sufficient for the next turn to pick it up.
            # No tmux respawn is needed — the app-server persists and a
            # one-shot subprocess spawns per turn.
            if solo_mode is not None:
                self.solo_mode = solo_mode
            self.env = self._clean_env(new_env)
            self._prepare_agent_env()
            return
        if self.agent_type not in {AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR}:
            raise ValueError("switch_env is only supported for Claude, Codex, and Cursor tabs")
        if self.target != ExecutionTarget.LOCAL:
            raise ValueError("switch_env is only supported for local tabs")
        if not await self._runtime._tmux_session_exists_async(self.tmux_session):
            raise RuntimeError("tmux session is not running; cannot switch env on a stopped tab")

        # Snapshot current state and on-disk launch files so we can roll back if
        # respawn-pane fails. Without rollback, a tmux-level failure (bad session,
        # tmux crashed, etc.) would leave self.env/self.solo_mode and the
        # <tabid>.sh/.settings.json out of sync with the still-running agent.
        old_env = dict(self.env)
        old_solo_mode = self.solo_mode
        self._runtime.LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        script_path = self._runtime.LAUNCH_ENV_DIR / f"{self.tab_id}.sh"
        settings_path = self._runtime.LAUNCH_ENV_DIR / f"{self.tab_id}.settings.json"
        old_script_bytes = script_path.read_bytes() if script_path.exists() else None
        old_settings_bytes = settings_path.read_bytes() if settings_path.exists() else None

        def _rollback_launch_files() -> None:
            self.env = self._clean_env(old_env)
            self._prepare_agent_env()
            self.solo_mode = old_solo_mode
            if old_script_bytes is not None:
                script_path.write_bytes(old_script_bytes)
            elif script_path.exists():
                script_path.unlink()
            if old_settings_bytes is not None:
                settings_path.write_bytes(old_settings_bytes)
            elif settings_path.exists():
                settings_path.unlink()

        if solo_mode is not None:
            self.solo_mode = solo_mode
        self.env = self._clean_env(new_env)
        self._prepare_agent_env()

        # Build the relaunch command first; _with_local_env_wrapper and
        # _claude_settings_arg write the .sh and .settings.json as a side effect.
        # If those writes raise (e.g. invalid env), state was already mutated
        # above but no tmux operation has happened yet, so the on-disk files are
        # consistent with self.env and next restart/switch will pick them up.
        user_shell = os.environ.get("SHELL", "/bin/bash")

        if self.agent_type == AgentType.CODEX:
            # Codex: resume only via a pinned, cwd-verified session uuid;
            # otherwise start fresh. Solo flags apply to both the exact-resume
            # and fallback branches (delegated to _codex_launch_command).
            inner_cmd = self._codex_launch_command(recover=True)
        else:
            # Claude path
            settings_arg = self._claude_settings_arg()
            bypass_acceptance_arg = self._claude_bypass_acceptance_arg()
            model_arg = self._claude_model_arg()
            session_arg = self._claude_session_arg()

            if self.solo_mode:
                base = "IS_SANDBOX=1 claude --dangerously-skip-permissions"
            else:
                base = self._runtime.get_default_command()

            if self.agent_session_id:
                quoted_sid = shlex.quote(self.agent_session_id)
                resume_inner = (
                    f"{base}{settings_arg}{bypass_acceptance_arg}{model_arg}"
                    f" --resume {quoted_sid}"
                )
                inner_cmd = (
                    f"{resume_inner} || "
                    f"{base}{settings_arg}{bypass_acceptance_arg}{model_arg}{session_arg}"
                )
            else:
                # Defensive: legacy tab without a pinned session id; start fresh.
                inner_cmd = f"{base}{settings_arg}{bypass_acceptance_arg}{model_arg}{session_arg}"

        wrapped = self._with_env(inner_cmd)

        # Append `; exec $SHELL` so the pane stays alive at a shell when the
        # agent exits. This matches how solo/cursor/codex tabs launch initially
        # and gives the user a visible shell prompt if resume fails and the
        # fresh fallback also errors out (otherwise the pane would die silently).
        respawn_cmd = f"{wrapped}; exec {shlex.quote(user_shell)}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._runtime.tmux_command(
                    "respawn-pane",
                    "-k",
                    "-t",
                    self.tmux_session,
                    "--",
                    user_shell,
                    "-lc",
                    respawn_cmd,
                ),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self._runtime._agent_spawn_env(),
            )
            _, stderr = await proc.communicate()
        except Exception:
            _rollback_launch_files()
            raise
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            _rollback_launch_files()
            raise RuntimeError(error or f"tmux respawn-pane failed with code {proc.returncode}")
