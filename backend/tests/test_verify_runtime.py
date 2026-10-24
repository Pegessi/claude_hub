"""Exercise verification runtime ownership with small, bounded local processes."""

from __future__ import annotations

import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from unittest.mock import Mock

import pytest

_OWNER = Path(__file__).resolve().parents[2] / "scripts/check_verify_runtime.py"
_CHILD_SETUP = """
import json
import os
import subprocess
import sys
import time
from pathlib import Path
runtime = Path(os.environ['_CLAUDE_HUB_VERIFY_RUNTIME'])
evidence = Path(os.environ['_CLAUDE_HUB_VERIFY_EVIDENCE'])
ready = Path(os.environ['OWNER_TEST_READY'])
"""


@pytest.fixture
def runtime_owner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_runtime_test_owner", _OWNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _alive(pid), f"owned test process {pid} did not stop"


def _wait_json(path: Path, process: subprocess.Popen[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text())
                assert isinstance(value, dict)
                return value
            except json.JSONDecodeError:
                pass
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"owner exited before readiness: {stdout}\n{stderr}")
        time.sleep(0.02)
    pytest.fail(f"owned process did not publish {path.name}")


@contextmanager
def _invocation(
    tmp_path: Path, source: str, *, keep: bool = False, target: str = "backend-tests"
) -> Iterator[tuple[subprocess.Popen[str], Path, Path]]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    ready = tmp_path / "ready.json"
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": str(tmp_path),
        "CLAUDE_HUB_VERIFY_ARTIFACT_ROOT": str(artifacts),
        "CLAUDE_HUB_VERIFY_KEEP_RUNTIME": "1" if keep else "0",
        "OWNER_TEST_READY": str(ready),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(_OWNER),
            "--target",
            target,
            "--",
            sys.executable,
            "-I",
            "-c",
            _CHILD_SETUP + source,
        ],
        env=environment,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield process, artifacts, ready
    finally:
        # Even a failed assertion may leave the intentionally escaped test child.
        # Only this invocation's generated readiness/report records name owners.
        records = []
        if ready.is_file():
            records.append(json.loads(ready.read_text()))
        for report in artifacts.glob("claude-hub-verify.*/runtime.json"):
            if report.is_file():
                records.append(json.loads(report.read_text()))
        for record in records:
            for key in ("child_pid", "target_pid"):
                if record.get(key):
                    _stop_pid(int(record[key]))
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.communicate(timeout=3)
        # Kept or rejected-cleanup sandboxes are disposable test data. Never
        # inspect/remove any other /tmp directory, including historical runs.
        for record in records:
            value = record.get("runtime")
            if value:
                root = Path(value)
                assert root.parent == Path("/tmp") and root.name.startswith("ch-v-")
                if root.is_dir() and not root.is_symlink():
                    shutil.rmtree(root)


def _result(
    process: subprocess.Popen[str], artifacts: Path, *, timeout: float = 15
) -> tuple[dict[str, Any], Path, str, str]:
    stdout, stderr = process.communicate(timeout=timeout)
    evidence = next(
        path for path in artifacts.glob("claude-hub-verify.*") if (path / "runtime.json").is_file()
    )
    return json.loads((evidence / "runtime.json").read_text()), evidence, stdout, stderr


@pytest.mark.parametrize("status", [0, 7])
def test_success_and_failure_remove_runtime_but_keep_evidence(tmp_path: Path, status: int) -> None:
    source = f"""
(runtime / 'large-input').write_bytes(b'x' * (2 * 1024 * 1024))
for name in ('pytest.log', 'junit.xml', 'coverage.xml', 'provenance.json'):
    (evidence / name).write_text('retained-' + name)
print('target output', flush=True)
raise SystemExit({status})
"""
    with _invocation(tmp_path, source) as (process, artifacts, _):
        report, evidence, stdout, stderr = _result(process, artifacts)
        assert process.returncode == status, stderr
        assert report["command_exit_code"] == status
        assert report["exit_code"] == status
        assert report["process_group_stopped"] is True
        assert report["cleanup"]["state"] == "removed"
        assert not Path(report["runtime"]).exists()
        assert "target output" in stdout
        for name in ("pytest.log", "junit.xml", "coverage.xml", "provenance.json"):
            assert (evidence / name).read_text() == "retained-" + name
        assert (evidence / "exit-code").read_text().strip() == str(status)


