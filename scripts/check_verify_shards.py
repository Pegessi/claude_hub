#!/usr/bin/env python3
"""Run the default backend pytest scope in isolated, coverage-enabled processes."""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

IGNORES = (
    "--ignore=tests/test_terminal_replay.py",
    "--ignore=tests/test_terminal_input_latency_perf.py",
    "--ignore=tests/test_terminal_hmr_recovery_e2e.py",
)


class Collection:
    def __init__(self, destination: Path, expected: Path | None = None) -> None:
        self.destination = destination
        self.expected = expected

    def pytest_collection_finish(self, session: Any) -> None:
        items = [{"nodeid": item.nodeid, "file": str(item.path)} for item in session.items]
        self.destination.write_text(json.dumps(items), encoding="utf-8")
        if self.expected is not None:
            expected = json.loads(self.expected.read_text(encoding="utf-8"))
            actual_counts = collections.Counter(item["nodeid"] for item in items)
            expected_counts = collections.Counter(item["nodeid"] for item in expected)
            if actual_counts != expected_counts:
                raise RuntimeError("shard collection differs from the full-suite manifest")

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.expected is None:
            return
        stats = {
            name: len(terminalreporter.stats.get(name, []))
            for name in ("passed", "skipped", "xfailed", "xpassed")
        }
        self.destination.with_suffix(".stats.json").write_text(json.dumps(stats), encoding="utf-8")


def isolated_env(runtime: Path, coverage: Path, index: str) -> dict[str, str]:
    env = os.environ.copy()
    names = {
        "HOME": "home",
        "TMPDIR": "tmp",
        "XDG_CONFIG_HOME": "config",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "XDG_RUNTIME_DIR": "xdg-runtime",
        "CLAUDE_HUB_HOME": "hub",
        "CLAUDE_HUB_STATE_ROOT": "hub-state",
    }
    for key, name in names.items():
        path = runtime / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    env["CLAUDE_HUB_TMUX_SOCKET"] = f"ch-shard-{index}-{uuid.uuid4().hex[:12]}"
    env["COVERAGE_FILE"] = str(coverage)
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        env.pop(key, None)
    return env


def partition(items: list[dict[str, str]], count: int) -> list[list[dict[str, str]]]:
    files: dict[str, list[dict[str, str]]] = {}
    for item in items:
        files.setdefault(item["file"], []).append(item)
    # Test count is a portable initial estimate; large files are scheduled first.
    bins: list[list[dict[str, str]]] = [[] for _ in range(min(count, len(files)))]
    for group in sorted(files.values(), key=lambda group: -len(group)):
        min(bins, key=len).extend(group)
    return bins


def status(proc: subprocess.Popen[Any]) -> int | None:
    # Observe without reaping: the leader PID remains reserved until its entire
    # process group has been signalled, even if pytest exited before a child.
    result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None:
        return None
    if result.si_code == os.CLD_EXITED:
        return result.si_status
    return 128 + result.si_status


_SUBREAPER = False


def enable_subreaper() -> None:
    """Adopt escaped descendants before starting any shard."""
    global _SUBREAPER
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    _SUBREAPER = True


def reap_adopted_zombies(leaders: list[subprocess.Popen[Any]]) -> None:
    """Reap exited adoptees during execution, retaining every worker PID anchor."""
    if not _SUBREAPER:
        return
    protected = {proc.pid for proc in leaders}
    membership = Path(f"/proc/self/task/{os.getpid()}/children")
    while True:
        reaped = False
        for value in membership.read_text().split():
            pid = int(value)
            if pid in protected:
                continue
            try:
                result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if result is not None:
                    os.waitpid(pid, 0)
                    reaped = True
            except ChildProcessError:
                continue
        if not reaped:
            return


def cleanup_adopted() -> None:
    """Terminate and reap only verified adopted children, without PID reuse races."""
    if not _SUBREAPER:
        return
    membership = Path(f"/proc/self/task/{os.getpid()}/children")
    deadline = time.monotonic() + 5
    sent: dict[int, float] = {}
    while True:
        pids = [int(value) for value in membership.read_text().split()]
        if not pids:
            return
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(f"unable to clean up owned descendant processes: {pids}")
        for pid in pids:
            try:
                # The unreaped child remains a PID anchor between this check and
                # kill; SIGCHLD is default and no other thread reaps children.
                result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                continue
            if result is not None:
                os.waitpid(pid, 0)
                sent.pop(pid, None)
                continue
            first = sent.setdefault(pid, now)
            signum = signal.SIGKILL if now - first >= 0.5 else signal.SIGTERM
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
        time.sleep(0.05)


