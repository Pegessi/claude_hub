import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, TypedDict

from ..config import settings
from ..models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    TerminalAgentStatus,
    TerminalTab,
    WorkspaceSessionRole,
)
from .remote_profiles import remote_profile_manager

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".claude_hub" / "tabs.json"
ORDER_FILE = Path.home() / ".claude_hub" / "tab_order.json"
LAUNCH_ENV_DIR = Path.home() / ".claude_hub" / "launch_env"
TMUX_SESSION_PREFIX = "claude-hub-"

# ANSI escape sequences (CSI, OSC, charset selection) — stripped before
# pattern matching so cursor blinks and color codes don't churn the hash
# or break substring checks.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")

# Bare shell prompt (zsh/bash/fish/powerlevel) at end of last line → idle.
_BARE_SHELL_PROMPT_RE = re.compile(r"[❯>$#%»→λ]\s*$")

# Strict tail anchors used for classification. Each tuple is matched against
# the lowercased last 5 non-empty lines of the captured pane. Order of checks
# in `_classify_agent_status` is: ATTENTION → WORKING → IDLE hints.
_ATTENTION_TAIL_PATTERNS = (
    "do you want to proceed",
    "do you want to continue",
    "enter to select",
    "tab/arrow keys to navigate",
    "arrow keys to navigate",
    "type something.",
    "esc to cancel",
    "(y/n)",
    "(y/n/a)",
    "[y/n]",
    "[y/n/a]",
    "press enter to continue",
    "press any key to continue",
)

_WORKING_TAIL_PATTERNS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "ctrl-c to interrupt",
    # Cursor CLI shows "ctrl+c to stop" while the agent is actively running.
    "ctrl+c to stop",
    "ctrl-c to stop",
    "running…",
    "running...",
)

# Claude Code also reports active work with spinner-style status lines such
# as "✢ Gusting… (52s · ↑ 1.5k tokens · thought for 2s)".
_CLAUDE_WORKING_STATUS_RE = re.compile(r"^[✻✢✶✳✷✸✹✺✽✦✧]\s+\S+…\s+\(", re.MULTILINE)

# Cursor CLI reports active work with a status line like "Running 662 tokens"
# (no ellipsis, capitalized). Match it as a strict regex to avoid false hits
# from arbitrary "running" mentions in command output.
_CURSOR_WORKING_STATUS_RE = re.compile(r"\brunning\s+\d[\d,\.]*\s+tokens?\b", re.IGNORECASE)

# Bottom-of-UI hints emitted by Claude Code / Codex when idle and waiting for
# user input. Presence of any of these means the agent UI is showing but no
# work is in flight.
_IDLE_TAIL_HINTS = (
    "? for shortcuts",
    "/ for commands",
)

_STATUS_CACHE_TTL_SECONDS = 0.75
_PORT_CHECK_TIMEOUT_SECONDS = 0.2
_REMOTE_CAPTURE_TIMEOUT_SECONDS = 10.0
_VOLCENGINE_CODING_PLAN_MODEL_ALIASES = {
    "ark/seed-code-0602": "doubao-seed-2.0-code",
    "ark/seed-code-0602[1m]": "doubao-seed-2.0-code",
    "ark/seed-code-6062": "doubao-seed-2.0-code",
    "ark/seed-code-6062[1m]": "doubao-seed-2.0-code",
}
_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
DEFAULT_CLAUDE_LAUNCH_ENV: Dict[str, str] = {
    "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
    "ANTHROPIC_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "doubao-seed-2.0-code",
    "CLAUDE_CODE_SUBAGENT_MODEL": "doubao-seed-2.0-code",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class CursorPosition(TypedDict):
    cursor_x: int
    cursor_y: int


class _AgentStatusSnapshot(TypedDict):
    hash: str
    last_changed_at: Optional[datetime]


def _is_local_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT_SECONDS)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _tmux_session_name(tab_id: str) -> str:
    return f"{TMUX_SESSION_PREFIX}{tab_id[:8]}"


def _tmux_server_running() -> bool:
    """Check if tmux server is running."""
    ret = os.system("tmux ls 2>/dev/null")
    return ret == 0 or ret == 256  # 0 = has sessions, 256 = no sessions but server running


def _ensure_tmux_server() -> bool:
    """Ensure tmux server is running, start it if not."""
    if _tmux_server_running():
        logger.debug("tmux server is already running")
        return True
    try:
        # Start a dummy session to initialize tmux server, then detach
        # This ensures the tmux server stays running even with no sessions
        logger.info("tmux server not running, starting it...")
        ret = os.system("tmux new-session -d -s __tmux_server_keepalive__ 2>/dev/null")
        if ret == 0:
            logger.info("tmux server started successfully with keepalive session")
        return True
    except Exception as e:
        logger.warning(f"Failed to start tmux server: {e}")
        return False


def _tmux_session_exists(session_name: str) -> bool:
    """Check if a tmux session exists (sync, used during startup)."""
    ret = os.system(f"tmux has-session -t {session_name} 2>/dev/null")
    return ret == 0


