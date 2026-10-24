#!/usr/bin/env python3
"""Own one verify target's processes and disposable runtime, not its evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_RUNTIME_KEYS = (
    "HOME",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "CLAUDE_HUB_HOME",
    "CLAUDE_HUB_STATE_ROOT",
)


def emit(message: str, *, error: bool = False) -> None:
    """Keep a closed console pipe from aborting cleanup or replacing test status."""
    stream = sys.stderr if error else sys.stdout
    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        # Python flushes streams again at interpreter shutdown. Redirect the
        # failed descriptor too, otherwise that flush can replace the exit code.
        try:
            descriptor = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(descriptor, stream.fileno())
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            pass


def _beneath(value: str, root: Path) -> bool:
    path = Path(os.path.normpath(value.removesuffix(" (deleted)")))
    return path.is_absolute() and path.is_relative_to(root)


def runtime_users(root: Path, run_id: str) -> list[int]:
    """Find visible Linux processes still referring to this run, without logging env."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []  # Other POSIX hosts still have the owned process-group check.
    users: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            values = dict(
                item.split("=", 1)
                for item in (entry / "environ").read_bytes().decode(errors="replace").split("\0")
                if "=" in item
            )
            owned = values.get("_CLAUDE_HUB_VERIFY_RUN_ID") == run_id
            owned = owned or any(_beneath(values.get(key, ""), root) for key in _RUNTIME_KEYS)
            if not owned:
                owned = _beneath(os.readlink(entry / "cwd"), root)
            if not owned:
                for descriptor in (entry / "fd").iterdir():
                    try:
                        if _beneath(os.readlink(descriptor), root):
                            owned = True
                            break
                    except OSError:
                        continue
            if owned:
                users.append(int(entry.name))
        except (OSError, ValueError):
            # Exited or inaccessible unrelated processes are not ownership proof.
            continue
    return users


def group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    # A zombie holds no runtime files and cannot be killed again. Linux PID 1
    # may reap it later; do not turn that delay into permanent sandbox retention.
    proc = Path("/proc")
    if proc.is_dir():
        found = False
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group:
                    found = True
                    if fields[0] != "Z":
                        return True
            except (OSError, ValueError, IndexError):
                continue
        if found:
            return False
        # An incomplete /proc view is not proof of exit. Recheck the group in
        # case its last member exited while the process list was being read.
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
    return True


def signal_group(group: int, signum: int) -> None:
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass


def stop_group(process: subprocess.Popen[bytes], first_signal: int = signal.SIGTERM) -> bool:
    """Stop only descendants remaining in the session created for this target."""
    if group_alive(process.pid):
        signal_group(process.pid, first_signal)
        deadline = time.monotonic() + 3
        while group_alive(process.pid) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.05)
    if group_alive(process.pid):
        signal_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return False
    deadline = time.monotonic() + 3
    while group_alive(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not group_alive(process.pid)


def interrupt_target(process: subprocess.Popen[bytes], target: str, signum: int) -> bool:
    """Let the target's own finalizers run before terminating residual children."""
    if target in {"backend-tests", "terminal-hmr", "terminal-replay"}:
        # The leader is Bash waiting for a uv/pytest pipeline. SIGINT reaches
        # pytest's KeyboardInterrupt cleanup; SIGTERM would skip its finalizers.
        # The outer command still returns the original requested signal status.
        signal_group(process.pid, signal.SIGINT)
    else:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
    # The browser owner may spend 25s on a runner and 25s on Vite. Keep time for
    # its summary too; once the leader exits, residual group cleanup stays short.
    deadline = time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return stop_group(process)


def cleanup_runtime(
    root: Path, identity: tuple[int, int], run_id: str, *, keep: bool, processes_stopped: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(root), "state": "retained"}
    if not processes_stopped:
        return {**result, "reason": "owned_process_group_still_alive"}
    if keep:
        return {**result, "reason": "explicit_keep"}
    try:
        current = root.lstat()
    except FileNotFoundError:
        return {**result, "state": "absent"}
    except OSError as exc:
        return {**result, "reason": "cleanup_failed", "error": type(exc).__name__}
    try:
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            return {**result, "reason": "runtime_identity_changed"}
        users = runtime_users(root, run_id)
        if users:
            return {**result, "reason": "runtime_still_in_use", "pids": users}
        shutil.rmtree(root)
    except OSError as exc:
        return {**result, "reason": "cleanup_failed", "error": type(exc).__name__}
    return {**result, "state": "removed"}


def write_report(evidence: Path, report: dict[str, Any]) -> None:
    """Replace rather than follow a report-file symlink left by a child."""
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", dir=evidence)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(report, indent=2) + "\n")
        os.replace(temporary, evidence / "runtime.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def finish_report(evidence: Path, report: dict[str, Any], status: int) -> int:
    try:
        write_report(evidence, report)
    except OSError as exc:
        status = status or 1
        emit(f"WARNING: could not write runtime report: {type(exc).__name__}", error=True)
    try:
        # Do not follow a child-created exit-code symlink either.
        descriptor, temporary = tempfile.mkstemp(prefix=".exit-", dir=evidence)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(f"{status}\n")
            os.replace(temporary, evidence / "exit-code")
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError as exc:
        status = status or 1
        emit(f"WARNING: could not write exit code: {type(exc).__name__}", error=True)
    if report["exit_code"] != status:
        report["exit_code"] = status
        try:
            write_report(evidence, report)
        except OSError:
            pass  # The original target failure still takes precedence.
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a target command is required")
    keep_value = os.environ.get("CLAUDE_HUB_VERIFY_KEEP_RUNTIME", "0")
    if keep_value not in {"0", "1"}:
        parser.error("CLAUDE_HUB_VERIFY_KEEP_RUNTIME must be 0 or 1")
    if args.target in {"backend-tests", "terminal-hmr", "terminal-replay"} and os.environ.get(
        "CLAUDE_HUB_TEST_BACKEND_URL"
    ):
        parser.error(f"{args.target} must not use an external backend")
    parent = Path(
        os.environ.get("CLAUDE_HUB_VERIFY_ARTIFACT_ROOT") or os.environ.get("TMPDIR") or "/tmp"
    )
    if not parent.is_dir():
        parser.error(f"artifact parent does not exist: {parent}")
    prefix = "claude-hub-browser-mocks." if args.target == "browser-mocks" else "claude-hub-verify."
    interrupted: list[int] = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def request_stop(signum: int, _frame: Any) -> None:
        # Record only, including during creation/reporting. Raising here would
        # abandon a just-created path before its owner can save the identity.
        if not interrupted:
            interrupted.append(signum)

    evidence: Path | None = None
    runtime: Path | None = None
    identity: tuple[int, int] | None = None
    process: subprocess.Popen[bytes] | None = None
    run_id = uuid.uuid4().hex
    report: dict[str, Any] = {"schema": 1, "target": args.target, "state": "running"}
    status = final_status = 1
    stopped = True
    error: str | None = None
    for sig in previous:
        signal.signal(sig, request_stop)
    try:
        evidence = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
        # Unix-domain sockets need a short path, even for deep evidence roots.
        runtime = Path(tempfile.mkdtemp(prefix="ch-v-", dir="/tmp"))
        created = runtime.stat()
        identity = (created.st_dev, created.st_ino)
        env = dict(os.environ)
        env.update(
            {
                "_CLAUDE_HUB_VERIFY_OWNER_PID": str(os.getpid()),
                "_CLAUDE_HUB_VERIFY_RUNTIME": str(runtime),
                "_CLAUDE_HUB_VERIFY_EVIDENCE": str(evidence),
                "_CLAUDE_HUB_VERIFY_RUN_ID": run_id,
            }
        )
        report.update(
            runtime=str(runtime),
            owner_pid=os.getpid(),
            run_id=run_id,
            process_inspection=(
                "linux_proc" if Path("/proc").is_dir() else "owned_process_group_only"
            ),
        )
        write_report(evidence, report)
        emit(f"Verify evidence: {evidence}; disposable runtime: {runtime}")
        if not interrupted:
            process = subprocess.Popen(command, env=env, start_new_session=True)
            report["pid"] = process.pid
            write_report(evidence, report)
            while process.poll() is None:
                if interrupted:
                    stopped = interrupt_target(process, args.target, interrupted[0])
                    break
                time.sleep(0.05)
            # stop_group may time out on an uninterruptible child. Keep that
            # runtime and return the signal status instead of waiting forever.
            status = process.returncode if process.returncode is not None else 1
            if status < 0:
                status = 128 - status
        if interrupted:
            status = 128 + interrupted[0]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if interrupted:
            status = 128 + interrupted[0]
        emit(error, error=True)
    finally:
        try:
            if process is not None:
                try:
                    stopped = stop_group(process) and stopped
                except OSError as exc:
                    stopped = False
                    error = error or f"process cleanup failed: {type(exc).__name__}"
                    emit(error, error=True)
            cleanup: dict[str, Any] = {"state": "not_created"}
            if runtime is not None:
                if identity is None:
                    cleanup = {"state": "retained", "reason": "runtime_identity_unknown"}
                else:
                    cleanup = cleanup_runtime(
                        runtime, identity, run_id, keep=keep_value == "1", processes_stopped=stopped
                    )
            clean = (
                cleanup["state"] in {"removed", "absent", "not_created"}
                or cleanup.get("reason") == "explicit_keep"
            )
            if status == 0 and interrupted:
                status = 128 + interrupted[0]
            final_status = status if status != 0 else 0 if clean else 1
            report.update(
                state="finished",
                command_exit_code=status,
                exit_code=final_status,
                process_group_stopped=stopped,
                cleanup=cleanup,
                error=error,
            )
            if evidence is not None:
                final_status = finish_report(evidence, report, final_status)
            emit(f"Verify runtime {cleanup['state']}: {runtime}; evidence retained at {evidence}")
            if not clean:
                emit(f"WARNING: runtime retained: {cleanup.get('reason')}", error=True)
        finally:
            # Publication may itself be slow. Do not ignore a signal recorded
            # after the first status calculation, or replace an existing failure.
            if final_status == 0 and interrupted:
                final_status = 128 + interrupted[0]
                report["exit_code"] = final_status
                if evidence is not None:
                    final_status = finish_report(evidence, report, final_status)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return final_status


if __name__ == "__main__":
    raise SystemExit(main())