def stop(processes: list[subprocess.Popen[Any]], *, graceful: bool = False) -> None:
    # Leave time for pytest fixture finalizers before escalating. The runtime
    # owner allows 60 seconds for the whole target to shut down.
    if graceful:
        for proc in processes:
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline and any(status(proc) is None for proc in processes):
            reap_adopted_zombies(processes)
            time.sleep(0.05)
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for proc in processes:
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass
        if signum == signal.SIGTERM and processes:
            time.sleep(0.2)
    for proc in processes:
        proc.wait()
    # Never signal a leader again once its PID anchor has been reaped, even
    # if descendant cleanup raises and the outer finally retries cleanup.
    processes.clear()
    cleanup_adopted()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--internal", choices=("collect", "worker"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--actual", type=Path)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    backend, runtime, evidence = (p.resolve() for p in (args.backend, args.runtime, args.evidence))
    if args.internal:
        import pytest

        assert args.actual is not None
        plugin = Collection(args.actual, args.manifest if args.internal == "worker" else None)
        cache = runtime / "pytest-cache"
        options = ["-o", f"cache_dir={cache}"]
        if args.internal == "collect":
            # Collection itself can import application code, including modules
            # whose files contain no tests. Include it in the coverage union.
            options += [
                "--collect-only",
                "-q",
                *IGNORES,
                "--cov=claude_hub",
                "--cov-report=",
            ]
        else:
            options += [
                "-xvs",
                *IGNORES,
                "--cov=claude_hub",
                "--cov-report=",
                "--durations=50",
                "--durations-min=0",
                *args.files,
            ]
        return int(pytest.main(options, plugins=[plugin]))

    # Only the supervisor adopts children; worker pytest processes do not.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    enable_subreaper()
    runtime.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    common = [
        sys.executable,
        "-I",
        script,
        "--backend",
        str(backend),
        "--runtime",
        str(runtime),
        "--evidence",
        str(evidence),
        "--workers",
        str(args.workers),
    ]
    children: list[subprocess.Popen[Any]] = []
    logs: list[Any] = []
    interrupted = 0

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = signum

    old_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        manifest = evidence / "collection.json"
        collect_runtime = runtime / "collection"
        env = isolated_env(collect_runtime, evidence / ".coverage.collection", "collect")
        with (evidence / "collection.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                common + ["--internal", "collect", "--actual", str(manifest)],
                cwd=backend,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(proc)
            while status(proc) is None and not interrupted:
                reap_adopted_zombies(children)
                time.sleep(0.05)
            if interrupted:
                return 128 + interrupted
            if status(proc):
                print(
                    f"Collection failed ({status(proc)}); see {log.name}",
                    file=sys.stderr,
                )
                return status(proc) or 1
        stop(children)
        children = []
        items = json.loads(manifest.read_text(encoding="utf-8"))
        if not items:
            print("Collection produced no tests", file=sys.stderr)
            return 1
        groups = partition(items, args.workers)
        print(
            f"Collected {len(items)} tests in {len(groups)} shards; logs: {evidence}",
            flush=True,
        )
        children = []
        coverage_files = [evidence / ".coverage.collection"]
        for index, group in enumerate(groups):
            shard = runtime / f"shard-{index}"
            shard_evidence = evidence / f"shard-{index}"
            shard_evidence.mkdir(parents=True, exist_ok=True)
            coverage = shard_evidence / ".coverage"
            coverage_files.append(coverage)
            expected = evidence / f"shard-{index}.json"
            expected.write_text(json.dumps(group), encoding="utf-8")
            actual = evidence / f"shard-{index}.actual.json"
            files = list(dict.fromkeys(item["file"] for item in group))
            log = (evidence / f"shard-{index}.log").open("w", encoding="utf-8")
            logs.append(log)
            env = isolated_env(shard, coverage, str(index))
            command = common.copy()
            command[command.index("--runtime") + 1] = str(shard)
            command += [
                "--internal",
                "worker",
                "--manifest",
                str(expected),
                "--actual",
                str(actual),
                *files,
            ]
            children.append(
                subprocess.Popen(
                    command,
                    cwd=backend,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        failed = None
        try:
            while any(status(proc) is None for proc in children) and not interrupted:
                failed = next((p for p in children if status(p) not in (None, 0)), None)
                if failed:
                    break
                reap_adopted_zombies(children)
                time.sleep(0.05)
            failed = next((p for p in children if status(p) not in (None, 0)), None)
            if interrupted or failed:
                print(
                    f"Shard failure or interruption; inspect {evidence}/shard-*.log",
                    file=sys.stderr,
                )
                return 128 + interrupted if interrupted else (status(failed) or 1)
        finally:
            stop(children, graceful=bool(interrupted or failed))
            children = []
            for log in logs:
                log.close()
        totals = {name: 0 for name in ("passed", "skipped", "xfailed", "xpassed")}
        for index in range(len(groups)):
            stats_path = evidence / f"shard-{index}.actual.stats.json"
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            for name in totals:
                totals[name] += int(stats[name])
        if sum(totals.values()) != len(items):
            print(f"Test outcome count differs from collection: {totals}", file=sys.stderr)
            return 1
        print(
            "Shard outcomes: " + ", ".join(f"{v} {k}" for k, v in totals.items()),
            flush=True,
        )
        missing = [str(path) for path in coverage_files if not path.is_file()]
        if missing:
            print(f"Missing shard coverage data: {missing}", file=sys.stderr)
            return 1
        env = os.environ.copy()
        env["COVERAGE_FILE"] = str(evidence / ".coverage")
        # Recreate pytest-cov's source setting when reporting. Without it,
        # coverage XML changes source roots and filenames even for equal data.
        report = (
            "import coverage; c = coverage.Coverage(source=['claude_hub']); "
            "c.load(); c.xml_report(outfile='coverage.xml'); c.report(show_missing=True)"
        )
        commands = (
            [
                sys.executable,
                "-I",
                "-m",
                "coverage",
                "combine",
                "--keep",
                *map(str, coverage_files),
            ],
            [sys.executable, "-I", "-c", report],
        )
        for command in commands:
            proc = subprocess.Popen(
                command,
                cwd=backend,
                env=env,
                start_new_session=True,
            )
            children = [proc]
            while status(proc) is None and not interrupted:
                reap_adopted_zombies(children)
                time.sleep(0.05)
            if interrupted:
                return 128 + interrupted
            if status(proc):
                return status(proc) or 1
            stop(children)
            children = []
        print(
            f"All {len(items)} collected tests completed; coverage combined from {len(groups)} shards."
        )
        return 0
    finally:
        stop(children, graceful=True)
        for log in logs:
            log.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