@pytest.mark.parametrize("status", [0, 7])
def test_explicit_keep_retains_runtime_without_changing_target_status(
    tmp_path: Path, status: int
) -> None:
    source = f"(runtime / 'input').write_text('debug me')\nraise SystemExit({status})\n"
    with _invocation(tmp_path, source, keep=True) as (process, artifacts, _):
        report, _, _, stderr = _result(process, artifacts)
        assert process.returncode == status, stderr
        assert report["cleanup"]["reason"] == "explicit_keep"
        assert (Path(report["runtime"]) / "input").read_text() == "debug me"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process-state assertions")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["interrupt", "terminate"])
def test_signals_stop_owned_target_and_descendant_before_cleanup(
    tmp_path: Path, signum: int
) -> None:
    source = """
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
ready.write_text(json.dumps({'runtime': str(runtime), 'target_pid': os.getpid(), 'child_pid': child.pid}))
time.sleep(60)
"""
    with _invocation(tmp_path, source) as (process, artifacts, ready):
        owned = _wait_json(ready, process)
        process.send_signal(signum)
        report, _, _, stderr = _result(process, artifacts)
        assert process.returncode == 128 + signum, stderr
        assert report["process_group_stopped"] is True
        assert report["cleanup"]["state"] == "removed"
        assert not Path(report["runtime"]).exists()
        assert not _alive(owned["target_pid"])
        assert not _alive(owned["child_pid"])


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process-state assertions")
def test_term_ignoring_descendant_is_killed_and_reaped_without_losing_success(
    tmp_path: Path,
) -> None:
    child_source = """
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ['OWNER_TEST_READY'] + '.child').write_text('ready')
time.sleep(60)
"""
    source = f"""
child = subprocess.Popen([sys.executable, '-I', '-c', {child_source!r}])
deadline = time.monotonic() + 5
while not Path(str(ready) + '.child').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('child did not become ready')
    time.sleep(0.01)
ready.write_text(json.dumps({{'runtime': str(runtime), 'child_pid': child.pid}}))
"""
    with _invocation(tmp_path, source) as (process, artifacts, ready):
        owned = _wait_json(ready, process)
        report, _, _, stderr = _result(process, artifacts)
        assert process.returncode == 0, stderr
        assert report["process_group_stopped"] is True
        assert report["cleanup"]["state"] == "removed"
        assert not _alive(owned["child_pid"])


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux tagged-process inspection")
@pytest.mark.parametrize("status, expected", [(0, 1), (7, 7)])
def test_escaped_tagged_child_retains_runtime_and_original_failure(
    tmp_path: Path, status: int, expected: int
) -> None:
    source = f"""
child = subprocess.Popen(
    [sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
    start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
ready.write_text(json.dumps({{'runtime': str(runtime), 'child_pid': child.pid}}))
raise SystemExit({status})
"""
    with _invocation(tmp_path, source) as (process, artifacts, ready):
        owned = _wait_json(ready, process)
        report, _, _, stderr = _result(process, artifacts)
        assert process.returncode == expected, stderr
        assert report["command_exit_code"] == status
        assert report["cleanup"]["reason"] == "runtime_still_in_use"
        assert owned["child_pid"] in report["cleanup"]["pids"]
        assert Path(report["runtime"]).is_dir()
        assert _alive(owned["child_pid"])


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_replaced_runtime_identity_never_deletes_other_data(
    tmp_path: Path, runtime_owner: ModuleType, replacement: str
) -> None:
    runtime, retired, outside = (tmp_path / name for name in ("runtime", "retired", "outside"))
    runtime.mkdir()
    (runtime / "owned").write_text("keep original identity")
    before = runtime.stat()
    outside.mkdir()
    (outside / "sentinel").write_text("never remove")
    runtime.rename(retired)
    if replacement == "symlink":
        runtime.symlink_to(outside, target_is_directory=True)
    else:
        runtime.mkdir()
        (runtime / "foreign").write_text("replacement data")
    result = runtime_owner.cleanup_runtime(
        runtime, (before.st_dev, before.st_ino), "test-run", keep=False, processes_stopped=True
    )
    assert result["reason"] == "runtime_identity_changed"
    assert (retired / "owned").read_text() == "keep original identity"
    assert (outside / "sentinel").read_text() == "never remove"
    if replacement == "directory":
        assert (runtime / "foreign").read_text() == "replacement data"