async def _tmux_kill_session(session_name: str) -> None:
    """Kill a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _agent_spawn_env() -> dict:
    """Environment for ttyd/tmux-spawned agent panes.

    Agent TUIs (Cursor/Claude/Codex) gate their styled rendering on color
    support. Two things flatten them into a colorless, low-contrast UI here:

    1. Inside tmux the inner TERM is ``tmux-256color`` with ``COLORTERM`` unset.
    2. When the backend is launched from a parent process that disables color
       (e.g. another agent CLI exports ``NO_COLOR=1`` / ``FORCE_COLOR=0``),
       those flags are inherited all the way down to the agent panes and
       suppress every escape sequence — so the input prompt, dimmed
       placeholders and accent colors all collapse to one flat shade.

    Normalize the environment so panes always render with their full palette,
    regardless of how the backend itself was started.
    """
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env["COLORTERM"] = "truecolor"
    env["FORCE_COLOR"] = "3"
    return env


def get_default_command() -> str:
    return settings.default_command


def get_agent_command(agent_type: AgentType) -> str:
    if agent_type == AgentType.CODEX:
        return "codex"
    if agent_type == AgentType.CURSOR:
        return "agent"
    return get_default_command()


class TTYDProcess:
    """Manages a single ttyd process backed by a tmux session for persistence."""

    def __init__(
        self,
        tab_id: str,
        port: int,
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        created_at: Optional[datetime] = None,
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
        env: Optional[Dict[str, str]] = None,
    ):
        self.tab_id = tab_id
        self.port = port
        self.name = name
        self.cwd = cwd
        self.solo_mode = solo_mode
        self.agent_type = agent_type
        self.target = target
        self.remote_profile_id = remote_profile_id
        self.remote_cwd = remote_cwd
        self.remote_reconnect = remote_reconnect
        self.remote_forward_port = remote_forward_port
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.workspace_role = workspace_role
        launch_env = env if env else self._default_env_for_agent(agent_type)
        self.env = self._clean_env(launch_env)
        self._prepare_agent_env()
        if agent_type != AgentType.CLAUDE:
            self._setup_tunnel_env()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.created_at = created_at or datetime.now()
        self.is_active = False
        self.tmux_session = _tmux_session_name(tab_id)
        # For terminal tabs, use the user's shell instead of an agent command.
        if shell:
            self.shell = shell
        elif agent_type == AgentType.TERMINAL:
            self.shell = os.environ.get("SHELL", "/bin/bash")
        else:
            self.shell = get_agent_command(agent_type)

    @staticmethod
    def _clean_env(env: Dict[str, str]) -> Dict[str, str]:
        cleaned: Dict[str, str] = {}
        for key, value in env.items():
            name = str(key).strip()
            if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Invalid environment variable name: {name or '<empty>'}")
            cleaned[name] = str(value)
        # Normalize proxy env vars: add the opposite-case variant so both
        # Node.js (uppercase HTTP_PROXY) and Unix tools (lowercase http_proxy)
        # pick up the same setting regardless of what the user typed.
        _PROXY_KEYS = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy"})
        additions: dict[str, str] = {}
        for key in list(cleaned):
            lower = key.lower()
            if lower in _PROXY_KEYS:
                opposite = key.upper() if key.islower() else key.lower()
                if opposite not in cleaned:
                    additions[opposite] = cleaned[key]
        cleaned.update(additions)
        return cleaned

    @staticmethod
    def _default_env_for_agent(agent_type: AgentType) -> Dict[str, str]:
        if agent_type != AgentType.CLAUDE:
            return {}
        return DEFAULT_CLAUDE_LAUNCH_ENV.copy()

    def _prepare_agent_env(self) -> None:
        if self.agent_type != AgentType.CLAUDE:
            return
        self._normalize_volcengine_coding_plan_model()

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

    # Tunnel registry: tunnel_key -> (local_port, process)
    _tunnel_registry: dict[str, tuple[int, subprocess.Popen]] = {}
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

    def _env_shell_prefix(self) -> str:
        if not self.env:
            return ""
        assignments = [f"{key}={shlex.quote(value)}" for key, value in self.env.items()]
        return "env " + " ".join(assignments) + " "

    def _env_export_commands(self) -> list[str]:
        return [f"export {key}={shlex.quote(value)}" for key, value in self.env.items()]

    def _with_env(self, command: str) -> str:
        if self.env and self.target == ExecutionTarget.LOCAL:
            return self._with_local_env_wrapper(command)
        return f"{self._env_shell_prefix()}{command}"

    def _with_local_env_wrapper(self, command: str) -> str:
        LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        script_path = LAUNCH_ENV_DIR / f"{self.tab_id}.sh"
        exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in self.env.items())
        script = "#!/bin/sh\n" "set -eu\n" f"{exports}\n" 'exec "${SHELL:-/bin/bash}" -lc "$1"\n'
        script_path.write_text(script, encoding="utf-8")
        os.chmod(script_path, 0o600)
        return f"/bin/sh {shlex.quote(str(script_path))} {shlex.quote(command)}"

    def _claude_settings_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE or not self.env:
            return ""
        if self.target != ExecutionTarget.LOCAL:
            return ""
        LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        settings_path = LAUNCH_ENV_DIR / f"{self.tab_id}.settings.json"
        settings_path.write_text(
            json.dumps({"env": self.env}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(settings_path, 0o600)
        return f" --settings {shlex.quote(str(settings_path))}"

    def _claude_model_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE:
            return ""
        model = self.env.get("ANTHROPIC_MODEL")
        if not model:
            return ""
        return f" --model {shlex.quote(model)}"

    def _solo_command(self) -> Optional[str]:
        if self.agent_type == AgentType.CODEX:
            return "codex --ask-for-approval never --sandbox danger-full-access"
        if self.agent_type == AgentType.CLAUDE:
            return (
                "IS_SANDBOX=1 claude --dangerously-skip-permissions"
                f"{self._claude_settings_arg()}{self._claude_model_arg()}"
            )
        if self.agent_type == AgentType.CURSOR:
            # Cursor agent runs in yolo by default; solo_mode toggle is a no-op.
            return "agent"
        return None

    def _tmux_shell_command(self, session_exists: bool) -> str:
        if (
            self.solo_mode
            and not session_exists
            and self.agent_type
            in {
                AgentType.CLAUDE,
                AgentType.CODEX,
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
        """
        if _tmux_session_exists(self.tmux_session):
            return False

        _ensure_tmux_server()
        cmd = ["tmux", "new-session", "-d", "-s", self.tmux_session]
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])
        cmd.append("--")
        if self.target == ExecutionTarget.REMOTE:
            cmd.append(shlex.join(self._build_remote_launcher()))
        elif self.solo_mode and self.agent_type in {AgentType.CLAUDE, AgentType.CODEX}:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command())}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type == AgentType.CURSOR:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command())}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type == AgentType.CLAUDE:
            cmd.append(self._with_env(self._agent_start_command()))
        else:
            cmd.append(self._with_env(self.shell))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=_agent_spawn_env(),
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux new-session failed with code {proc.returncode}")
        return True

    async def start(self) -> None:
        """Start the ttyd process with tmux for session persistence."""
        if self.process and self.process.returncode is None:
            logger.warning(f"Process for tab {self.tab_id} already running")
            return

        # Ensure tmux server is running first
        _ensure_tmux_server()

        # Check if tmux session already exists
        session_exists = _tmux_session_exists(self.tmux_session)
        if session_exists:
            logger.info(f"tmux session {self.tmux_session} exists, will reattach")
        else:
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
                env=_agent_spawn_env(),
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

    # xterm.js client options tuned for minimal input latency and maximum
    # render throughput. These are forwarded to every ttyd instance via
    # repeated ``-t key=value`` flags.
    #
    # Rationale:
    #   cursorBlink=false        — each blink repaints the cursor cell; also
    #                              makes the UI feel janky on repaint-heavy
    #                              frames. Saves ~1 repaint/500ms and removes a
    #                              common source of perceived typing jitter.
    #   rendererType=webgl       — WebGL2 renderer is ~2–5× faster than the
    #                              default canvas renderer under high output
    #                              throughput; frees the main thread so key
    #                              events are dispatched sooner.
    #   allowProposedApi=true    — required for rendererType=webgl in some
    #                              ttyd/xterm.js version combos.
    #   drawBoldTextInBrightColors=false — avoids extra color map lookups.
    #   minimumContrastRatio=1   — skip contrast adjustment work per glyph.
    #   scrollback=100000        — kept from the previous baseline.
    #   fastScrollModifier=alt   — kept from the previous baseline.
    #   macOptionIsMeta=false    — kept from the previous baseline.
    _TTYD_CLIENT_OPTIONS: tuple[tuple[str, str], ...] = (
        ("scrollback", "100000"),
        ("fastScrollModifier", "alt"),
        ("macOptionIsMeta", "false"),
        ("cursorBlink", "false"),
        ("rendererType", "webgl"),
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
                "new-session",
                "-A",
                "-s",
                self.tmux_session,
            ]
        )
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])

        if self.target == ExecutionTarget.REMOTE:
            cmd.extend(self._build_remote_launcher())
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
                    f"{self._with_env(self._agent_start_command())}; exec {user_shell}",
                ]
            )
        elif self.agent_type == AgentType.CURSOR and not session_exists:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{self._with_env(self._agent_start_command())}; exec {user_shell}",
                ]
            )
        elif self.agent_type == AgentType.CLAUDE and not session_exists:
            cmd.append(self._with_env(self._agent_start_command()))
        else:
            cmd.append(self._with_env(self.shell))

        return cmd

    def _agent_start_command(self) -> str:
        if self.agent_type == AgentType.CODEX:
            if self.solo_mode:
                return "codex --ask-for-approval never --sandbox danger-full-access"
            return "codex"
        if self.agent_type == AgentType.CURSOR:
            return "agent"
        if self.agent_type == AgentType.TERMINAL:
            return "${SHELL:-/bin/bash} -l"
        if self.solo_mode:
            return (
                "IS_SANDBOX=1 claude --dangerously-skip-permissions"
                f"{self._claude_settings_arg()}{self._claude_model_arg()}"
            )
        return f"{get_default_command()}{self._claude_settings_arg()}{self._claude_model_arg()}"

    def _remote_ssh_target(self) -> tuple[str, int]:
        if not self.remote_profile_id:
            raise ValueError("Remote tab requires remote_profile_id")
        profile = remote_profile_manager.get_profile(self.remote_profile_id)
        if not profile:
            raise ValueError(f"Remote profile not found: {self.remote_profile_id}")
        host = f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host
        return host, profile.port

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

    def _build_remote_attach_command(self) -> str:
        remote_session = self.tmux_session
        cwd = self.remote_cwd or self.cwd or "~"
        start_command = self._with_env(self._agent_start_command())
        quoted_session = shlex.quote(remote_session)
        quoted_start = shlex.quote(start_command)
        shell = "${SHELL:-/bin/bash}"
        bootstrap_path = self._remote_path_bootstrap()
        checks: list[str] = []

        direct_start_script = "; ".join(
            [
                "printf 'Remote tmux not found in PATH; starting without remote tmux persistence.\\n'",
                start_command,
                "status=$?",
                "printf '\\nRemote agent exited with code %s. Dropping to shell.\\n' \"$status\"",
                f"exec {shell} -l",
            ]
        )

        script = "; ".join(
            [
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
                f"exec tmux attach-session -t {quoted_session}; "
                "fi",
                direct_start_script,
            ]
        )
        return self._remote_shell_command(script)

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

    def _build_remote_launcher(self) -> list[str]:
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

    async def _log_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            async for line in self.process.stderr:
                logger.warning(f"ttyd[{self.tab_id}] stderr: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"Error reading stderr for tab {self.tab_id}: {e}")

    async def _configure_tmux(self) -> None:
        """Configure tmux options for better mouse scrolling and user experience."""
        try:
            # Wait a bit for tmux session to be ready
            await asyncio.sleep(0.5)

            # Set tmux options - these will apply to all sessions
            tmux_commands = [
                ["set", "-g", "mouse", "off"],
                ["set", "-g", "history-limit", "100000"],
                ["set", "-g", "terminal-overrides", "xterm*:smcup@:rmcup@"],
                # Advertise 24-bit color so agent TUIs render their full palette
                # instead of degrading to a flat, colorless UI under tmux. The
                # tmux server inherits its global environment from whatever
                # launched it; if that parent disabled color (NO_COLOR=1 /
                # FORCE_COLOR=0, common when started from another agent CLI),
                # tmux forces those onto every new pane and overrides the env we
                # pass to new-session. Scrub them from the global environment so
                # panes can emit color.
                ["set", "-as", "terminal-features", ",xterm-256color:RGB"],
                ["setenv", "-g", "-u", "NO_COLOR"],
                ["setenv", "-g", "FORCE_COLOR", "3"],
                ["setenv", "-g", "COLORTERM", "truecolor"],
                # Allow scrolling without entering copy mode explicitly
                ["set", "-g", "mode-keys", "vi"],
                ["set", "-g", "status", "off"],
            ]

            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "tmux",
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            # Set options on our specific session too
            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "tmux",
                        "-t",
                        self.tmux_session,
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            logger.info(f"Configured tmux options for tab {self.tab_id}")
        except Exception as e:
            logger.warning(f"Failed to configure tmux for tab {self.tab_id}: {e}")

    async def _run_remote_capture_command(self, remote_command: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self._build_remote_ssh_command(remote_command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_REMOTE_CAPTURE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("remote tmux capture timed out") from exc

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"remote tmux capture failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def _capture_remote_history(self, lines: int = 100000) -> str:
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux capture-pane -p -e "
                    f"-S {shlex.quote(start)} "
                    f"-t {shlex.quote(self.tmux_session)}"
                ),
            ]
        )
        return await self._run_remote_capture_command(self._remote_shell_command(script))

    async def _capture_local_history(self, lines: int = 100000) -> str:
        """Capture full terminal history from tmux for replay on reconnect.

        Captures the entire terminal content (scrollback + visible screen)
        because the client-side replay must clear and rewrite the full
        buffer when ttyd has already rendered the visible screen before
        our script can intercept.

        Returns an empty string if the tmux session does not exist yet
        (ttyd creates sessions lazily on first WebSocket connection).
        """
        if not _tmux_session_exists(self.tmux_session):
            return ""
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-e",
            "-S",
            start,
            "-t",
            self.tmux_session,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def capture_history(self, lines: int = 100000, prefer_remote: bool = False) -> str:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_history = await self._capture_remote_history(lines)
                if remote_history:
                    return remote_history
            except Exception as e:
                logger.debug(
                    "Falling back to local history for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_history(lines)

    async def _capture_local_cursor_position(self) -> Optional[CursorPosition]:
        """Capture tmux pane cursor position as zero-based x/y coordinates."""
        if not _tmux_session_exists(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            self.tmux_session,
            "#{cursor_x} #{cursor_y}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux display-message failed with code {proc.returncode}")

        parts = stdout.decode("utf-8", errors="ignore").strip().split()
        if len(parts) != 2:
            return None

        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def _capture_remote_cursor_position(self) -> Optional[CursorPosition]:
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux display-message -p "
                    f"-t {shlex.quote(self.tmux_session)} "
                    f"{shlex.quote('#{cursor_x} #{cursor_y}')}"
                ),
            ]
        )
        stdout = await self._run_remote_capture_command(self._remote_shell_command(script))
        parts = stdout.strip().split()
        if len(parts) != 2:
            return None
        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def capture_cursor_position(
        self,
        prefer_remote: bool = False,
    ) -> Optional[CursorPosition]:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_cursor = await self._capture_remote_cursor_position()
                if remote_cursor:
                    return remote_cursor
            except Exception as e:
                logger.debug(
                    "Falling back to local cursor for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_cursor_position()

    async def capture_foreground_command(self) -> Optional[str]:
        """Capture the foreground command currently running in the tmux pane."""
        if not _tmux_session_exists(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            self.tmux_session,
            "#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        command = stdout.decode("utf-8", errors="ignore").strip()
        return command or None

    async def stop(self, kill_tmux: bool = False) -> None:
        """Stop the ttyd process. By default, KEEP tmux session alive for persistence.

        Args:
            kill_tmux: If True, also kill the tmux session. Only use this when
                      explicitly deleting a tab that the user no longer wants.
        """
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
            logger.warning(f"Explicitly killing tmux session {self.tmux_session} (tab deletion)")
            await _tmux_kill_session(self.tmux_session)
        else:
            logger.info(f"Preserving tmux session {self.tmux_session} for future reattachment")

        self.is_active = False

    def to_dict(self) -> dict:
        return {
            "id": self.tab_id,
            "name": self.name,
            "shell": self.shell,
            "cwd": self.cwd,
            "solo_mode": self.solo_mode,
            "agent_type": (
                self.agent_type.value if isinstance(self.agent_type, AgentType) else self.agent_type
            ),
            "target": (
                self.target.value if isinstance(self.target, ExecutionTarget) else self.target
            ),
            "remote_profile_id": self.remote_profile_id,
            "remote_cwd": self.remote_cwd,
            "remote_reconnect": self.remote_reconnect,
            "env": self.env,
            "remote_forward_port": self.remote_forward_port,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "workspace_role": (
                self.workspace_role.value
                if isinstance(self.workspace_role, WorkspaceSessionRole)
                else self.workspace_role
            ),
            "port": self.port,
            "created_at": self.created_at.isoformat(),
        }

    def to_schema(self) -> TerminalTab:
        return TerminalTab(
            id=self.tab_id,
            name=self.name,
            shell=self.shell,
            cwd=self.cwd,
            solo_mode=self.solo_mode,
            agent_type=self.agent_type,
            target=self.target,
            remote_profile_id=self.remote_profile_id,
            remote_cwd=self.remote_cwd,
            remote_reconnect=self.remote_reconnect,
            env=self.env,
            port=self.port,
            created_at=self.created_at,
            is_active=self.is_active,
            workspace_id=self.workspace_id,
            workspace_name=self.workspace_name,
            workspace_role=self.workspace_role,
        )


class TTYDManager:
    """Manages multiple ttyd processes with tmux-backed persistence."""

    def __init__(self) -> None:
        self.processes: Dict[str, TTYDProcess] = {}
        self._next_port = settings.ttyd_base_port
        self._tab_order: List[str] = []
        self._status_snapshots: Dict[str, _AgentStatusSnapshot] = {}
        self._status_cache: Dict[str, TerminalAgentStatus] = {}
        self._start_locks: Dict[str, asyncio.Lock] = {}
        logger.info("=" * 60)
        logger.info("Initializing TTYDManager - tmux session persistence enabled")
        logger.info("=" * 60)
        # Ensure tmux server is running on initialization
        _ensure_tmux_server()
        self._load_state()
        self._load_order()
        # Ensure all loaded tabs are in the order list
        self._sync_order_with_processes()

    def _load_state(self) -> None:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    max_port = self._next_port
                    for tab_data in data:
                        agent_type_str = tab_data.get("agent_type", "claude")
                        agent_type = (
                            AgentType(agent_type_str)
                            if agent_type_str in [e.value for e in AgentType]
                            else AgentType.CLAUDE
                        )
                        target_str = tab_data.get("target", "local")
                        target = (
                            ExecutionTarget(target_str)
                            if target_str in [e.value for e in ExecutionTarget]
                            else ExecutionTarget.LOCAL
                        )
                        workspace_role_str = tab_data.get("workspace_role")
                        workspace_role = (
                            WorkspaceSessionRole(workspace_role_str)
                            if workspace_role_str in [e.value for e in WorkspaceSessionRole]
                            else None
                        )
                        process = TTYDProcess(
                            tab_id=tab_data["id"],
                            port=tab_data["port"],
                            name=tab_data["name"],
                            shell=tab_data.get("shell"),
                            cwd=tab_data.get("cwd"),
                            created_at=datetime.fromisoformat(tab_data["created_at"]),
                            solo_mode=tab_data.get("solo_mode", False),
                            agent_type=agent_type,
                            target=target,
                            remote_profile_id=tab_data.get("remote_profile_id"),
                            remote_cwd=tab_data.get("remote_cwd"),
                            remote_reconnect=tab_data.get("remote_reconnect", True),
                            remote_forward_port=tab_data.get("remote_forward_port"),
                            env=tab_data.get("env", {}),
                            workspace_id=tab_data.get("workspace_id"),
                            workspace_name=tab_data.get("workspace_name"),
                            workspace_role=workspace_role,
                        )
                        self.processes[process.tab_id] = process
                        if process.port > max_port:
                            max_port = process.port
                    self._next_port = max_port + 1
                logger.info(f"Loaded {len(self.processes)} tabs from {STATE_FILE}")
                for tab_id, process in self.processes.items():
                    logger.info(
                        f"  - Tab: {process.name} (tmux: {process.tmux_session}, agent: {process.agent_type.value})"
                    )
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
        else:
            logger.info(f"No state file at {STATE_FILE}, starting fresh")

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump([p.to_dict() for p in self.processes.values()], f)
            logger.debug(f"Saved {len(self.processes)} tabs to {STATE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_order(self) -> None:
        """Load tab order from file."""
        if ORDER_FILE.exists():
            try:
                with open(ORDER_FILE, "r") as f:
                    self._tab_order = json.load(f)
                logger.info(f"Loaded tab order: {self._tab_order}")
            except Exception as e:
                logger.error(f"Failed to load tab order: {e}")
                self._tab_order = []
        else:
            logger.info(f"Order file {ORDER_FILE} does not exist, starting with empty order")

    def _save_order(self) -> None:
        """Save tab order to file."""
        try:
            ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ORDER_FILE, "w") as f:
                json.dump(self._tab_order, f)
            logger.info(f"Saved tab order to {ORDER_FILE}: {self._tab_order}")
        except Exception as e:
            logger.error(f"Failed to save tab order: {e}")

    def set_tab_order(self, tab_ids: List[str]) -> None:
        """Set the order of tabs."""
        logger.info(f"set_tab_order called with: {tab_ids}")
        logger.info(f"Current processes keys: {list(self.processes.keys())}")
        # Validate that all tab IDs exist
        valid_ids = [tid for tid in tab_ids if tid in self.processes]
        # Add any missing tabs at the end
        for tid in self.processes:
            if tid not in valid_ids:
                valid_ids.append(tid)
        self._tab_order = valid_ids
        logger.info(f"Final tab order set to: {self._tab_order}")
        self._save_order()

    def _ensure_tab_in_order(self, tab_id: str) -> None:
        """Ensure a tab is in the order list."""
        if tab_id not in self._tab_order:
            self._tab_order.append(tab_id)
            self._save_order()

    def set_tab_workspace_metadata(
        self,
        tab_id: str,
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        workspace_role: Optional[WorkspaceSessionRole],
    ) -> bool:
        """Attach workspace ownership metadata to an existing tab."""
        process = self.processes.get(tab_id)
        if not process:
            return False

        if (
            process.workspace_id == workspace_id
            and process.workspace_name == workspace_name
            and process.workspace_role == workspace_role
        ):
            return True

        process.workspace_id = workspace_id
        process.workspace_name = workspace_name
        process.workspace_role = workspace_role
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

    def _sync_order_with_processes(self) -> None:
        """Sync order list with current processes - add any missing tabs."""
        # Add any tabs that are in processes but not in order
        order_changed = False
        for tab_id in self.processes:
            if tab_id not in self._tab_order:
                self._tab_order.append(tab_id)
                order_changed = True
        # Remove any tab IDs that are in order but not in processes
        original_len = len(self._tab_order)
        self._tab_order = [tid for tid in self._tab_order if tid in self.processes]
        if len(self._tab_order) != original_len:
            order_changed = True
        # If order is still empty, initialize with all process IDs
        if not self._tab_order and self.processes:
            self._tab_order = list(self.processes.keys())
            order_changed = True
        if order_changed:
            logger.info(f"Synced tab order: {self._tab_order}")
            self._save_order()

    def _get_next_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

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
        env: Optional[Dict[str, str]] = None,
    ) -> TerminalTab:
        logger.info(
            f"create_tab called with: name={name}, solo_mode={solo_mode}, shell={shell}, cwd={cwd}, agent_type={agent_type}, target={target}, remote_profile_id={remote_profile_id}, remote_forward_port={remote_forward_port}, workspace_id={workspace_id}, workspace_role={workspace_role}"
        )
        tab_id = str(uuid.uuid4())
        port = self._get_next_port()

        process = TTYDProcess(
            tab_id,
            port,
            name,
            shell,
            cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            remote_forward_port=remote_forward_port,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
            env=env,
        )
        logger.info(
            f"Created TTYDProcess with solo_mode={process.solo_mode}, agent_type={process.agent_type}"
        )
        await process.start()

        self.processes[tab_id] = process
        self._ensure_tab_in_order(tab_id)
        self._save_state()
        return process.to_schema()

    async def ensure_tab_tmux_session(self, tab_id: str) -> bool:
        process = self.processes.get(tab_id)
        if not process:
            raise KeyError(tab_id)
        return await process.ensure_tmux_session()

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
            target=source.target,
            remote_profile_id=source.remote_profile_id,
            remote_cwd=source.remote_cwd,
            remote_reconnect=source.remote_reconnect,
            remote_forward_port=source.remote_forward_port,
            env=source.env,
        )

    async def delete_tab(self, tab_id: str) -> bool:
        """Delete a tab and explicitly kill its tmux session (user requested deletion)."""
        if tab_id not in self.processes:
            return False

        process = self.processes.pop(tab_id)
        logger.warning(
            f"User requested deletion of tab {tab_id}, killing tmux session {process.tmux_session}"
        )
        await process.stop(kill_tmux=True)
        if tab_id in self._tab_order:
            self._tab_order.remove(tab_id)
            self._save_order()
        self._save_state()
        return True

    def get_tab(self, tab_id: str) -> Optional[TerminalTab]:
        if tab_id not in self.processes:
            return None
        return self.processes[tab_id].to_schema()

    async def ensure_tab_running(self, tab_id: str) -> Optional[TerminalTab]:
        """Ensure the tab has a live ttyd listener while preserving tmux state."""
        process = self.processes.get(tab_id)
        if not process:
            return None

        lock = self._start_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            if _is_local_port_listening(process.port):
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
                "Starting missing ttyd listener for tab %s on port %s",
                tab_id,
                process.port,
            )
            try:
                await process.start()
            except Exception:
                # During uvicorn --reload an old backend may still own the
                # port briefly. If the listener exists now, let the proxy use
                # it; a later request will restart ttyd if that old listener is
                # cleaned up.
                if _is_local_port_listening(process.port):
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

    def list_tabs(self) -> list[TerminalTab]:
        # Return tabs in saved order
        logger.info(
            f"list_tabs called, _tab_order={self._tab_order}, processes={list(self.processes.keys())}"
        )
        ordered_tabs: list[TerminalTab] = []
        # First add tabs in the saved order
        for tab_id in self._tab_order:
            if tab_id in self.processes:
                ordered_tabs.append(self.processes[tab_id].to_schema())
        # Then add any tabs not in the order list
        for process in self.processes.values():
            if process.tab_id not in self._tab_order:
                ordered_tabs.append(process.to_schema())
        logger.info(f"list_tabs returning: {[t.name for t in ordered_tabs]}")
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

    def _classify_agent_status(
        self,
        process: TTYDProcess,
        output: str,
        output_hash: str,
        foreground_command: Optional[str],
    ) -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
        now = datetime.now()
        snapshot = self._status_snapshots.get(process.tab_id)
        last_hash = snapshot.get("hash") if snapshot else None
        last_changed_at = snapshot.get("last_changed_at") if snapshot else None

        if snapshot is None:
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": None,
            }
        elif last_hash != output_hash:
            last_changed_at = now
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": last_changed_at,
            }

        if not _tmux_session_exists(process.tmux_session):
            return AgentRuntimeStatus.OFFLINE, "Offline", "tmux session is not available", None

        # Only inspect the bottom of the visible screen — the current prompt
        # area. Historical scrollback is ignored so that words like
        # "Reading"/"editing" from past activity can't drive classification.
        non_empty = [line.strip() for line in output.splitlines() if line.strip()]
        tail_lines = non_empty[-5:]
        status_tail_lines = non_empty[-10:]
        tail = "\n".join(tail_lines).lower()
        status_tail = "\n".join(status_tail_lines).lower()
        last_line = tail_lines[-1] if tail_lines else ""

        for pattern in _ATTENTION_TAIL_PATTERNS:
            if pattern in tail:
                return (
                    AgentRuntimeStatus.ATTENTION,
                    "Agent waiting for input",
                    "needs your response",
                    last_changed_at,
                )

        for pattern in _WORKING_TAIL_PATTERNS:
            if pattern in status_tail:
                return (
                    AgentRuntimeStatus.WORKING,
                    "Working",
                    "agent is processing",
                    last_changed_at,
                )

        if _CLAUDE_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return (
                AgentRuntimeStatus.WORKING,
                "Working",
                "agent is processing",
                last_changed_at,
            )

        if _CURSOR_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return (
                AgentRuntimeStatus.WORKING,
                "Working",
                "agent is processing",
                last_changed_at,
            )

        if _BARE_SHELL_PROMPT_RE.search(last_line):
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                "shell prompt visible",
                last_changed_at,
            )

        for hint in _IDLE_TAIL_HINTS:
            if hint in tail:
                return (
                    AgentRuntimeStatus.IDLE,
                    "Idle",
                    "agent prompt visible",
                    last_changed_at,
                )

        if foreground_command and foreground_command in {"claude", "codex", "agent"}:
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                f"{foreground_command} is waiting",
                last_changed_at,
            )

        return (
            AgentRuntimeStatus.IDLE,
            "Idle",
            "no activity indicators",
            last_changed_at,
        )

    async def get_tab_agent_status(
        self,
        tab_id: str,
        use_cache: bool = True,
    ) -> Optional[TerminalAgentStatus]:
        """Get a best-effort terminal agent status for one tab."""
        process = self.processes.get(tab_id)
        if not process:
            return None

        sampled_at = datetime.now()
        cached = self._status_cache.get(tab_id)
        if (
            use_cache
            and cached
            and (sampled_at - cached.sampled_at).total_seconds() < _STATUS_CACHE_TTL_SECONDS
        ):
            return cached

        if not _tmux_session_exists(process.tmux_session):
            status = TerminalAgentStatus(
                tab_id=process.tab_id,
                tab_name=process.name,
                agent_type=process.agent_type,
                status=AgentRuntimeStatus.OFFLINE,
                status_text="Offline",
                detail="tmux session is not available",
                tmux_session=process.tmux_session,
                last_changed_at=None,
                sampled_at=sampled_at,
            )
            self._status_cache[tab_id] = status
            return status

        try:
            raw_output = await process.capture_history(lines=120)
        except Exception as e:
            logger.debug(f"Unable to capture status output for tab {tab_id}: {e}")
            raw_output = ""

        foreground_command = await process.capture_foreground_command()
        output = _ANSI_ESCAPE_RE.sub("", raw_output)
        output_hash = hashlib.sha256(output.encode("utf-8", errors="ignore")).hexdigest()
        runtime_status, status_text, detail, last_changed_at = self._classify_agent_status(
            process,
            output,
            output_hash,
            foreground_command,
        )

        status = TerminalAgentStatus(
            tab_id=process.tab_id,
            tab_name=process.name,
            agent_type=process.agent_type,
            status=runtime_status,
            status_text=status_text,
            detail=detail,
            tmux_session=process.tmux_session,
            last_changed_at=last_changed_at,
            sampled_at=sampled_at,
        )
        self._status_cache[tab_id] = status
        return status

    async def list_tab_agent_statuses(
        self,
        tab_ids: Optional[Iterable[str]] = None,
    ) -> list[TerminalAgentStatus]:
        """List best-effort terminal agent statuses in tab order."""
        if tab_ids is None:
            ordered_ids = [tab_id for tab_id in self._tab_order if tab_id in self.processes]
            ordered_ids.extend(tab_id for tab_id in self.processes if tab_id not in ordered_ids)
        else:
            seen: set[str] = set()
            ordered_ids = []
            for tab_id in tab_ids:
                if tab_id in self.processes and tab_id not in seen:
                    ordered_ids.append(tab_id)
                    seen.add(tab_id)

        results = await asyncio.gather(
            *(self.get_tab_agent_status(tab_id) for tab_id in ordered_ids)
        )
        return [status for status in results if status]

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
        if tab_id not in self.processes:
            return None

        process = self.processes[tab_id]
        needs_restart = False

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
            process.agent_type = agent_type
            needs_restart = True
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
            process.env = TTYDProcess._clean_env(env)
            process._prepare_agent_env()
            if process.agent_type != AgentType.CLAUDE:
                process._setup_tunnel_env()
            needs_restart = True

        if needs_restart:
            logger.info(
                f"Updating tab {tab_id}, restarting ttyd but preserving tmux session {process.tmux_session}"
            )
            await process.stop(kill_tmux=False)
            await process.start()

        self._save_state()
        return process.to_schema()

    async def start_all_tabs(self) -> None:
        """Start all saved tabs on startup. Tmux sessions survive backend restarts.

        Tabs are started in parallel: each ``process.start()`` already awaits a
        ~1s sleep after spawning ttyd, so a sequential loop took ~N seconds for
        N tabs and blocked FastAPI's lifespan startup. Running them concurrently
        keeps the post-restart unavailable window roughly constant regardless of
        tab count, which directly reduces the duration of the front-end
        "Reconnecting…" overlay after a backend reload.
        """
        # Ensure tmux server is running before starting any tabs
        logger.info("Ensuring tmux server is running...")
        _ensure_tmux_server()

        # List all existing tmux sessions for debugging
        try:
            result = os.popen("tmux ls 2>&1").read()
            logger.info(f"Existing tmux sessions before starting tabs:\n{result}")
        except Exception as e:
            logger.debug(f"Could not list tmux sessions: {e}")

        processes = list(self.processes.values())
        logger.info(f"Starting {len(processes)} saved tabs in parallel...")

        async def _start_one(process: TTYDProcess) -> None:
            try:
                await process.start()
                session_status = (
                    "reattached" if _tmux_session_exists(process.tmux_session) else "created"
                )
                logger.info(
                    f"Tab {process.tab_id} started (tmux session {process.tmux_session} {session_status})"
                )
            except Exception as e:
                logger.error(f"Failed to start tab {process.tab_id}: {e}")

        await asyncio.gather(*(_start_one(p) for p in processes))

    async def cleanup(self) -> None:
        """Stop ttyd processes but keep tmux sessions alive for next startup."""
        logger.info("=" * 60)
        logger.info("CLEANING UP - tmux sessions WILL BE PRESERVED")
        logger.info("=" * 60)
        for process in list(self.processes.values()):
            logger.info(
                f"Will preserve tmux session: {process.tmux_session} for tab: {process.name}"
            )
            await process.stop(kill_tmux=False)
        logger.info("Cleanup complete - all tmux sessions preserved")


# Global manager instance
ttyd_manager = TTYDManager()
