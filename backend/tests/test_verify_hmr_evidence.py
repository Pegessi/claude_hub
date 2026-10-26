"""Check HMR evidence routing and CI deadlines without launching services."""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import types
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import Mock

import pytest
import requests

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hmr_helpers(tmp_path: Path) -> types.ModuleType:
    # Importing the E2E module probes Chromium. Compile the actual helper bodies
    # instead, then replace only their process/network boundaries below.
    source = _ROOT / "backend/tests/test_terminal_hmr_recovery_e2e.py"
    names = {
        "_hmr_log_path",
        "_start_isolated_backend",
        "_stop_backend",
        "_start_harness_vite",
        "_stop_harness_vite",
    }
    tree = ast.parse(source.read_text())
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    module = types.ModuleType("hmr_evidence_helpers")
    module.__dict__.update(
        {
            "__file__": str(source),
            "Any": Any,
            "Path": Path,
            "os": os,
            "pytest": pytest,
            "requests": requests,
            "shlex": shlex,
            "shutil": types.SimpleNamespace(which=lambda tool: f"/unused/{tool}"),
            "sys": sys,
            "time": types.SimpleNamespace(sleep=lambda seconds: None),
            "uuid": uuid,
            "_FRONTEND_DIR": frontend,
            "_HARNESS_HTML": "/test/harness/terminal-hmr-harness.html",
            "_free_port": lambda: 5298,
            "_isolated_tmux_env": lambda bindir: {},
            "_default_tmux_env": lambda: {},
        }
    )
    exec(
        compile(ast.Module(body=list(functions), type_ignores=[]), str(source), "exec"),
        module.__dict__,
    )
    return module


@pytest.mark.parametrize("helper", ["backend", "vite"])
@pytest.mark.parametrize("managed", [False, True], ids=["direct-pytest", "verify"])
@pytest.mark.parametrize("startup_fails", [False, True], ids=["ready", "startup-failure"])
def test_hmr_helper_logs_survive_disposable_runtime_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hmr_helpers: types.ModuleType,
    helper: str,
    managed: bool,
    startup_fails: bool,
) -> None:
    runtime = tmp_path / "runtime" / "test_hmr_case0"
    runtime.mkdir(parents=True)
    (runtime / "scratch.bin").write_bytes(b"disposable")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    if managed:
        monkeypatch.setenv("_CLAUDE_HUB_VERIFY_EVIDENCE", str(evidence))
    else:
        monkeypatch.delenv("_CLAUDE_HUB_VERIFY_EVIDENCE", raising=False)
    logs: list[tuple[Path, BinaryIO]] = []
    processes: list[Mock] = []

    def launch(command: list[str], **kwargs: Any) -> Mock:
        output = kwargs["stdout"]
        output.write(f"diagnostic from launch {len(logs)}\n".encode())
        output.flush()
        logs.append((Path(output.name), output))
        process = Mock()
        process.poll.return_value = 1 if startup_fails else None
        processes.append(process)
        return process

    hmr_helpers.__dict__["subprocess"] = types.SimpleNamespace(
        Popen=launch, STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired
    )
    session = Mock()
    session.get.return_value = types.SimpleNamespace(
        status_code=503 if startup_fails else 200, text="terminal-hmr-harness"
    )
    hmr_helpers.__dict__["local_requests_session"] = lambda: session
    for _ in range(1 if startup_fails else 2):
        if startup_fails:
            with pytest.raises(pytest.fail.Exception, match="diagnostic from launch"):
                if helper == "backend":
                    hmr_helpers._start_isolated_backend(runtime)
                else:
                    hmr_helpers._start_harness_vite("http://127.0.0.1:5297", runtime)
        elif helper == "backend":
            context = hmr_helpers._start_isolated_backend(runtime)
            hmr_helpers._stop_backend(context)
        else:
            context = hmr_helpers._start_harness_vite("http://127.0.0.1:5297", runtime)
            hmr_helpers._stop_harness_vite(context)
    assert all(stream.closed for _, stream in logs)
    assert len(processes) == len(logs)
    filename = "isolated-backend.log" if helper == "backend" else "harness-vite.log"
    if managed:
        assert len({path for path, _ in logs}) == len(logs)
        assert all(
            path.is_relative_to(evidence / "terminal-hmr" / runtime.name) for path, _ in logs
        )
        shutil.rmtree(runtime.parent)
        for index, (path, _) in enumerate(logs):
            assert path.name.startswith(Path(filename).stem + "-")
            assert path.read_text() == f"diagnostic from launch {index}\n"
        assert not list(evidence.rglob("scratch.bin"))
        assert not list(evidence.rglob("home"))
    else:
        assert {path for path, _ in logs} == {runtime / filename}
        assert not list(evidence.iterdir())


def test_hmr_parallel_case_names_do_not_replace_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hmr_helpers: types.ModuleType
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("_CLAUDE_HUB_VERIFY_EVIDENCE", str(evidence))
    first = hmr_helpers._hmr_log_path(tmp_path / "worker0/test_case0", "isolated-backend.log")
    first.write_text("first worker")
    second = hmr_helpers._hmr_log_path(tmp_path / "worker1/test_case0", "isolated-backend.log")
    second.write_text("second worker")
    assert first != second
    assert first.read_text() == "first worker"
    assert second.read_text() == "second worker"


def test_ci_browser_deadlines_leave_time_for_owner_cleanup_and_reporting() -> None:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text()
    commands = re.findall(
        r"timeout --signal=TERM --kill-after=(\d+)s \d+m ./scripts/verify.sh (browser-mocks|terminal-hmr)",
        workflow,
    )
    assert {target for _, target in commands} == {"browser-mocks", "terminal-hmr"}
    # The owner permits 60s cooperative exit, then 3s TERM, 3s KILL and up to
    # 3s reaping. Reserve another 21s for scheduling and report writes.
    minimum_kill_after = 60 + (3 + 3 + 3) + 21
    for seconds, target in commands:
        assert int(seconds) >= minimum_kill_after, f"{target} kills its cleanup owner too early"
