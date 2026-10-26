#!/usr/bin/env python3
"""Own one verify target's processes and disposable runtime, not its evidence."""

from __future__ import annotations

import argparse
import ctypes
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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SAFE_RMTREE = shutil.rmtree.avoids_symlink_attacks

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


def poll_target(process: subprocess.Popen[bytes]) -> int | None:
    """Observe exit without releasing the PID used for later group signals."""
    if process.returncode is not None:
        return process.returncode
    result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def stop_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop the anchored group; the caller reaps its leader only afterwards."""
    if group_alive(process.pid):
        signal_group(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    if group_alive(process.pid):
        signal_group(process.pid, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while group_alive(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not group_alive(process.pid)


def interrupt_target(process: subprocess.Popen[bytes], target: str, signum: int) -> bool:
    """Let target finalizers run, without reaping the session leader."""
    if target in {"backend-tests", "terminal-hmr", "terminal-replay"}:
        # Bash waits for the pipeline; pytest finalizes on INT, not default TERM.
        signal_group(process.pid, signal.SIGINT)
    else:
        # Popen.send_signal() calls poll() and could release this PID too soon.
        try:
            os.kill(process.pid, signum)
        except ProcessLookupError:
            pass
    # Browser cleanup budgets are 25s for the runner and 25s for Vite.
    deadline = time.monotonic() + 60
    while poll_target(process) is None and time.monotonic() < deadline:
        live_owned_children(exclude=process.pid)
        time.sleep(0.05)
    return poll_target(process) is not None


def child_subreaper(enabled: bool | None = None) -> int | None:
    """Get/set the Linux process-local orphan-child adoption flag."""
    if not sys.platform.startswith("linux"):
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if enabled is not None and libc.prctl(36, int(enabled), 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return previous.value


def live_owned_children(exclude: int | None = None) -> list[int]:
    """Find adopted children even when they deny /proc environ access."""
    if not sys.platform.startswith("linux"):
        return []
    membership = Path(f"/proc/self/task/{os.getpid()}/children")
    for _ in range(3):
        children = [int(value) for value in membership.read_text().split()]
        live: list[int] = []
        reaped = False
        for pid in children:
            if pid == exclude:
                continue
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            except FileNotFoundError:
                continue
            except (OSError, IndexError):
                live.append(pid)
                continue
            if fields[0] != "Z":
                live.append(pid)
            else:
                try:
                    reaped = bool(os.waitpid(pid, os.WNOHANG)[0]) or reaped
                except ChildProcessError:
                    pass
        if not reaped:
            return live
        # Re-read membership after reaping: descendants can have been adopted
        # while a previous child was exiting. Never reap the target anchor here.
    return [int(value) for value in membership.read_text().split() if int(value) != exclude]


def directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def cleanup_runtime(
    root: Path,
    identity: tuple[int, int],
    run_id: str,
    *,
    keep: bool,
    processes_stopped: bool,
    owned_children: list[int] | None = None,
    runtime_fd: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(root), "state": "retained"}
    if not processes_stopped:
        return {**result, "reason": "owned_process_group_still_alive"}
    if owned_children:
        return {**result, "reason": "runtime_still_in_use", "pids": owned_children}
    try:
        if not root.exists() and not root.is_symlink():
            return {**result, "state": "absent"}
        if directory_identity(root) != identity:
            return {**result, "reason": "runtime_identity_changed"}
        descriptor = (
            os.dup(runtime_fd)
            if runtime_fd is not None
            else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        )
        try:
            pinned = os.fstat(descriptor)
            if (pinned.st_dev, pinned.st_ino) != identity:
                return {**result, "reason": "runtime_identity_changed"}
            users = sorted(set(runtime_users(root, run_id)) | set(owned_children or []))
            if users:
                return {**result, "reason": "runtime_still_in_use", "pids": users}
            if directory_identity(root) != identity:
                return {**result, "reason": "runtime_identity_changed"}
            if keep:
                return {**result, "reason": "explicit_keep"}
            if not _SAFE_RMTREE:
                return {**result, "reason": "safe_rmtree_unavailable"}
            # Never reopen root by pathname for recursion: it may be replaced
            # after any identity check. All destructive traversal stays on this fd.
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.name, dir_fd=descriptor)
                    else:
                        os.unlink(entry.name, dir_fd=descriptor)
            if directory_identity(root) != identity:
                return {**result, "reason": "runtime_identity_changed"}
            # POSIX has no conditional-by-inode rmdir. This last operation can
            # remove only an empty name; it never traverses a replacement tree.
            root.rmdir()
        finally:
            os.close(descriptor)
    except OSError as exc:
        return {**result, "reason": "cleanup_failed", "error": type(exc).__name__}
    return {**result, "state": "removed"}


@contextmanager
def directory_descriptor(directory: Path | int) -> Iterator[int]:
    descriptor = (
        os.dup(directory)
        if isinstance(directory, int)
        else os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def replace_evidence(directory: Path | int, name: str, content: str) -> None:
    """Publish within the pinned directory, never through its mutable pathname."""
    with directory_descriptor(directory) as parent:
        temporary = f".{name}-{uuid.uuid4().hex}"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def write_report(evidence: Path | int, report: dict[str, Any]) -> None:
    replace_evidence(evidence, "runtime.json", json.dumps(report, indent=2) + "\n")


def finish_report(evidence: Path | int, report: dict[str, Any], status: int) -> int:
    try:
        write_report(evidence, report)
    except OSError as exc:
        status = status or 1
        emit(f"WARNING: could not write runtime report: {type(exc).__name__}", error=True)
    try:
        replace_evidence(evidence, "exit-code", f"{status}\n")
    except OSError as exc:
        status = status or 1
        emit(f"WARNING: could not write exit code: {type(exc).__name__}", error=True)
    if report["exit_code"] != status:
        report["exit_code"] = status
        try:
            write_report(evidence, report)
        except OSError:
            pass
    return status


def record_pytest_status(arguments: list[str]) -> int:
    """Publish Bash's status receipts through the inherited evidence directory fd."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pytest_status", type=int)
    parser.add_argument("log_status", type=int)
    parser.add_argument("junit_status", type=int)
    parser.add_argument("has_junit", choices=("0", "1"))
    args = parser.parse_args(arguments)
    try:
        descriptor = int(os.environ["_CLAUDE_HUB_VERIFY_EVIDENCE_FD"])
        replace_evidence(descriptor, "pytest-exit-code", f"{args.pytest_status}\n")
        replace_evidence(descriptor, "log-exit-code", f"{args.log_status}\n")
        if args.has_junit == "1":
            replace_evidence(descriptor, "junit-validation-exit-code", f"{args.junit_status}\n")
    except (OSError, ValueError, KeyError) as exc:
        emit(f"WARNING: cannot publish pytest status: {type(exc).__name__}", error=True)
        return 1
    return 0


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
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        parser.error("verification ownership requires waitid/WNOWAIT support")
    parent = Path(
        os.environ.get("CLAUDE_HUB_VERIFY_ARTIFACT_ROOT") or os.environ.get("TMPDIR") or "/tmp"
    )
    if not parent.is_dir():
        parser.error(f"artifact parent does not exist: {parent}")
    prefix = "claude-hub-browser-mocks." if args.target == "browser-mocks" else "claude-hub-verify."
    interrupted: list[int] = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    previous_sigchld = signal.getsignal(signal.SIGCHLD)

    def request_stop(signum: int, _frame: Any) -> None:
        if not interrupted:
            interrupted.append(signum)

    evidence: Path | None = None
    evidence_fd: int | None = None
    evidence_identity: tuple[int, int] | None = None
    runtime: Path | None = None
    runtime_fd: int | None = None
    identity: tuple[int, int] | None = None
    process: subprocess.Popen[bytes] | None = None
    previous_subreaper: int | None = None
    run_id = uuid.uuid4().hex
    report: dict[str, Any] = {"schema": 1, "target": args.target, "state": "running"}
    status = final_status = 1
    stopped = True
    error: str | None = None
    for sig in previous:
        signal.signal(sig, request_stop)
    try:
        # An inherited SIGCHLD ignore disposition would auto-reap the PID anchor.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        # This CLI owns one fresh process tree. Linux adoption closes the gap
        # left by detached children that deliberately deny /proc inspection.
        previous_subreaper = child_subreaper(True)
        evidence = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
        evidence_fd = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        pinned_evidence = os.fstat(evidence_fd)
        evidence_identity = (pinned_evidence.st_dev, pinned_evidence.st_ino)
        runtime = Path(tempfile.mkdtemp(prefix="ch-v-", dir="/tmp"))
        # Keep the inode allocated throughout the run, not only while deleting.
        # A deleted/recreated pathname cannot reuse this pinned inode identity.
        runtime_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        created = os.fstat(runtime_fd)
        identity = (created.st_dev, created.st_ino)
        env = dict(os.environ)
        env.update(
            {
                "_CLAUDE_HUB_VERIFY_OWNER_PID": str(os.getpid()),
                "_CLAUDE_HUB_VERIFY_RUNTIME": str(runtime),
                "_CLAUDE_HUB_VERIFY_EVIDENCE": str(evidence),
                "_CLAUDE_HUB_VERIFY_EVIDENCE_FD": str(evidence_fd),
                "_CLAUDE_HUB_VERIFY_RUN_ID": run_id,
            }
        )
        report.update(
            runtime=str(runtime),
            owner_pid=os.getpid(),
            run_id=run_id,
            process_inspection=(
                "linux_subreaper_and_proc"
                if previous_subreaper is not None
                else "owned_process_group_only"
            ),
        )
        write_report(evidence_fd, report)
        emit(f"Verify evidence: {evidence}; disposable runtime: {runtime}")
        if not interrupted:
            process = subprocess.Popen(
                command, env=env, start_new_session=True, pass_fds=(evidence_fd,)
            )
            report["pid"] = process.pid
            write_report(evidence_fd, report)
            while (observed := poll_target(process)) is None:
                # A nested owner may wait for its group to disappear. Reap dead
                # adoptees during execution too, without releasing our PID anchor.
                live_owned_children(exclude=process.pid)
                if interrupted:
                    interrupt_target(process, args.target, interrupted[0])
                    observed = poll_target(process)
                    break
                time.sleep(0.05)
            status = observed if observed is not None else 1
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
            owned_children: list[int] = []
            if process is not None:
                try:
                    # All group operations happen while this leader PID is still
                    # reserved. Neither poll_target nor interrupt_target reaps it.
                    stopped = stop_group(process)
                    owned_children = live_owned_children(exclude=process.pid)
                except OSError as exc:
                    stopped = False
                    error = error or f"process cleanup failed: {type(exc).__name__}"
                    emit(error, error=True)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    stopped = False
                # No operation below this point may signal the old PID or PGID.
            cleanup: dict[str, Any] = {"state": "not_created"}
            if runtime is not None:
                if identity is None:
                    cleanup = {"state": "retained", "reason": "runtime_identity_unknown"}
                else:
                    cleanup = cleanup_runtime(
                        runtime,
                        identity,
                        run_id,
                        keep=keep_value == "1",
                        processes_stopped=stopped,
                        owned_children=owned_children,
                        runtime_fd=runtime_fd,
                    )
            clean = (
                cleanup["state"] in {"removed", "absent", "not_created"}
                or cleanup.get("reason") == "explicit_keep"
            )
            if status == 0 and interrupted:
                status = 128 + interrupted[0]
            evidence_changed = (
                evidence is not None and directory_identity(evidence) != evidence_identity
            )
            final_status = status if status != 0 else 0 if clean and not evidence_changed else 1
            report.update(
                state="finished",
                command_exit_code=status,
                exit_code=final_status,
                process_group_stopped=stopped,
                owned_children=owned_children,
                evidence_path_changed=evidence_changed,
                cleanup=cleanup,
                error=error,
            )
            if evidence_fd is not None:
                final_status = finish_report(evidence_fd, report, final_status)
            location = (
                "the pinned original directory (path changed)"
                if evidence_changed
                else str(evidence)
            )
            emit(f"Verify runtime {cleanup['state']}: {runtime}; evidence retained at {location}")
            if not clean:
                emit(f"WARNING: runtime retained: {cleanup.get('reason')}", error=True)
            if evidence_changed:
                emit(f"WARNING: evidence directory identity changed: {evidence}", error=True)
        finally:
            report_changed = False
            if previous_subreaper is not None:
                try:
                    child_subreaper(bool(previous_subreaper))
                except OSError as exc:
                    final_status = final_status or 1
                    report["subreaper_restore_error"] = type(exc).__name__
                    report_changed = True
                    emit(
                        f"WARNING: cannot restore child adoption flag: {type(exc).__name__}",
                        error=True,
                    )
            if runtime_fd is not None:
                try:
                    os.close(runtime_fd)
                except OSError as exc:
                    final_status = final_status or 1
                    report["runtime_descriptor_close_error"] = type(exc).__name__
                    report_changed = True
            signal.signal(signal.SIGCHLD, previous_sigchld)
            # Keep the evidence fd until status-changing teardown is complete.
            # Publication can be slow; include late cancellation and path changes
            # without replacing an existing nonzero target status.
            if final_status == 0 and interrupted:
                final_status = 128 + interrupted[0]
            if evidence is not None and evidence_identity is not None:
                if directory_identity(evidence) != evidence_identity:
                    report_changed = (
                        report.get("evidence_path_changed") is not True or report_changed
                    )
                    report["evidence_path_changed"] = True
                    final_status = final_status or 1
            if evidence_fd is not None:
                try:
                    if report_changed or report.get("exit_code") != final_status:
                        report["exit_code"] = final_status
                        final_status = finish_report(evidence_fd, report, final_status)
                finally:
                    os.close(evidence_fd)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return final_status


if __name__ == "__main__":
    if sys.argv[1:2] == ["--record-pytest"]:
        raise SystemExit(record_pytest_status(sys.argv[2:]))
    raise SystemExit(main())
