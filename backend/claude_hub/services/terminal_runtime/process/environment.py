"""Process environment responsibilities on the single terminal owner."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from ....models import AgentType, ExecutionTarget, SessionKind
from ...provider_proxy import has_provider_proxy_settings, provider_subprocess_env
from ...proxy_rules import PROXY_ENV_KEYS
from ..defaults import _MODEL_ENV_KEYS, _VOLCENGINE_CODING_PLAN_MODEL_ALIASES
from .host import ProcessHost


class ProcessEnvironmentMixin(ProcessHost):
    _TUNNEL_SCRIPT_TEMPLATE = """\
import asyncio, sys

async def _proxy(reader, writer):
    try:
        pr, pw = await asyncio.open_connection({proxy_host!r}, {proxy_port})
        pw.write(f"CONNECT {target_host}:{target_port} HTTP/1.1\\r\\nHost: {target_host}:{target_port}\\r\\n\\r\\n".encode())
        await pw.drain()
        resp = await pr.readuntil(b"\\r\\n\\r\\n")
        if b"200" not in resp.split(b"\\r\\n")[0]:
            writer.close(); pw.close(); pr.close()
            return
        async def pipe(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data: break
                    w.write(data); await w.drain()
            except: pass
        await asyncio.gather(pipe(reader, pw), pipe(pr, writer))
    except: pass
    finally:
        writer.close(); pw.close(); pr.close()

async def _main():
    server = await asyncio.start_server(_proxy, "127.0.0.1", {local_port})
    await server.serve_forever()

asyncio.run(_main())
"""

    def _prepare_agent_env(self) -> None:
        if self.agent_type != AgentType.CLAUDE:
            return
        self._normalize_volcengine_coding_plan_model()
        # Keep the per-tab settings file fresh on every env mutation path
        # (create, restore from tabs.json, switch_env, update_tab). It is the
        # CLI-precedence carrier for this tab's env: both terminal launches
        # and Chat one-shot turns (ClaudeNativeSession._build_command) pass it
        # via ``--settings`` so the tab env beats user-level
        # ``~/.claude/settings.json`` env overrides.
        self._write_launch_settings_file()

    def _normalize_volcengine_coding_plan_model(self) -> None:
        base_url = self.env.get("ANTHROPIC_BASE_URL", "")
        if "ark.cn-beijing.volces.com/api/coding" not in base_url:
            return
        model = self.env.get("ANTHROPIC_MODEL")
        if not model:
            return
        normalized = _VOLCENGINE_CODING_PLAN_MODEL_ALIASES.get(model.strip().lower())
        if not normalized:
            return
        for key in _MODEL_ENV_KEYS:
            current = self.env.get(key)
            if current is None or current.strip().lower() == model.strip().lower():
                self.env[key] = normalized

    def _setup_tunnel_env(self) -> None:
        """If proxy env vars + ANTHROPIC_BASE_URL are set, create a TCP
        tunnel through the CONNECT proxy and rewrite the API URL to point
        at the tunnel.

        This works around Node.js undici/http not reading HTTP_PROXY.
        """
        if not self.env:
            return

        # Find proxy host:port from env (any case variant)
        proxy_url: str | None = None
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            val = self.env.get(key)
            if val:
                proxy_url = val
                break
        if not proxy_url:
            return

        # Parse proxy URL
        m = re.match(r"^(https?://)?([^:/]+)(?::(\d+))?", proxy_url)
        if not m:
            return
        proxy_host = m.group(2)
        proxy_port = int(m.group(3) or (443 if m.group(1) == "https://" else 80))

        # Find target API URL
        api_key = next(
            (k for k in ("ANTHROPIC_BASE_URL", "anthropic_base_url") if k in self.env), None
        )
        if not api_key:
            return
        api_url = self.env[api_key]

        # Parse target host:port from API URL
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?(/.*)?$", api_url)
        if not m:
            return
        target_host = m.group(1)
        target_port = int(m.group(2) or 443)
        target_path = m.group(3) or ""

        # Find a free port for the tunnel
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            local_port = s.getsockname()[1]

        tunnel_key = f"{target_host}:{target_port}->{proxy_host}:{proxy_port}"

        # Reuse existing tunnel
        existing = self._tunnel_registry.get(tunnel_key)
        if existing:
            local_port, proc = existing
            if proc.poll() is None:
                # Still alive — just rewrite env
                self.env[api_key] = f"https://127.0.0.1:{local_port}{target_path}"
                self.env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
                return
            # Dead — remove and recreate
            del self._tunnel_registry[tunnel_key]

        # Start new tunnel
        script = self._TUNNEL_SCRIPT_TEMPLATE.format(
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            target_host=target_host,
            target_port=target_port,
            local_port=local_port,
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._tunnel_registry[tunnel_key] = (local_port, proc)

        # Wait briefly so the tunnel is ready
        time.sleep(0.15)

        # Rewrite env vars
        self.env[api_key] = f"https://127.0.0.1:{local_port}{target_path}"
        self.env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    def _child_env(self) -> Dict[str, str]:
        """Environment handed to the tab's shell / agent process.

        Overlays ``CLAUDE_HUB_TAB_ID`` on top of the user/preset-configured
        ``self.env`` so an agent running in the tab can self-identify and
        target its own tab (e.g. register a ``tab_message`` scheduled task via
        the CLI). Kept out of ``self.env`` itself so it is not persisted as
        user config and survives ``switch_env`` reassignments that rebuild
        ``self.env`` from a fresh payload.
        """
        child = dict(self.env)
        child["CLAUDE_HUB_TAB_ID"] = self.tab_id
        return child

    def _env_shell_prefix(self) -> str:
        env = self._shell_child_env()
        if not env:
            return ""
        assignments = [f"{key}={shlex.quote(value)}" for key, value in env.items()]
        return "env " + " ".join(assignments) + " "

    def _env_export_commands(self) -> list[str]:
        return [
            f"export {key}={shlex.quote(value)}" for key, value in self._shell_child_env().items()
        ]

    def _shell_child_env(self) -> Dict[str, str]:
        """Carry current local proxy settings even through an older tmux server.

        Keep inherited values out of persisted tab/Claude settings, and never
        forward this machine's proxy environment to a remote host.
        """
        child = self._child_env()
        if self.target != ExecutionTarget.LOCAL:
            return child
        inherited = {
            key: value for key, value in os.environ.items() if key.lower() in PROXY_ENV_KEYS
        }
        return provider_subprocess_env(self.agent_type.value, inherited, child)

    def _with_env(self, command: str) -> str:
        if self.target == ExecutionTarget.LOCAL:
            return self._with_local_env_wrapper(command)
        return f"{self._env_shell_prefix()}{command}"

    def _with_local_env_wrapper(self, command: str) -> str:
        self._runtime.LAUNCH_ENV_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        script_path = self._runtime.LAUNCH_ENV_DIR / f"{self.tab_id}.sh"
        child = self._shell_child_env()
        absent_proxy_keys = sorted(
            name for lower in PROXY_ENV_KEYS for name in (lower, lower.upper()) if name not in child
        )
        # A long-lived tmux server may retain a removed proxy or NO_PROXY=*,
        # even when this backend has no proxy settings at all.
        clear_proxies = f"unset {' '.join(absent_proxy_keys)}\n" if absent_proxy_keys else ""
        exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in child.items())
        if has_provider_proxy_settings(self.agent_type.value):
            # Run login initialization first, then source this protected file
            # to reassert deployment/session values in that same shell.
            proxy_exports = "\n".join(
                f"export {key}={shlex.quote(value)}"
                for key, value in child.items()
                if key.lower() in PROXY_ENV_KEYS
            )
            script = (
                "#!/bin/sh\n"
                'if [ "${_CLAUDE_HUB_APPLY_PROXY_ENV:-}" = "1" ]; then\n'
                f"{clear_proxies}{proxy_exports}\nreturn 0\nfi\n"
                f"set -eu\n{clear_proxies}{exports}\n"
                'exec "${SHELL:-/bin/bash}" -lc \'_CLAUDE_HUB_APPLY_PROXY_ENV=1; '
                '. "$1"; unset _CLAUDE_HUB_APPLY_PROXY_ENV; eval "$2"\' '
                f'claude-hub {shlex.quote(str(script_path))} "$1"\n'
            )
        else:
            script = (
                "#!/bin/sh\nset -eu\n"
                f"{clear_proxies}{exports}\n"
                'exec "${SHELL:-/bin/bash}" -lc "$1"\n'
            )
        self._runtime._write_private_launch_file(script_path, script)
        return f"/bin/sh {shlex.quote(str(script_path))} {shlex.quote(command)}"

    def _write_launch_settings_file(self) -> Optional[Path]:
        """Persist the per-tab Claude settings file carrying this tab's env.

        Claude Code applies ``env`` from settings files at higher precedence
        than the process environment, and user-level ``~/.claude/settings.json``
        in turn overrides the inherited process env. Terminal launches pass
        this file via ``--settings`` so the tab env wins; the native Chat
        transport reuses the same file for its one-shot turns. The write is
        guarded like ``_claude_settings_arg`` (Claude + local + env present)
        and chmod 600 because it may contain API tokens.
        """
        if self.agent_type != AgentType.CLAUDE:
            return None
        if self.target != ExecutionTarget.LOCAL:
            return None
        self._runtime.LAUNCH_ENV_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings_path = self._runtime.LAUNCH_ENV_DIR / f"{self.tab_id}.settings.json"
        if not self.env and not has_provider_proxy_settings("claude"):
            settings_path.unlink(missing_ok=True)
            return None
        self._runtime._write_private_launch_file(
            settings_path,
            json.dumps(
                {"env": provider_subprocess_env("claude", {}, self._child_env())},
                ensure_ascii=False,
                indent=2,
            ),
        )
        return settings_path

    def _claude_settings_arg(self) -> str:
        settings_path = self._write_launch_settings_file()
        if settings_path is None:
            return ""
        return f" --settings {shlex.quote(str(settings_path))}"

    def _claude_bypass_acceptance_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE:
            return ""
        if self.session_kind != SessionKind.CHAT or not self.solo_mode:
            return ""
        # Claude 2.1.159 reads this disclaimer acknowledgement from the
        # command-line settings source, but ignores the same internal key in a
        # settings file. Keep the ordinary settings file for launch env
        # propagation and add a second, secret-free inline settings source.
        # Selecting Solo Mode is the user's explicit request for
        # --dangerously-skip-permissions. Never mutate ~/.claude.json, and
        # leave Terminal sessions on Claude's visible native confirmation UI.
        acknowledgement = json.dumps(
            {"bypassPermissionsModeAccepted": True},
            separators=(",", ":"),
        )
        return f" --settings {shlex.quote(acknowledgement)}"

    def _claude_model_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE:
            return ""
        model = self.env.get("ANTHROPIC_MODEL")
        if not model:
            return ""
        return f" --model {shlex.quote(model)}"

    def _codex_model_arg(self) -> str:
        """Return the explicit model selected by a managed-session executor launch contract.

        ``CODEX_MODEL`` is an internal launch-contract variable, not a Codex
        authentication/config variable.  Keeping the value in the persisted
        tab environment lets a cold restart rebuild the same CLI command.
        """

        if self.agent_type != AgentType.CODEX:
            return ""
        model = self.env.get("CODEX_MODEL")
        if not model:
            return ""
        return f" --model {shlex.quote(model)}"
