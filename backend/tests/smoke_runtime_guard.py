"""Private Linux process guard for the opt-in Chat workflow smoke.

Use only in a dedicated controller whose children all belong to this smoke.
Stop its child creators and close third-party runtimes before final cleanup.
Known Popen children have one reaping owner: their Popen object. This guard
handles ordinary fork/exec descendants while the controller remains alive;
it does not promise tree cleanup after the controller is killed by SIGKILL.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PROC_ROOT = Path("/proc")
_MAX_THREADS = 128
_MAX_CHILDREN = 512
# Reserve half the signal budget for KILL so TERM cannot block escalation.
_MAX_ACTIONS = 1024
_MAX_REAPS = 1024
_CHILDREN_BYTES = 64 * 1024
_STAT_BYTES = 4096
_POLL_SECONDS = 0.05


def _error(errors: set[str], stage: str, exc: OSError | ValueError) -> str:
    code = f"{stage}:{type(exc).__name__}:{getattr(exc, 'errno', None)}"
    errors.add(code)
    return code


def _subreaper(enable: bool = False) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    if enable and libc.prctl(36, ctypes.c_ulong(1), 0, 0, 0) != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))
    enabled = ctypes.c_int(0)
    if libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))
    return enabled.value == 1


def require_runtime_capabilities() -> None:
    """Check real kernel support before this controller creates any children."""
    if sys.platform != "linux":
        raise RuntimeError("The live smoke requires Linux")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("The live smoke requires pidfd support")
    descriptor = os.pidfd_open(os.getpid(), 0)
    try:
        signal.pidfd_send_signal(descriptor, 0, None, 0)
    finally:
        os.close(descriptor)
    if not _subreaper(enable=True):
        raise RuntimeError("Child subreaper could not be verified")
    children = _PROC_ROOT / str(os.getpid()) / "task" / str(os.getpid()) / "children"
    try:
        with children.open("rb") as source:
            raw = source.read(_CHILDREN_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("The live smoke requires readable proc children files") from exc
    if len(raw) > _CHILDREN_BYTES:
        raise RuntimeError("The controller children file exceeds the smoke scan budget")


def _identity(pid: int) -> dict[str, Any] | None:
    try:
        with (_PROC_ROOT / str(pid) / "stat").open("rb") as source:
            raw = source.read(_STAT_BYTES + 1)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return None
        raise
    end = raw.rfind(b")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    if len(raw) > _STAT_BYTES or len(fields) < 20:
        raise ValueError("Invalid or oversized proc stat")
    return {
        "pid": pid,
        "ppid": int(fields[1]),
        "start_ticks": int(fields[19]),
        "state": fields[0].decode("ascii"),
    }


def _direct_children(
    controller_pid: int, deadline: float, errors: set[str]
) -> tuple[list[dict[str, Any]], bool]:
    children: list[dict[str, Any]] = []
    seen: set[int] = set()
    complete = True
    thread_count = 0
    try:
        with os.scandir(_PROC_ROOT / str(controller_pid) / "task") as threads:
            for thread in threads:
                if not thread.name.isdigit():
                    continue
                thread_count += 1
                if time.monotonic() >= deadline or thread_count > _MAX_THREADS:
                    errors.add("scan:thread_or_time_budget")
                    return children, False
                try:
                    with (Path(thread.path) / "children").open("rb") as source:
                        raw = source.read(_CHILDREN_BYTES + 1)
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        continue
                    _error(errors, "children_read", exc)
                    complete = False
                    continue
                if len(raw) > _CHILDREN_BYTES:
                    errors.add("scan:children_byte_budget")
                    return children, False
                for token in raw.split():
                    if time.monotonic() >= deadline:
                        errors.add("scan:time_budget")
                        return children, False
                    try:
                        pid = int(token)
                        if pid <= 0 or pid == controller_pid:
                            raise ValueError("Invalid child PID")
                    except ValueError as exc:
                        _error(errors, "children_parse", exc)
                        complete = False
                        continue
                    if pid in seen:
                        continue
                    if len(seen) >= _MAX_CHILDREN:
                        errors.add("scan:child_budget")
                        return children, False
                    seen.add(pid)
                    try:
                        identity = _identity(pid)
                    except (OSError, ValueError) as exc:
                        _error(errors, "identity_read", exc)
                        complete = False
                        continue
                    if identity is None:
                        continue
                    if identity["ppid"] != controller_pid:
                        errors.add("scan:parent_changed")
                        complete = False
                        continue
                    children.append(identity)
    except OSError as exc:
        _error(errors, "threads_read", exc)
        complete = False
    return children, complete


def _signal_direct(
    identity: dict[str, Any], signum: signal.Signals, controller_pid: int, errors: set[str]
) -> dict[str, Any]:
    action = {
        "pid": identity["pid"],
        "start_ticks": identity["start_ticks"],
        "signal": signum.name,
        "outcome": "unknown",
    }
    try:
        descriptor = os.pidfd_open(int(identity["pid"]), 0)
    except ProcessLookupError:
        action["outcome"] = "already_exited"
        return action
    except OSError as exc:
        action["outcome"] = "pidfd_error"
        action["error"] = _error(errors, "pidfd_open", exc)
        return action
    try:
        try:
            current = _identity(int(identity["pid"]))
        except (OSError, ValueError) as exc:
            action["outcome"] = "identity_read_error"
            action["error"] = _error(errors, "identity_read", exc)
            return action
        if current is None:
            action["outcome"] = "already_exited"
            return action
        if current["start_ticks"] != identity["start_ticks"] or current["ppid"] != controller_pid:
            action["outcome"] = "identity_changed"
            errors.add("signal:identity_changed")
            return action
        try:
            signal.pidfd_send_signal(descriptor, signum, None, 0)
            action["outcome"] = "sent"
        except ProcessLookupError:
            action["outcome"] = "already_exited"
        except OSError as exc:
            action["outcome"] = "signal_error"
            action["error"] = _error(errors, "signal", exc)
        return action
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            action["close_error"] = _error(errors, "pidfd_close", exc)


def _poll_known(processes: list[subprocess.Popen[Any]], deadline: float, errors: set[str]) -> bool:
    finished = True
    for process in processes:
        if process.returncode is not None:
            continue
        if time.monotonic() >= deadline:
            errors.add("poll:time_budget")
            return False
        try:
            if process.poll() is None:
                finished = False
        except OSError as exc:
            _error(errors, "poll", exc)
            finished = False
    return finished


def _reap_adopted(deadline: float, errors: set[str], reaped: list[int]) -> bool:
    """Called only after every known Popen has collected its own exit status."""
    while len(reaped) < _MAX_REAPS:
        if time.monotonic() >= deadline:
            errors.add("reap:time_budget")
            return False
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError as exc:
            _error(errors, "reap", exc)
            return False
        if pid == 0:
            return False
        reaped.append(pid)
    errors.add("reap:count_budget")
    return False


def _phase(
    signum: signal.Signals,
    processes: list[subprocess.Popen[Any]],
    deadline: float,
    errors: set[str],
    actions: list[dict[str, Any]],
    reaped: list[int],
) -> tuple[bool, list[dict[str, Any]]]:
    attempted: set[tuple[int, int]] = set()
    phase_start = len(actions)
    remaining: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        known_finished = _poll_known(processes, deadline, errors)
        no_children = _reap_adopted(deadline, errors, reaped) if known_finished else False
        remaining, scan_complete = _direct_children(os.getpid(), deadline, errors)
        if known_finished and no_children and scan_complete and not remaining:
            return True, remaining
        for child in remaining:
            if time.monotonic() >= deadline:
                break
            key = (int(child["pid"]), int(child["start_ticks"]))
            if key in attempted or child["state"] == "Z":
                continue
            if len(actions) - phase_start >= _MAX_ACTIONS // 2:
                errors.add(f"signal:{signum.name}:count_budget")
                return False, remaining
            attempted.add(key)
            actions.append(_signal_direct(child, signum, os.getpid(), errors))
        time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return False, remaining


def stop_owned_descendants(
    processes: list[subprocess.Popen[Any]], term_timeout: float = 10, kill_timeout: float = 5
) -> dict[str, Any]:
    """Best-effort bounded cleanup; errors prevent a verified-success result.

    Limits bound Python work and reads, not an individual stalled kernel call.
    A TERM phase timeout permits the planned KILL escalation. Failure to verify
    the final state, or any non-benign operation error, keeps complete false.
    """
    processes = list(processes)
    errors: set[str] = set()
    actions: list[dict[str, Any]] = []
    reaped: list[int] = []
    if not math.isfinite(term_timeout) or term_timeout < 0:
        errors.add("timeout:term")
        term_timeout = 10
    if not math.isfinite(kill_timeout) or kill_timeout < 0:
        errors.add("timeout:kill")
        kill_timeout = 5
    try:
        if not _subreaper():
            errors.add("subreaper:disabled")
    except OSError as exc:
        _error(errors, "subreaper_check", exc)
    empty, remaining = _phase(
        signal.SIGTERM, processes, time.monotonic() + term_timeout, errors, actions, reaped
    )
    if not empty:
        empty, remaining = _phase(
            signal.SIGKILL, processes, time.monotonic() + kill_timeout, errors, actions, reaped
        )
    if not empty:
        errors.add("cleanup:incomplete")
    return {
        "complete": empty and not errors,
        "remaining_at_last_scan": remaining,
        "known_returncodes": [
            {"pid": process.pid, "returncode": process.returncode} for process in processes
        ],
        "actions": actions,
        "adopted_pids_reaped": reaped,
        "errors": sorted(errors),
    }
