import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

from ..config import settings
from ..models import AgentType, TerminalTab

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".claude_hub" / "tabs.json"
ORDER_FILE = Path.home() / ".claude_hub" / "tab_order.json"
TMUX_SESSION_PREFIX = "claude-hub-"


class CursorPosition(TypedDict):
    cursor_x: int
    cursor_y: int


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


def get_default_command() -> str:
    return settings.default_command


def get_agent_command(agent_type: AgentType) -> str:
    if agent_type == AgentType.CODEX:
        return "codex"
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
    ):
        self.tab_id = tab_id
        self.port = port
        self.name = name
        self.cwd = cwd
        self.solo_mode = solo_mode
        self.agent_type = agent_type
        self.process: Optional[asyncio.subprocess.Process] = None
        self.created_at = created_at or datetime.now()
        self.is_active = False
        self.tmux_session = _tmux_session_name(tab_id)
        # For terminal tabs, use the user's shell instead of an agent command.
        if shell:
            self.shell = shell
        elif agent_type == AgentType.CURSOR:
            self.shell = os.environ.get("SHELL", "/bin/bash")
        else:
            self.shell = get_agent_command(agent_type)

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
        logger.info(f"Starting ttyd for tab {self.tab_id} on port {self.port}: {' '.join(cmd)}")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
        cmd = [
            settings.ttyd_path,
            "--port",
            str(self.port),
            "--interface",
            "127.0.0.1",
            "--writable",
            # Improve scrolling behavior with xterm.js options
            "-t",
            "scrollback=100000",
            "-t",
            "fastScrollModifier=alt",
            "-t",
            "macOptionIsMeta=false",
            "tmux",
            "new-session",
            "-A",
            "-s",
            self.tmux_session,
        ]
        if self.cwd:
            cmd.extend(["-c", self.cwd])

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
            if self.agent_type == AgentType.CODEX:
                solo_command = "codex --ask-for-approval never --sandbox danger-full-access"
            else:
                solo_command = "IS_SANDBOX=1 claude --dangerously-skip-permissions"
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{solo_command}; exec {user_shell}",
                ]
            )
        else:
            cmd.append(self.shell)

        return cmd

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

    async def capture_history(self, lines: int = 100000) -> str:
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

    async def capture_cursor_position(self) -> Optional[CursorPosition]:
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
            port=self.port,
            created_at=self.created_at,
            is_active=self.is_active,
        )


class TTYDManager:
    """Manages multiple ttyd processes with tmux-backed persistence."""

    def __init__(self) -> None:
        self.processes: Dict[str, TTYDProcess] = {}
        self._next_port = settings.ttyd_base_port
        self._tab_order: List[str] = []
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
                        process = TTYDProcess(
                            tab_id=tab_data["id"],
                            port=tab_data["port"],
                            name=tab_data["name"],
                            shell=tab_data.get("shell"),
                            cwd=tab_data.get("cwd"),
                            created_at=datetime.fromisoformat(tab_data["created_at"]),
                            solo_mode=tab_data.get("solo_mode", False),
                            agent_type=agent_type,
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
    ) -> TerminalTab:
        logger.info(
            f"create_tab called with: name={name}, solo_mode={solo_mode}, shell={shell}, cwd={cwd}, agent_type={agent_type}"
        )
        tab_id = str(uuid.uuid4())
        port = self._get_next_port()

        process = TTYDProcess(
            tab_id, port, name, shell, cwd, solo_mode=solo_mode, agent_type=agent_type
        )
        logger.info(
            f"Created TTYDProcess with solo_mode={process.solo_mode}, agent_type={process.agent_type}"
        )
        await process.start()

        self.processes[tab_id] = process
        self._ensure_tab_in_order(tab_id)
        self._save_state()
        return process.to_schema()

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
        return await process.capture_history(lines)

    async def get_tab_cursor_position(self, tab_id: str) -> Optional[CursorPosition]:
        """Get tmux cursor position for a tab."""
        process = self.processes.get(tab_id)
        if not process:
            return None
        return await process.capture_cursor_position()

    async def update_tab(
        self,
        tab_id: str,
        name: Optional[str] = None,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: Optional[bool] = None,
        agent_type: Optional[AgentType] = None,
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

        if needs_restart:
            logger.info(
                f"Updating tab {tab_id}, restarting ttyd but preserving tmux session {process.tmux_session}"
            )
            await process.stop(kill_tmux=False)
            await process.start()

        self._save_state()
        return process.to_schema()

    async def start_all_tabs(self) -> None:
        """Start all saved tabs on startup. Tmux sessions survive backend restarts."""
        # Ensure tmux server is running before starting any tabs
        logger.info("Ensuring tmux server is running...")
        _ensure_tmux_server()

        # List all existing tmux sessions for debugging
        try:
            result = os.popen("tmux ls 2>&1").read()
            logger.info(f"Existing tmux sessions before starting tabs:\n{result}")
        except Exception as e:
            logger.debug(f"Could not list tmux sessions: {e}")

        logger.info(f"Starting {len(self.processes)} saved tabs...")
        for process in self.processes.values():
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
