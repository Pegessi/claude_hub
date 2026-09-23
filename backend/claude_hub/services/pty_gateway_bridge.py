"""Transparent PTY bridge for interactive tabs on a PTY-gateway host.

A normal remote tab runs ``ssh -tt host '<bootstrap>'`` — the bootstrap is an
argv command. A gateway swallows the argv, so instead the tab launcher runs this
bridge under the local tmux/ttyd pane:

    browser → ttyd → local tmux → bridge fd0/fd1 → proxy-PTY → ssh -tt → remote
                                                                      PTY/bash

The bridge is a byte-for-byte proxy with a one-shot injection at connect time:

1. Spawn an **argv-less** ``ssh -tt`` (plus the ``-R`` report reverse-forward)
   bound to a fresh local proxy PTY.
2. Hold off forwarding keystrokes until the genuine ``user@host:…$`` prompt has
   appeared *twice* (the connect ``init $`` splash is never typed into).
3. Type ``echo <bootstrap-b64> | base64 -d | bash`` so the remote login shell
   starts the detached tmux session and ``exec tmux attach``es; then drop into
   fully transparent passthrough — Up/Tab/Ctrl-C/alt-screen/scrollback all flow
   untouched because every intervening local PTY is in raw mode.

On SSH exit the bridge re-runs the whole connection + handshake (the remote
detached tmux survives, so it re-attaches the same processes).

Runnable as ``python -m claude_hub.services.pty_gateway_bridge``. The
:class:`BootstrapHandshake` state machine is IO-free and unit-tested directly.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import os
import pty
import signal
import struct
import termios
from typing import Optional

from ..models import RemoteProfile
from ..models.schemas import RemoteTransport
from .pty_exec import (
    build_ssh_argv,
    real_prompt_seen,
    set_winsize,
)

_RECONNECT_DELAY_SECONDS = 3.0


class BootstrapHandshake:
    """Finite state machine that turns the connect stream into typed bootstrap.

    Feed every byte read from the SSH PTY to :meth:`feed`; write whatever it
    returns back to the PTY. Keystrokes must not be passed through until
    :attr:`ready` is true — before that the gateway splash could swallow them.
    """

    PROMPT1 = "prompt1"
    PROMPT2 = "prompt2"
    DONE = "done"

    def __init__(self, bootstrap_line: str) -> None:
        self._bootstrap = bootstrap_line.encode("utf-8") + b"\r"
        self.state = self.PROMPT1
        self._all = bytearray()
        self._post_enter = bytearray()

    @property
    def ready(self) -> bool:
        return self.state == self.DONE

    def feed(self, data: bytes) -> bytes:
        if self.state == self.DONE:
            return b""
        if self.state == self.PROMPT1:
            # Accumulate: the prompt line can straddle two PTY reads.
            self._all.extend(data)
            if real_prompt_seen(bytes(self._all)):
                self.state = self.PROMPT2
                self._post_enter.clear()
                return b"\r"
            return b""
        # PROMPT2: only inspect bytes that arrived after the synchronising Enter.
        self._post_enter.extend(data)
        if real_prompt_seen(bytes(self._post_enter)):
            self.state = self.DONE
            return self._bootstrap
        return b""


def build_bootstrap_line(bootstrap_script: str) -> str:
    """Single typed line that decodes and runs the (possibly multiline) script."""

    encoded = base64.b64encode(bootstrap_script.encode("utf-8")).decode("ascii")
    return f"echo {encoded} | base64 -d | bash"


def _set_raw(fd: int) -> Optional[list]:
    try:
        original = termios.tcgetattr(fd)
    except termios.error:
        return None
    attrs = termios.tcgetattr(fd)
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
        return None
    return original


def _restore(fd: int, original: Optional[list]) -> None:
    if original is not None:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except termios.error:
            pass


def _winsize_from(fd: int) -> Optional[tuple[int, int]]:
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    except OSError:
        return None
    rows, cols = struct.unpack("HH", packed[:4])
    return (rows, cols) if rows and cols else None


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class _Bridge:
    def __init__(
        self,
        profile: RemoteProfile,
        bootstrap_script: str,
        *,
        remote_forwards: Optional[list[tuple[int, int]]] = None,
        stdin_fd: int = 0,
        stdout_fd: int = 1,
        prompt_timeout: float = 30.0,
    ) -> None:
        self.profile = profile
        self.bootstrap_script = bootstrap_script
        self.remote_forwards = remote_forwards or []
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.prompt_timeout = prompt_timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._handshake = BootstrapHandshake(build_bootstrap_line(bootstrap_script))
        self._got_prompt_at: Optional[float] = None
        self._ssh_eof = asyncio.Event()

    # ---- fd helpers ---------------------------------------------------------

    def _write(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                # Drop output rather than deadlock the pump; PTY backpressure on a
                # terminal-sized buffer is transient.
                return
            except OSError:
                return
            view = view[written:]

    def _read(self, fd: int) -> Optional[bytes]:
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return b""
        except OSError:
            return None

    # ---- one connection -----------------------------------------------------

    async def _connect_once(self) -> int:
        """Run one ssh connection with handshake; return ssh exit status."""

        master_fd, slave_fd = pty.openpty()
        self._master_fd, self._slave_fd = master_fd, slave_fd
        size = _winsize_from(self.stdout_fd)
        set_winsize(master_fd, *(size or (40, 160)))

        extra: list[str] = []
        for remote_port, local_port in self.remote_forwards:
            extra += [
                "-o",
                "ExitOnForwardFailure=yes",
                "-R",
                f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
            ]
        argv = build_ssh_argv(self.profile)
        # Insert forward/extra flags right after "ssh".
        argv[1:1] = extra
        _set_raw(master_fd)

        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        # The slave is held by ssh; the parent does not need its copy.
        os.close(slave_fd)
        self._slave_fd = None

        self._handshake = BootstrapHandshake(build_bootstrap_line(self.bootstrap_script))
        self._got_prompt_at = None
        self._ssh_eof.clear()
        _set_nonblocking(self.stdin_fd)
        _set_nonblocking(master_fd)

        loop = asyncio.get_running_loop()
        loop.add_reader(self.stdin_fd, self._on_stdin_readable)
        loop.add_reader(master_fd, self._on_master_readable)
        try:
            await self._wait_handshake_or_eof()
            await self._process.wait()
        finally:
            loop.remove_reader(self.stdin_fd)
            loop.remove_reader(master_fd)
            try:
                os.close(master_fd)
            except OSError:
                pass
            self._master_fd = None
        return self._process.returncode if self._process.returncode is not None else -1

    def _on_master_readable(self) -> None:
        assert self._master_fd is not None
        while True:
            chunk = self._read(self._master_fd)
            if chunk is None:
                self._ssh_eof.set()
                return
            if not chunk:
                return
            if not self._handshake.ready:
                injected = self._handshake.feed(chunk)
                if injected:
                    self._write(self._master_fd, injected)
                if self._handshake.ready:
                    # Handshake just completed; nothing from the user was passed
                    # before this point, so no stale input is replayed into tmux.
                    pass
            # Remote output is always forwarded (pre-hook flash included).
            self._write(self.stdout_fd, chunk)

    def _on_stdin_readable(self) -> None:
        assert self._master_fd is not None
        while True:
            chunk = self._read(self.stdin_fd)
            if chunk is None:
                return
            if not chunk:
                return
            # Gate user keys until the bootstrap has been typed so pre-prompt
            # keystrokes cannot be swallowed by the connect splash.
            if self._handshake.ready:
                self._write(self._master_fd, chunk)

    async def _wait_handshake_or_eof(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.prompt_timeout
        while not self._handshake.ready:
            if self._ssh_eof.is_set():
                raise EOFError("ssh closed before remote prompt appeared")
            if loop.time() >= deadline:
                raise TimeoutError("timed out waiting for remote prompt")
            await asyncio.sleep(0.05)

    # ---- driver -------------------------------------------------------------

    async def run(self, reconnect: bool) -> int:
        original_stdin = _set_raw(self.stdin_fd) if os.isatty(self.stdin_fd) else None

        def _on_winch(*_args: object) -> None:
            if self._master_fd is not None:
                size = _winsize_from(self.stdout_fd)
                if size:
                    set_winsize(self._master_fd, *size)

        old_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, _on_winch)
        last_status = 0
        try:
            while True:
                try:
                    last_status = await self._connect_once()
                except (EOFError, TimeoutError) as exc:
                    self._notice(f"remote PTY handshake failed: {exc}")
                    last_status = 255
                if not reconnect:
                    return last_status
                self._notice(
                    f"Remote connection closed with code {last_status}. "
                    "Reconnecting in 3 seconds. Press Ctrl-C to stop."
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
        finally:
            signal.signal(signal.SIGWINCH, old_handler)
            _restore(self.stdin_fd, original_stdin)

    def _notice(self, text: str) -> None:
        try:
            os.write(self.stdout_fd, (f"\r\n{text}\r\n").encode("utf-8"))
        except OSError:
            pass


def _parse_forwards(values: list[str]) -> list[tuple[int, int]]:
    forwards: list[tuple[int, int]] = []
    for value in values:
        remote_port, _, local_port = value.partition(":")
        if not _:
            raise argparse.ArgumentTypeError(
                f"--remote-forward expects REMOTE:LOCAL, got {value!r}"
            )
        forwards.append((int(remote_port), int(local_port)))
    return forwards


def _profile_from_args(args: argparse.Namespace) -> RemoteProfile:
    host = args.target
    user = None
    if "@" in host:
        user, host = host.split("@", 1)
    return RemoteProfile(
        id="pty-gateway",
        name="pty-gateway",
        ssh_host=host,
        user=user,
        port=args.port,
        transport=RemoteTransport.PTY_GATEWAY,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pty_gateway_bridge",
        description="Transparent PTY bridge for interactive SSH gateway hosts.",
    )
    parser.add_argument("--target", required=True, help="ssh alias or user@host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument(
        "--bootstrap-b64",
        required=True,
        help="base64-encoded remote bootstrap script typed after the prompt",
    )
    parser.add_argument(
        "--remote-forward",
        action="append",
        default=[],
        metavar="REMOTE:LOCAL",
        help="reverse forward, repeatable (implies ExitOnForwardFailure)",
    )
    reconnect = parser.add_mutually_exclusive_group()
    reconnect.add_argument("--reconnect", dest="reconnect", action="store_true")
    reconnect.add_argument("--no-reconnect", dest="reconnect", action="store_false")
    parser.set_defaults(reconnect=True)
    parser.add_argument("--prompt-timeout", type=float, default=30.0)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        bootstrap_script = base64.b64decode(args.bootstrap_b64).decode("utf-8")
    except Exception as exc:
        raise SystemExit(f"invalid --bootstrap-b64: {exc}") from exc
    profile = _profile_from_args(args)
    forwards = _parse_forwards(args.remote_forward)
    bridge = _Bridge(
        profile,
        bootstrap_script,
        remote_forwards=forwards,
        prompt_timeout=args.prompt_timeout,
    )
    return asyncio.run(bridge.run(reconnect=args.reconnect))


if __name__ == "__main__":
    raise SystemExit(main())
