"""Process remote responsibilities on the single terminal owner."""

from __future__ import annotations

import base64
import os
import shlex
import sys
from typing import Optional

from ....config import settings
from ....models import RemoteProfile
from ...remote_profiles import profile_capabilities, remote_profile_manager
from .host import ProcessHost


class ProcessRemoteMixin(ProcessHost):
    def _remote_ssh_target(self) -> tuple[str, int]:
        profile = self._remote_profile()
        host = f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host
        return host, profile.port

    def _remote_profile(self) -> RemoteProfile:
        if not self.remote_profile_id:
            raise ValueError("Remote tab requires remote_profile_id")
        profile = remote_profile_manager.get_profile(self.remote_profile_id)
        if not profile:
            raise ValueError(f"Remote profile not found: {self.remote_profile_id}")
        return profile

    def _remote_is_pty_gateway(self) -> bool:
        return profile_capabilities(self._remote_profile()).is_pty_gateway

    @staticmethod
    def _remote_path_bootstrap() -> str:
        return (
            'if [ -d "$HOME/.nvm/versions/node" ]; then '
            'for dir in "$HOME"/.nvm/versions/node/*/bin; do '
            '[ -d "$dir" ] && PATH="$dir:$PATH"; '
            "done; "
            "export PATH; "
            "fi"
        )

    @staticmethod
    def _remote_shell_command(script: str) -> str:
        return f"exec ${{SHELL:-/bin/bash}} -lc {shlex.quote(script)}"

    def _build_remote_ssh_command(self, remote_command: str) -> list[str]:
        host, port = self._remote_ssh_target()
        cmd = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "LogLevel=ERROR",
        ]
        if port != 22:
            cmd.extend(["-p", str(port)])
        cmd.extend([host, remote_command])
        return cmd

    def _build_remote_attach_script(self, *, bootstrap_file: Optional[str] = None) -> str:
        """The remote shell script that ensures/attaches the detached tmux.

        Shared by the normal argv bootstrap and the PTY-gateway typed bootstrap
        so both transports produce identical remote session semantics.

        On a PTY gateway the launcher passes the secret-bearing temp script's
        own path as ``$1`` (``bootstrap_file``). The script arms an EXIT/HUP trap
        that removes that file and explicitly deletes it as soon as the detached
        tmux pane exists — before ``exec tmux attach`` — so the token-bearing
        file is never on disk for the (potentially whole-session) attach and is
        cleaned up even if the connection is SIGHUP'd mid-bootstrap. For a normal
        argv launch ``$1`` is unset and these are no-ops.
        """

        remote_session = self.tmux_session
        cwd = self.remote_cwd or self.cwd or "~"
        start_command = self._with_env(self._agent_start_command())
        quoted_session = shlex.quote(remote_session)
        quoted_start = shlex.quote(start_command)
        shell = "${SHELL:-/bin/bash}"
        bootstrap_path = self._remote_path_bootstrap()
        checks: list[str] = []

        # Arm temp-file self-cleanup first so a SIGHUP at any point during
        # bootstrap still removes the secret-bearing file. Guarded on $1 being
        # set (gateway typed launch only); the normal argv path leaves it empty.
        cleanup_preamble = (
            '__chp_bootstrap_file="${1:-}"; '
            "__chp_cleanup() { "
            '[ -n "${__chp_bootstrap_file:-}" ] && '
            'rm -f -- "$__chp_bootstrap_file" 2>/dev/null || true; '
            "}; "
            "trap __chp_cleanup EXIT HUP INT TERM"
        )
        # Detached pane now exists and inherited its env at new-session time;
        # the on-disk secret is no longer needed. Runs before exec attach.
        cleanup_before_attach = "__chp_cleanup"

        direct_start_script = "; ".join(
            [
                "printf 'Remote tmux not found in PATH; starting without remote tmux persistence.\\n'",
                start_command,
                "status=$?",
                "printf '\\nRemote agent exited with code %s. Dropping to shell.\\n' \"$status\"",
                cleanup_before_attach,
                f"exec {shell} -l",
            ]
        )

        return "; ".join(
            [
                cleanup_preamble,
                bootstrap_path,
                *self._env_export_commands(),
                *checks,
                self._remote_cwd_command(cwd, shell),
                "if command -v tmux >/dev/null 2>&1; then "
                f"tmux has-session -t {quoted_session} 2>/dev/null || "
                f"tmux new-session -d -s {quoted_session} {quoted_start}; "
                f"tmux set-option -t {quoted_session} status off >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} mouse off >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} focus-events on >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} history-limit 100000 >/dev/null 2>&1 || true; "
                f"tmux set-window-option -t {quoted_session} mode-keys vi >/dev/null 2>&1 || true; "
                f"{cleanup_before_attach}; "
                f"exec tmux attach-session -t {quoted_session}; "
                "fi",
                direct_start_script,
            ]
        )

    def _build_remote_attach_command(self) -> str:
        return self._remote_shell_command(self._build_remote_attach_script())

    @classmethod
    def _remote_cwd_command(cls, cwd: str, shell: str) -> str:
        quoted_cwd = cls._quote_remote_cwd(cwd)
        cwd_arg = shlex.quote(cwd)
        return (
            f"cd {quoted_cwd} || {{ "
            f"printf 'Remote cwd not found: %s; using home directory.\\n' {cwd_arg}; "
            "cd ~ || { "
            "printf 'Remote home directory not available; dropping to shell.\\n'; "
            f"exec {shell} -l; "
            "}; "
            "}"
        )

    @staticmethod
    def _quote_remote_cwd(cwd: str) -> str:
        if cwd == "~":
            return "~"
        if cwd.startswith("~/"):
            return "~/" + shlex.quote(cwd[2:])
        return shlex.quote(cwd)

    def _build_pty_gateway_launcher(self) -> list[str]:
        """Launcher for a PTY-gateway host (argv is swallowed).

        Runs the transparent bridge under the local pane PTY. The bridge holds
        an argv-less ``ssh -tt`` (plus the ``-R`` report forward), waits for the
        real remote prompt, types the base64 bootstrap, then proxies raw bytes —
        and re-runs the whole handshake on every SSH reconnect.
        """

        host, port = self._remote_ssh_target()
        bootstrap_b64 = base64.b64encode(self._build_remote_attach_script().encode("utf-8")).decode(
            "ascii"
        )
        bridge = [
            sys.executable,
            "-m",
            "claude_hub.services.pty_gateway_bridge",
            "--target",
            host,
            "--port",
            str(port),
            "--bootstrap-b64",
            bootstrap_b64,
            "--reconnect" if self.remote_reconnect else "--no-reconnect",
        ]
        if self.remote_forward_port:
            bridge.extend(
                [
                    "--remote-forward",
                    f"{self.remote_forward_port}:{settings.port}",
                ]
            )
        user_shell = os.environ.get("SHELL", "/bin/bash")
        return [user_shell, "-lc", shlex.join(bridge)]

    def _build_remote_launcher(self) -> list[str]:
        if self._remote_is_pty_gateway():
            return self._build_pty_gateway_launcher()
        host, port = self._remote_ssh_target()
        user_shell = os.environ.get("SHELL", "/bin/bash")
        ssh_parts = ["ssh", "-tt"]
        ssh_parts.extend(["-o", "LogLevel=ERROR"])
        if self.remote_forward_port:
            ssh_parts.extend(
                [
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-R",
                    f"127.0.0.1:{self.remote_forward_port}:127.0.0.1:{settings.port}",
                ]
            )
        if port != 22:
            ssh_parts.extend(["-p", str(port)])
        ssh_parts.extend([host, self._build_remote_attach_command()])
        ssh_command = " ".join(shlex.quote(part) for part in ssh_parts)

        if self.remote_reconnect:
            launcher = (
                "while true; do "
                f"{ssh_command}; "
                "status=$?; "
                "printf '\\nRemote connection closed with code %s. "
                'Reconnecting in 3 seconds. Press Ctrl-C to stop.\\n\' "$status"; '
                "sleep 3; "
                "done"
            )
        else:
            launcher = f"exec {ssh_command}"

        return [user_shell, "-lc", launcher]
