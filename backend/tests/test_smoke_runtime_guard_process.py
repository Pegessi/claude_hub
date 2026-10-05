"""Explicit opt-in test with a bounded, cooperative double-fork daemon."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests import smoke_runtime_guard as guard

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("CLAUDE_HUB_RUN_PROCESS_GUARD_TEST") != "1",
    reason="Requires explicit approval for the Linux process-guard test",
)

_DAEMON = textwrap.dedent("""
    import ctypes
    import json
    import os
    import signal
    import sys
    import time

    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.alarm(15)
    sys.path.insert(0, sys.argv[1])
    import smoke_runtime_guard as guard
    controller_pid = int(sys.argv[2])
    if os.fork():
        os._exit(0)
    signal.alarm(15)
    os.setsid()
    if os.fork():
        os._exit(0)
    signal.alarm(15)
    deadline = time.monotonic() + 2
    while os.getppid() != controller_pid:
        if time.monotonic() >= deadline:
            os._exit(3)
        time.sleep(0.01)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, ctypes.c_ulong(signal.SIGKILL), 0, 0, 0) != 0:
        os._exit(4)
    configured = ctypes.c_int(0)
    if libc.prctl(2, ctypes.byref(configured), 0, 0, 0) != 0:
        os._exit(5)
    if configured.value != signal.SIGKILL or os.getppid() != controller_pid:
        os._exit(6)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    identity = guard._identity(os.getpid())
    if identity is None:
        os._exit(7)
    print(json.dumps({"phase": "ready", **identity}), flush=True)
    while True:
        time.sleep(0.1)
    """)

_CONTROLLER = textwrap.dedent("""
    import json
    import os
    import select
    import signal
    import subprocess
    import sys
    import time

    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.alarm(20)
    sys.path.insert(0, sys.argv[1])
    import smoke_runtime_guard as guard

    def line(pipe, timeout):
        deadline = time.monotonic() + timeout
        data = bytearray()
        while len(data) < 4096:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([pipe], [], [], remaining)[0]:
                raise TimeoutError("bounded handshake timed out")
            chunk = os.read(pipe.fileno(), 1)
            if not chunk:
                raise RuntimeError("handshake pipe closed")
            data.extend(chunk)
            if chunk == b"\\n":
                return bytes(data)
        raise ValueError("handshake exceeded size limit")

    guard.require_runtime_capabilities()
    starter = None
    result = None
    try:
        starter = subprocess.Popen(
            [sys.executable, "-I", "-c", DAEMON_CODE, sys.argv[1], str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        ready = json.loads(line(starter.stdout, 4))
        if ready.get("ppid") != os.getpid() or ready.get("phase") != "ready":
            raise RuntimeError("daemon ownership handshake failed")
        print(json.dumps(ready), flush=True)
        if line(sys.stdin.buffer, 4).strip() != b"go":
            raise RuntimeError("parent did not authorize cleanup")
        if starter.wait(timeout=2) != 0:
            raise RuntimeError("starter did not exit normally before cleanup")
    finally:
        result = guard.stop_owned_descendants(
            [starter] if starter is not None else [], term_timeout=0.2, kill_timeout=2
        )
        print(json.dumps(result), flush=True)
    raise SystemExit(0 if result["complete"] else 1)
    """)


def _line(pipe, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([pipe], [], [], remaining)[0]:
            raise TimeoutError("controller handshake timed out")
        chunk = os.read(pipe.fileno(), 1)
        if not chunk:
            raise RuntimeError("controller closed its handshake pipe")
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data)
    raise ValueError("controller handshake exceeded size limit")


def test_subreaper_cleans_double_fork_without_touching_sibling():
    tests_dir = Path(__file__).resolve().parent
    controller_code = "DAEMON_CODE = " + repr(_DAEMON) + "\n" + _CONTROLLER
    controller = sibling = None
    daemon_fd = None
    cleanup_errors = []
    try:
        sibling = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_DFL); time.sleep(30)",
            ]
        )
        controller = subprocess.Popen(
            [sys.executable, "-I", "-c", controller_code, str(tests_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        ready = json.loads(_line(controller.stdout, 4))
        assert ready["phase"] == "ready"
        candidate_fd = os.pidfd_open(ready["pid"], 0)
        try:
            current = guard._identity(ready["pid"])
            if (
                current is None
                or current["start_ticks"] != ready["start_ticks"]
                or current["ppid"] != controller.pid
                or controller.poll() is not None
            ):
                raise RuntimeError("daemon changed before parent acquired its pidfd")
        except BaseException:
            os.close(candidate_fd)
            raise
        daemon_fd = candidate_fd
        controller.stdin.write(b"go\n")
        controller.stdin.flush()
        stdout, stderr = controller.communicate(timeout=6)
        assert controller.returncode == 0, stderr.decode(errors="replace")
        result = json.loads(stdout)
        assert result["complete"] is True
        assert not result["errors"]
        assert len(result["known_returncodes"]) == 1
        assert result["known_returncodes"][0]["returncode"] == 0
        assert ready["pid"] in result["adopted_pids_reaped"]
        assert any(
            action["pid"] == ready["pid"] and action["signal"] == "SIGKILL"
            for action in result["actions"]
        )
        assert select.select([daemon_fd], [], [], 0)[0]
        assert sibling.poll() is None
    finally:
        if daemon_fd is not None:
            try:
                signal.pidfd_send_signal(daemon_fd, signal.SIGKILL, None, 0)
            except ProcessLookupError:
                pass
            except OSError as exc:
                cleanup_errors.append(f"daemon_signal:{type(exc).__name__}")
            finally:
                try:
                    os.close(daemon_fd)
                except OSError as exc:
                    cleanup_errors.append(f"daemon_fd_close:{type(exc).__name__}")
        for process in (controller, sibling):
            if process is None:
                continue
            try:
                if process is controller:
                    if process.stdin is not None and not process.stdin.closed:
                        try:
                            process.stdin.close()
                        except BrokenPipeError:
                            pass
                elif process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=6 if process is controller else 2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_errors.append(f"owned_parent:{type(exc).__name__}")
        assert not cleanup_errors, cleanup_errors