def test_nested_symlink_is_removed_without_following_its_target(
    tmp_path: Path, runtime_owner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, outside = tmp_path / "runtime", tmp_path / "outside"
    runtime.mkdir()
    outside.mkdir()
    (outside / "sentinel").write_text("never remove")
    (runtime / "link").symlink_to(outside, target_is_directory=True)
    before = runtime.stat()
    monkeypatch.setattr(runtime_owner, "runtime_users", lambda *args: [])
    result = runtime_owner.cleanup_runtime(
        runtime, (before.st_dev, before.st_ino), "test-run", keep=False, processes_stopped=True
    )
    assert result["state"] == "removed"
    assert not runtime.exists()
    assert (outside / "sentinel").read_text() == "never remove"


@pytest.mark.parametrize("keep", [False, True])
def test_live_owned_group_prevents_cleanup_even_with_keep_option(
    tmp_path: Path, runtime_owner: ModuleType, keep: bool
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    before = runtime.stat()
    result = runtime_owner.cleanup_runtime(
        runtime, (before.st_dev, before.st_ino), "test-run", keep=keep, processes_stopped=False
    )
    assert result["reason"] == "owned_process_group_still_alive"
    assert runtime.is_dir()


@pytest.mark.parametrize("failure_path", ["runtime.json", "exit-code"])
@pytest.mark.parametrize("status, expected", [(0, 1), (7, 7)])
def test_final_report_failure_preserves_existing_target_failure(
    tmp_path: Path, failure_path: str, status: int, expected: int
) -> None:
    source = f"""
deadline = time.monotonic() + 5
while 'pid' not in json.loads((evidence / 'runtime.json').read_text()):
    if time.monotonic() > deadline:
        raise RuntimeError('owner did not record target')
    time.sleep(0.01)
ready.write_text(json.dumps({{'runtime': str(runtime)}}))
path = evidence / {failure_path!r}
path.unlink(missing_ok=True)
path.mkdir()
raise SystemExit({status})
"""
    with _invocation(tmp_path, source) as (process, artifacts, ready):
        owned = _wait_json(ready, process)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == expected, stdout + stderr
        assert "could not write" in stderr
        assert not Path(owned["runtime"]).exists()
        evidence = next(artifacts.glob("claude-hub-verify.*"))
        if failure_path == "runtime.json":
            assert (evidence / "exit-code").read_text().strip() == str(expected)
        else:
            report = json.loads((evidence / "runtime.json").read_text())
            assert report["exit_code"] == expected


def test_report_file_symlinks_do_not_overwrite_outside_files(
    tmp_path: Path, runtime_owner: ModuleType
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not a report")
    (evidence / "runtime.json").symlink_to(outside)
    (evidence / "exit-code").symlink_to(outside)
    report = {"exit_code": 7}
    assert runtime_owner.finish_report(evidence, report, 7) == 7
    assert outside.read_text() == "not a report"
    assert json.loads((evidence / "runtime.json").read_text()) == report
    assert (evidence / "exit-code").read_text().strip() == "7"
    assert not (evidence / "runtime.json").is_symlink()
    assert not (evidence / "exit-code").is_symlink()


def test_cleanup_never_sweeps_historical_sibling_runs(tmp_path: Path) -> None:
    historical = tmp_path / "artifacts" / "claude-hub-verify.historical"
    historical.mkdir(parents=True)
    (historical / "pytest.log").write_text("retained history")
    with _invocation(tmp_path, "(runtime / 'input').write_text('temporary')\n") as (
        process,
        artifacts,
        _,
    ):
        report, _, _, stderr = _result(process, artifacts)
        assert process.returncode == 0, stderr
        assert report["cleanup"]["state"] == "removed"
        assert (historical / "pytest.log").read_text() == "retained history"


def test_signal_during_runtime_creation_cleans_without_launching_target(
    tmp_path: Path, runtime_owner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_mkdtemp = runtime_owner.tempfile.mkdtemp
    previous_handler = signal.getsignal(signal.SIGTERM)
    spawn = Mock(side_effect=AssertionError("interrupted target must not start"))

    def create_and_interrupt(*args: Any, **kwargs: Any) -> str:
        created = original_mkdtemp(*args, **kwargs)
        assert isinstance(created, str)
        if kwargs.get("prefix") == "ch-v-":
            os.kill(os.getpid(), signal.SIGTERM)
        return created

    monkeypatch.setattr(runtime_owner.tempfile, "mkdtemp", create_and_interrupt)
    monkeypatch.setattr(runtime_owner.subprocess, "Popen", spawn)
    monkeypatch.setenv("CLAUDE_HUB_VERIFY_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HUB_VERIFY_KEEP_RUNTIME", "0")
    monkeypatch.delenv("CLAUDE_HUB_TEST_BACKEND_URL", raising=False)
    monkeypatch.setattr(sys, "argv", [str(_OWNER), "--target", "backend-tests", "--", "unused"])
    assert runtime_owner.main() == 128 + signal.SIGTERM
    spawn.assert_not_called()
    report = json.loads(next(tmp_path.glob("claude-hub-verify.*/runtime.json")).read_text())
    assert report["cleanup"]["state"] == "removed"
    assert not Path(report["runtime"]).exists()
    assert signal.getsignal(signal.SIGTERM) == previous_handler


@pytest.fixture
def mocked_main_process(
    tmp_path: Path, runtime_owner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> tuple[Mock, Mock]:
    original_mkdtemp = runtime_owner.tempfile.mkdtemp

    def create_under_test_root(*args: Any, **kwargs: Any) -> str:
        # These tests intentionally retain runtime, but never launch a process
        # or need short socket paths. Keep their data inside pytest's own root.
        kwargs["dir"] = tmp_path
        created = original_mkdtemp(*args, **kwargs)
        assert isinstance(created, str)
        return created

    process = Mock(pid=123456789, returncode=None)
    stop = Mock(return_value=False)
    monkeypatch.setattr(runtime_owner.tempfile, "mkdtemp", create_under_test_root)
    monkeypatch.setattr(runtime_owner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runtime_owner, "stop_group", stop)
    monkeypatch.setenv("CLAUDE_HUB_VERIFY_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HUB_VERIFY_KEEP_RUNTIME", "0")
    monkeypatch.delenv("CLAUDE_HUB_TEST_BACKEND_URL", raising=False)
    monkeypatch.setattr(sys, "argv", [str(_OWNER), "--target", "backend-tests", "--", "unused"])
    return process, stop


def test_failed_group_stop_after_interrupt_never_waits_without_a_deadline(
    tmp_path: Path,
    runtime_owner: ModuleType,
    mocked_main_process: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, stop = mocked_main_process
    interrupt = Mock(return_value=False)
    monkeypatch.setattr(runtime_owner, "interrupt_target", interrupt)
    unbounded_waits: list[bool] = []

    def interrupt_on_poll() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    def bounded_wait(timeout: float | None = None) -> None:
        if timeout is None:
            unbounded_waits.append(True)
            raise AssertionError("unbounded wait after failed group stop")
        raise subprocess.TimeoutExpired("mocked target", timeout)

    process.poll.side_effect = interrupt_on_poll
    process.wait.side_effect = bounded_wait
    status = runtime_owner.main()
    assert not unbounded_waits
    assert status == 128 + signal.SIGTERM
    interrupt.assert_called_once_with(process, "backend-tests", signal.SIGTERM)
    stop.assert_called_once_with(process)
    report = json.loads(next(tmp_path.glob("claude-hub-verify.*/runtime.json")).read_text())
    assert report["process_group_stopped"] is False
    assert report["cleanup"]["state"] == "retained"
    assert Path(report["runtime"]).is_dir()


def test_group_cleanup_oserror_retains_runtime_and_existing_target_failure(
    tmp_path: Path, runtime_owner: ModuleType, mocked_main_process: tuple[Mock, Mock]
) -> None:
    process, stop = mocked_main_process
    process.returncode = 7
    process.poll.return_value = 7
    process.wait.return_value = 7
    stop.side_effect = OSError("simulated process cleanup failure")
    assert runtime_owner.main() == 7
    stop.assert_called_once_with(process)
    report_path = next(tmp_path.glob("claude-hub-verify.*/runtime.json"))
    report = json.loads(report_path.read_text())
    assert report["command_exit_code"] == report["exit_code"] == 7
    assert report["process_group_stopped"] is False
    assert report["cleanup"]["state"] == "retained"
    assert "OSError" in report["error"]
    assert Path(report["runtime"]).is_dir()
    assert (report_path.parent / "exit-code").read_text().strip() == "7"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process-state assertions")
@pytest.mark.parametrize("target", ["backend-tests", "backend-types"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["interrupt", "terminate"])
def test_nested_owner_finishes_delayed_evidence_before_group_cleanup(
    tmp_path: Path, target: str, signum: int
) -> None:
    source = """
import signal
received = []
def request_stop(signum, frame):
    received.append(signum)
signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
ready.write_text(json.dumps({'runtime': str(runtime), 'target_pid': os.getpid(), 'child_pid': child.pid}))
deadline = time.monotonic() + 15
while not received:
    if time.monotonic() > deadline:
        raise RuntimeError('nested owner did not receive interruption')
    time.sleep(0.02)
started = time.monotonic()
# Finalization legitimately exceeds the short orphan-process cleanup grace.
time.sleep(3.6)
if child.poll() is None:
    child.terminate()
child.wait(timeout=3)
(evidence / 'nested-lifecycle.json').write_text(json.dumps({
    'signal': received[0], 'elapsed': time.monotonic() - started, 'cleanup_complete': True,
}))
"""
    with _invocation(tmp_path, source, target=target) as (process, artifacts, ready):
        owned = _wait_json(ready, process)
        process.send_signal(signum)
        report, evidence, stdout, stderr = _result(process, artifacts)
        assert process.returncode == 128 + signum, stdout + stderr
        nested = json.loads((evidence / "nested-lifecycle.json").read_text())
        assert nested["cleanup_complete"] is True
        assert nested["elapsed"] >= 3.5
        assert nested["signal"] == (signal.SIGINT if target == "backend-tests" else signum)
        assert report["process_group_stopped"] is True
        assert report["cleanup"]["state"] == "removed"
        assert not Path(report["runtime"]).exists()
        assert not _alive(owned["target_pid"])
        assert not _alive(owned["child_pid"])


def test_term_during_final_report_publication_updates_exit_status_and_evidence(
    tmp_path: Path,
    runtime_owner: ModuleType,
    mocked_main_process: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, stop = mocked_main_process
    process.returncode = 0
    process.poll.return_value = 0
    stop.return_value = True
    original_finish = runtime_owner.finish_report
    published: list[int] = []

    def publish_then_interrupt(evidence: Path, report: dict[str, Any], status: int) -> int:
        published.append(status)
        result = original_finish(evidence, report, status)
        assert isinstance(result, int)
        if len(published) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(runtime_owner, "finish_report", publish_then_interrupt)
    assert runtime_owner.main() == 128 + signal.SIGTERM
    assert published[0] == 0
    report_path = next(tmp_path.glob("claude-hub-verify.*/runtime.json"))
    report = json.loads(report_path.read_text())
    assert report["exit_code"] == 128 + signal.SIGTERM
    assert (report_path.parent / "exit-code").read_text().strip() == str(128 + signal.SIGTERM)
    assert report["cleanup"]["state"] == "removed"
    assert not Path(report["runtime"]).exists()


@pytest.mark.parametrize("closed_stream", ["stdout", "stderr"])
def test_closed_diagnostic_pipe_preserves_target_failure_and_cleanup(
    tmp_path: Path, closed_stream: str
) -> None:
    source = f"""
release = Path(str(ready) + '.release')
deadline = time.monotonic() + 8
while not release.exists():
    if time.monotonic() > deadline:
        raise RuntimeError('test did not release target')
    time.sleep(0.01)
ready.write_text(json.dumps({{'runtime': str(runtime)}}))
if {closed_stream!r} == 'stderr':
    # Force an owner-side diagnostic, not an error from the target's stderr.
    (evidence / 'exit-code').mkdir()
raise SystemExit(7)
"""
    with _invocation(tmp_path, source) as (process, artifacts, ready):
        assert process.stdout is not None
        initial = b""
        deadline = time.monotonic() + 8
        while b"\n" not in initial:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "owner did not publish its initial line"
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            assert readable, "owner did not publish its initial line"
            chunk = os.read(process.stdout.fileno(), 4096)
            assert chunk, "owner exited before publishing its initial line"
            initial += chunk
        assert initial.startswith(b"Verify evidence:")
        if closed_stream == "stdout":
            process.stdout.close()
            process.stdout = None
        else:
            assert process.stderr is not None
            process.stderr.close()
            process.stderr = None
        # Closing before target exit guarantees that final owner diagnostics
        # encounter EPIPE, including Python's shutdown flush after main returns.
        Path(str(ready) + ".release").write_text("continue")
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 7, f"stdout={stdout!r}, stderr={stderr!r}"
        evidence = next(artifacts.glob("claude-hub-verify.*"))
        report = json.loads((evidence / "runtime.json").read_text())
        assert report["command_exit_code"] == report["exit_code"] == 7
        assert report["process_group_stopped"] is True
        assert report["cleanup"]["state"] == "removed"
        assert not Path(report["runtime"]).exists()
        if closed_stream == "stdout":
            assert (evidence / "exit-code").read_text().strip() == "7"
