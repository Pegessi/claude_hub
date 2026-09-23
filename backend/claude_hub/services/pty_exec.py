"""PTY-exec primitive for PTY-gateway SSH hosts.

A gateway sshd (see :mod:`remote_profiles`) ignores both ``ssh host 'cmd'`` and
a piped non-tty stdin, but an **argv-less** ``ssh -tt`` connection lands in a
real interactive shell. To run a one-shot command (directory listing, tmux
history/cursor capture) on such a host we must drive that PTY like a human:

1. open ``ssh -tt host`` with a real local PTY attached and **no** argv command;
2. wait for the genuine shell prompt to appear. The connect-time ``<Trial …>
   init $`` splash is *not* a shell prompt and must not be typed into;
3. send Enter and confirm a second prompt (round-trip synchronisation) so we
   know keystrokes are actually being read;
4. type a single physical line that brackets the real command with unique
   sentinels, capture the bytes between them, and read the exit code off the
   closing sentinel.

Multiline/arbitrary commands are transported as one base64 token so local line
discipline cannot mangle them. The interactive tab launcher reuses the same
prompt/handshake primitives from :mod:`pty_gateway_bridge`.

Everything network-touching is behind the :class:`SshPty` seam so tests inject a
scripted fake channel and never open a socket.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import pty
import re
import secrets
import shlex
import struct
import termios
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

from ..models import RemoteProfile

__all__ = [
    "PtyExecError",
    "PtyExecResult",
    "SshPty",
    "build_guarded_line",
    "extract_guarded_result",
    "looks_like_real_prompt",
    "new_token",
    "open_ssh_pty",
    "pty_exec",
    "real_prompt_seen",
    "spawn_pty",
    "strip_terminal_noise",
]

# A genuine login shell prompt, e.g. ``tiger@n1-2-3:~$`` or ``root@h:/#``.
# The gateway connect splash (``<Trial worker_0> init $``) deliberately does NOT
# match because it lacks the ``user@host:`` shape.
_REAL_PROMPT_RE = re.compile(r"[A-Za-z0-9_.\-]+@[A-Za-z0-9_.\-]+:[^\r\n]*[#$]\s*$")
# CSI / OSC / other escape sequences that must be ignored when matching prompts.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

BEGIN_TEMPLATE = "__CHPBEGIN_{token}__"
_END_RE_TEMPLATE = r"__CHPEND_{token}__:(-?\d+)"

DEFAULT_PROMPT_TIMEOUT = 25.0
DEFAULT_COMMAND_TIMEOUT = 20.0
_PROMPT_CONFIRM_TIMEOUT = 12.0
# Cap each event-wait so readiness is re-polled on a fixed cadence even if a
# wake event is coalesced under a busy event loop.
_READINESS_POLL_SECONDS = 0.1
_GRACE_EXIT_SECONDS = 1.0


class PtyExecError(RuntimeError):
    """Raised when a PTY-gateway command cannot be driven reliably."""


@dataclass(frozen=True)
class PtyExecResult:
    body: str
    returncode: int


def new_token() -> str:
    return secrets.token_hex(8)


def strip_terminal_noise(data: bytes | str) -> str:
    """Decode PTY bytes and drop ANSI/OSC escapes, carriage returns, NULs."""

    text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else data
    text = _ANSI_RE.sub("", text)
    # Collapse \r\n / lone \r so prompt/sentinel matching is line-based.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text


def real_prompt_seen(buffer: bytes | str, *, search_last_lines: int = 3) -> bool:
    """True if a genuine ``user@host:…$`` prompt is on a recent complete line."""

    text = strip_terminal_noise(buffer)
    lines = text.split("\n")
    tail = [line.strip() for line in lines[-search_last_lines:] if line.strip()]
    return any(_REAL_PROMPT_RE.search(line) for line in tail)


def looks_like_real_prompt(line: str) -> bool:
    return bool(_REAL_PROMPT_RE.search(strip_terminal_noise(line).strip()))


def build_guarded_line(command: str, token: str) -> str:
    """Render ``command`` as one typed physical line bracketed by sentinels.

    The command is base64-encoded so it occupies a single line and survives the
    gateway PTY's line discipline untouched. Output is delimited by a BEGIN
    marker before execution and an END marker carrying the exit code after it.
    """

    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    begin = BEGIN_TEMPLATE.format(token=token)
    # The executed END marker is ``__CHPEND_<token>__:<rc>``; ``%s`` is filled by
    # printf from $__chp_rc. The echoed (unexecuted) input line shows ``%s`` and
    # so never matches the digit-only closing regex.
    end_fmt = f"__CHPEND_{token}__:%s"
    return (
        f"printf '\\n{begin}\\n'; "
        f"echo {encoded} | base64 -d | bash; "
        f"__chp_rc=$?; "
        f"printf '{end_fmt}\\n' \"$__chp_rc\""
    )


def extract_guarded_result(buffer: bytes | str, token: str) -> Optional[PtyExecResult]:
    """Pull the body/exit-code between a command's sentinels from PTY bytes.

    The echoed input line and the connect splash are ignored: only bytes after
    the BEGIN marker and before the END marker are returned.
    """

    text = strip_terminal_noise(buffer)
    begin = BEGIN_TEMPLATE.format(token=token)
    end_re = re.compile(_END_RE_TEMPLATE.format(token=re.escape(token)))

    begin_idx = text.rfind(begin)
    if begin_idx < 0:
        return None
    tail = text[begin_idx + len(begin) :]
    match = end_re.search(tail)
    if not match:
        return None
    body = tail[: match.start()]
    body = body.lstrip("\n").rstrip()
    try:
        returncode = int(match.group(1))
    except ValueError:
        returncode = -1
    return PtyExecResult(body=body, returncode=returncode)


def ssh_target(profile: RemoteProfile) -> str:
    return f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host


def build_ssh_argv(profile: RemoteProfile, *, connect_timeout: int = 8) -> list[str]:
    """Argv for an interactive, command-less ``ssh -tt`` connection."""

    cmd = [
        "ssh",
        "-tt",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "LogLevel=ERROR",
    ]
    if profile.port and profile.port != 22:
        cmd.extend(["-p", str(profile.port)])
    cmd.append(ssh_target(profile))
    return cmd


def set_winsize(fd: int, rows: int = 40, cols: int = 160) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


class SshPty:
    """A live ``ssh -tt`` child bound to a local PTY we read and write."""

    def __init__(
        self,
        process: Optional[asyncio.subprocess.Process],
        master_fd: int,
        slave_fd: Optional[int],
        *,
        owns_process: bool = True,
    ) -> None:
        self.process = process
        self.master_fd = master_fd
        self.slave_fd = slave_fd
        self.owns_process = owns_process
        self._buffer = bytearray()
        self._wake = asyncio.Event()
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- lifecycle / raw IO -------------------------------------------------

    def start_reader(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.master_fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self.master_fd, 8192)
        except BlockingIOError:
            return
        except OSError:
            self._mark_closed()
            return
        if not chunk:
            self._mark_closed()
            return
        self._buffer.extend(chunk)
        self._wake.set()

    def _mark_closed(self) -> None:
        if not self._closed:
            self._closed = True
            self._wake.set()

    def write(self, data: bytes) -> None:
        if self._closed:
            raise PtyExecError("PTY channel closed before write")
        try:
            os.write(self.master_fd, data)
        except OSError as exc:
            raise PtyExecError(f"failed to write to PTY: {exc}") from exc

    @property
    def buffer(self) -> bytes:
        return bytes(self._buffer)

    async def wait_for(
        self,
        predicate: Callable[[bytes], bool],
        timeout: float,
        *,
        start_offset: int = 0,
    ) -> bytes:
        """Wait until predicate is true on the buffer; return current bytes.

        The inner wait is capped at ``_READINESS_POLL_SECONDS`` so readiness is
        re-evaluated on a fixed cadence even if an event wake is coalesced/lost
        under a busy event loop (observed under uvicorn for the post-CR prompt).
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate(self.buffer[start_offset:] if start_offset else self.buffer):
                return self.buffer
            if self._closed:
                tail = strip_terminal_noise(self.buffer[-400:])
                raise PtyExecError(f"SSH PTY closed before readiness; tail: {tail!r}")
            remaining = deadline - loop.time()
            if remaining <= 0:
                tail = strip_terminal_noise(self.buffer[-400:])
                raise PtyExecError(f"timed out waiting for PTY condition; tail: {tail!r}")
            self._wake.clear()
            # Re-check after clearing in case bytes arrived between check and clear.
            if predicate(self.buffer[start_offset:] if start_offset else self.buffer):
                return self.buffer
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=min(_READINESS_POLL_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                continue

    async def aclose(self) -> None:
        if self._closed and self.process is None:
            return
        # Politely log out of the remote shell, then reap the local ssh.
        if not self._closed:
            try:
                self.write(b"exit\r")
            except PtyExecError:
                pass
        if self.process is not None and self.owns_process:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=_GRACE_EXIT_SECONDS)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.process.kill()
        self._detach_reader()
        for fd in (self.slave_fd, self.master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.slave_fd = None
        self._closed = True

    def _detach_reader(self) -> None:
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except (OSError, ValueError):
                pass
            self._loop = None

    # ---- high-level handshake ----------------------------------------------

    async def wait_for_real_prompt(self, timeout: float = DEFAULT_PROMPT_TIMEOUT) -> None:
        """Wait for the gateway flash to clear and a real shell prompt to land.

        Two prompts are required: the first is detected, then Enter is sent and a
        *fresh* prompt must appear (a real round-trip). Typing before this point
        risks keystrokes being swallowed by the ``init $`` splash.
        """

        await self.wait_for(real_prompt_seen, timeout)
        offset = len(self.buffer)
        self.write(b"\r")
        await self.wait_for(
            lambda chunk: real_prompt_seen(chunk),
            _PROMPT_CONFIRM_TIMEOUT,
            start_offset=offset,
        )

    async def run_guarded(
        self,
        command: str,
        *,
        token: Optional[str] = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> PtyExecResult:
        token = token or new_token()
        line = build_guarded_line(command, token)
        offset = len(self.buffer)
        self.write(line.encode("utf-8") + b"\r")

        def _done(chunk: bytes) -> bool:
            return extract_guarded_result(chunk, token) is not None

        await self.wait_for(_done, timeout, start_offset=offset)
        result = extract_guarded_result(self.buffer[offset:], token)
        assert result is not None  # guarded by wait_for predicate
        return result


async def spawn_pty(
    argv: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    rows: int = 40,
    cols: int = 160,
) -> SshPty:
    """Spawn ``argv`` attached to a fresh raw local PTY. Generic seam used both
    for real ``ssh -tt`` and for hermetic tests that run a local fake shell."""

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, rows, cols)
    _make_raw(master_fd)
    child_env = env
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=child_env,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    channel = SshPty(process, master_fd, slave_fd)
    channel.start_reader()
    return channel


async def open_ssh_pty(
    profile: RemoteProfile,
    *,
    connect_timeout: int = 8,
    extra_ssh_args: Optional[list[str]] = None,
    rows: int = 40,
    cols: int = 160,
) -> SshPty:
    """Spawn an argv-less ``ssh -tt`` with a fresh local PTY attached."""

    argv = build_ssh_argv(profile, connect_timeout=connect_timeout)
    if extra_ssh_args:
        argv[1:1] = extra_ssh_args  # insert after "ssh", before "-tt"
    return await spawn_pty(argv, rows=rows, cols=cols)


def _make_raw(fd: int) -> None:
    try:
        attrs = termios.tcgetattr(fd)
    except termios.error:
        return
    # cfmakeraw equivalent: no echo/canonical processing, no signals.
    attrs[0] &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass


# Type of the injectable connection factory used by :func:`pty_exec`.
Connector = Callable[[RemoteProfile], Awaitable[SshPty]]


async def pty_exec(
    profile: RemoteProfile,
    command: str,
    *,
    prompt_timeout: float = DEFAULT_PROMPT_TIMEOUT,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    connector: Optional[Connector] = None,
) -> str:
    """Run ``command`` on a gateway host through a driven PTY; return stdout.

    Raises :class:`PtyExecError` on handshake/timeout failure. The remote exit
    code is not enforced here — callers parse the body and surface their own
    errors (e.g. invalid JSON → HTTP 502).
    """

    connect = connector or (lambda p: open_ssh_pty(p))
    channel = await connect(profile)
    try:
        await channel.wait_for_real_prompt(timeout=prompt_timeout)
        result = await channel.run_guarded(command, timeout=command_timeout)
        return result.body
    finally:
        await channel.aclose()


def quote_single_line(command: str) -> str:
    """Convenience shlex quote kept here for callers building remote snippets."""

    return shlex.quote(command)
