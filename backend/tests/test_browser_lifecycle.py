from __future__ import annotations

import asyncio
import errno
import importlib
import inspect
import json
import os
import runpy
import shutil
import signal
import stat
import subprocess
import tempfile
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = Path(__file__).resolve().parents[2] / "frontend" / "tests"
    monkeypatch.syspath_prepend(str(directory))
    module = importlib.import_module("browser_lifecycle")
    assert module.__file__ is not None
    assert Path(module.__file__).resolve() == (directory / "browser_lifecycle.py").resolve()
    return module


class _FakeContext:
    def __init__(self, owner: _FakePlaywright) -> None:
        self.owner = owner

    async def close(self) -> None:
        await self.owner.close_step("context")


class _FakeBrowser:
    def __init__(self, owner: _FakePlaywright) -> None:
        self.owner = owner

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        assert kwargs["service_workers"] == self.owner.expected_service_workers
        if self.owner.creation_failure == "context":
            raise RuntimeError("context creation failed")
        return _FakeContext(self.owner)

    async def close(self) -> None:
        await self.owner.close_step("browser")


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = self
        self.expected_service_workers = "block"
        self.creation_failure: str | None = None
        self.close_failure: tuple[str, BaseException] | None = None
        self.hold_stage: str | None = None
        self.attempts: list[str] = []
        self.entered = {
            name: asyncio.Event() for name in ("scenario", "context", "browser", "driver")
        }
        self.release = {name: asyncio.Event() for name in self.entered}

    async def start(self) -> _FakePlaywright:
        return self

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        assert kwargs["headless"] is True
        if self.creation_failure == "browser":
            raise RuntimeError("browser creation failed")
        return _FakeBrowser(self)

    async def __aexit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        await self.close_step("driver")

    async def close_step(self, name: str) -> None:
        self.attempts.append(name)
        self.entered[name].set()
        if self.hold_stage == name:
            await self.release[name].wait()
        if self.close_failure is not None and self.close_failure[0] == name:
            raise self.close_failure[1]


def _capture_signal_callbacks(
    monkeypatch: pytest.MonkeyPatch, lifecycle: ModuleType
) -> dict[int, Callable[[], None]]:
    loop = asyncio.get_running_loop()
    callbacks: dict[int, Callable[[], None]] = {}

    def install(signum: int, callback: Callable[..., None], *args: Any) -> None:
        callbacks[signum] = partial(callback, *args)

    def remove(signum: int) -> bool:
        return callbacks.pop(signum, None) is not None

    monkeypatch.setattr(loop, "add_signal_handler", install)
    monkeypatch.setattr(loop, "remove_signal_handler", remove)
    # No real handler was installed, so restoration must not mutate one.
    monkeypatch.setattr(lifecycle.signal, "signal", lambda _sig, _handler: signal.SIG_DFL)
    return callbacks


def _report(output: Path) -> dict[str, Any]:
    value = json.loads((output / "lifecycle.json").read_text())
    assert isinstance(value, dict)
    return value


async def _finish_owned(task: asyncio.Task[Any], fake: _FakePlaywright) -> None:
    for event in fake.release.values():
        event.set()
    if not task.done():
        task.cancel()
    _, pending = await asyncio.wait({task}, timeout=2.0)
    if task.done() and not task.cancelled():
        task.exception()
    assert not pending, "test-owned browser lifecycle task did not finish"


@pytest.mark.asyncio
@pytest.mark.parametrize("service_workers", [None, "block", "allow"])
async def test_service_worker_policy_is_explicit_and_default_stays_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle: ModuleType,
    service_workers: str | None,
) -> None:
    fake = _FakePlaywright()
    fake.expected_service_workers = service_workers or "block"
    monkeypatch.setattr(lifecycle, "async_playwright", lambda: fake)
    run = lifecycle.BrowserRun(tmp_path / "evidence", tmp_path / "unused-browser")
    options = {} if service_workers is None else {"service_workers": service_workers}
    async with run.context(viewport={"width": 800, "height": 600}, **options):
        pass
    assert run.cleanup_complete
    assert fake.attempts == ["context", "browser", "driver"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "normal",
        "assertion",
        "context-create",
        "browser-create",
        "scenario-exit",
        "assertion-close-error",
    ],
)
async def test_browser_lifecycle_reports_outcome_and_cleans_partial_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lifecycle: ModuleType, outcome: str
) -> None:
    fake = _FakePlaywright()
    if outcome in {"context-create", "browser-create"}:
        fake.creation_failure = outcome.removesuffix("-create")
    if outcome == "assertion-close-error":
        fake.close_failure = ("browser", RuntimeError("close failed"))
    monkeypatch.setattr(lifecycle, "async_playwright", lambda: fake)
    callbacks = _capture_signal_callbacks(monkeypatch, lifecycle)
    run = lifecycle.BrowserRun(tmp_path / "evidence", tmp_path / "unused-browser")

    async def scenario(owner: Any) -> None:
        async with owner.context(viewport={"width": 800, "height": 600}):
            if outcome in {"assertion", "assertion-close-error"}:
                raise AssertionError("scenario failed")
            if outcome == "scenario-exit":
                raise SystemExit(7)

    task = asyncio.create_task(
        lifecycle._execute(scenario, run, {"python": "fixture", "playwright": "fixture"})
    )
    try:
        _, pending = await asyncio.wait({task}, timeout=2.0)
        assert not pending
        assert task.result() == (0 if outcome == "normal" else 1)
        report = _report(run.output)
        assert report["result"] == ("PASS" if outcome == "normal" else "FAIL")
        assert report["cleanup_complete"] is (outcome != "assertion-close-error")
        assert report["process_tree_exit"] == "not_verified"
        assert not callbacks
        states = {item["step"]: item["state"] for item in report["cleanup"]}
        assert states["driver"] == "closed"
        if outcome == "browser-create":
            assert states == {
                "context": "not_acquired",
                "browser": "not_acquired",
                "driver": "closed",
            }
            assert fake.attempts == ["driver"]
        elif outcome == "context-create":
            assert states == {"context": "not_acquired", "browser": "closed", "driver": "closed"}
            assert fake.attempts == ["browser", "driver"]
        else:
            assert fake.attempts == ["context", "browser", "driver"]
        if outcome.startswith("assertion"):
            assert report["error_type"] == "AssertionError"
        if outcome == "assertion-close-error":
            assert states["browser"] == "error"
        if outcome == "scenario-exit":
            assert report["error_type"] == "SystemExit"
    finally:
        await _finish_owned(task, fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["scenario", "context", "browser", "driver"])
async def test_term_and_repeated_term_finish_every_close_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lifecycle: ModuleType, stage: str
) -> None:
    fake = _FakePlaywright()
    fake.hold_stage = stage
    monkeypatch.setattr(lifecycle, "async_playwright", lambda: fake)
    callbacks = _capture_signal_callbacks(monkeypatch, lifecycle)
    run = lifecycle.BrowserRun(tmp_path / "evidence", tmp_path / "unused-browser")
    scene_task: asyncio.Task[Any] | None = None

    async def scenario(owner: Any) -> None:
        nonlocal scene_task
        scene_task = asyncio.current_task()
        async with owner.context(viewport={"width": 800, "height": 600}):
            if stage == "scenario":
                fake.entered[stage].set()
                await fake.release[stage].wait()

    task = asyncio.create_task(
        lifecycle._execute(scenario, run, {"python": "fixture", "playwright": "fixture"})
    )
    try:
        await asyncio.wait_for(fake.entered[stage].wait(), timeout=2.0)
        assert scene_task is not None
        fire = callbacks[signal.SIGTERM]
        fire()
        fire()
        assert scene_task.cancelling() == 1, "repeat TERM interrupted cleanup a second time"
        fake.release[stage].set()
        _, pending = await asyncio.wait({task}, timeout=2.0)
        assert not pending
        assert task.result() == 128 + signal.SIGTERM
        report = _report(run.output)
        assert report["result"] == "INTERRUPTED"
        assert report["signal"] == signal.SIGTERM
        assert report["cleanup_complete"] is True
        assert fake.attempts == ["context", "browser", "driver"]
        assert all(item["state"] == "closed" for item in report["cleanup"])
        assert not callbacks
    finally:
        await _finish_owned(task, fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["context", "browser", "driver"])
@pytest.mark.parametrize("mode", ["error", "timeout"])
async def test_close_error_or_timeout_fails_without_skipping_later_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lifecycle: ModuleType, stage: str, mode: str
) -> None:
    fake = _FakePlaywright()
    if mode == "error":
        failure = SystemExit(9) if stage == "context" else RuntimeError("close failed")
        fake.close_failure = (stage, failure)
    else:
        fake.hold_stage = stage
        monkeypatch.setattr(lifecycle, "_CLOSE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(lifecycle, "async_playwright", lambda: fake)
    _capture_signal_callbacks(monkeypatch, lifecycle)
    run = lifecycle.BrowserRun(tmp_path / "evidence", tmp_path / "unused-browser")

    async def scenario(owner: Any) -> None:
        async with owner.context(viewport={"width": 800, "height": 600}):
            pass

    task = asyncio.create_task(
        lifecycle._execute(scenario, run, {"python": "fixture", "playwright": "fixture"})
    )
    try:
        _, pending = await asyncio.wait({task}, timeout=2.0)
        assert not pending
        assert task.result() == 1
        report = _report(run.output)
        assert report["result"] == "FAIL"
        assert report["cleanup_complete"] is False
        assert fake.attempts == ["context", "browser", "driver"]
        states = {item["step"]: item["state"] for item in report["cleanup"]}
        assert states[stage] == ("error" if mode == "error" else "timed_out")
        assert all(value == "closed" for key, value in states.items() if key != stage)
    finally:
        await _finish_owned(task, fake)


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(directory))
    module = importlib.import_module("verify_browser_mocks")
    assert module.__file__ is not None
    assert Path(module.__file__).resolve() == directory / "verify_browser_mocks.py"
    return module


@pytest.mark.parametrize("business_fails", [False, True])
def test_deep_evidence_uses_short_tmp_and_failed_run_retains_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle: ModuleType,
    launcher: ModuleType,
    business_fails: bool,
) -> None:
    repository = tmp_path / "repository"
    frontend = repository / "frontend"
    vite_entry = frontend / "node_modules/vite/bin/vite.js"
    vite_entry.parent.mkdir(parents=True)
    vite_entry.write_text("// not executed\n")
    (repository / ".node-test-version").write_text("fixture-node\n")
    node, browser = repository / "node", repository / "browser"
    for executable in (node, browser):
        executable.write_bytes(b"")
        executable.chmod(0o700)
    parent = tmp_path.joinpath(*(["deep-artifact-" + "x" * 32] * 5))
    parent.mkdir(parents=True)
    output = parent / "evidence"
    assert len(os.fsencode(str(output))) > 200
    monkeypatch.setattr(launcher, "ROOT", repository)
    monkeypatch.setattr(launcher, "FRONTEND", frontend)
    monkeypatch.setattr(
        lifecycle, "check_python_tools", lambda: {"python": "fixture", "playwright": "fixture"}
    )
    monkeypatch.setattr(launcher.sys, "path", list(launcher.sys.path))
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        [
            "verify_browser_mocks.py",
            "--node",
            str(node),
            "--browser",
            str(browser),
            "--output",
            str(output),
            "--port",
            "5291",
        ],
    )
    roots: list[Path] = []
    removed: list[Path] = []
    launches: list[tuple[list[str], dict[str, str]]] = []
    processes: list[Any] = []
    real_mkdtemp, real_rmtree = tempfile.mkdtemp, shutil.rmtree

    def make_owned_root(*, prefix: str, dir: str) -> str:
        assert prefix == "ch-ui-" and dir == "/tmp"
        root = Path(real_mkdtemp(prefix=prefix, dir=dir))
        roots.append(root)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        return str(root)

    def remove_owned_root(path: Path) -> None:
        assert Path(path) in roots, "attempted removal of an unowned directory"
        removed.append(Path(path))
        real_rmtree(path)

    class FakeProcess:
        def __init__(self, pid: int, returncode: int | None) -> None:
            self.pid, self.returncode = pid, returncode
            self.signals: list[int] = []

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

        def send_signal(self, signum: int) -> None:
            self.signals.append(signum)
            self.returncode = 0

        def kill(self) -> None:
            pytest.fail("fake processes must exit without escalation")

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        env = dict(kwargs["env"])
        index = len(launches)
        launches.append((command, env))
        assert kwargs["cwd"] == frontend
        assert roots and Path(env["TMPDIR"]) == roots[0] / str(index)
        assert stat.S_IMODE(Path(env["TMPDIR"]).stat().st_mode) == 0o700
        failed = business_fails and index == 1
        process = FakeProcess(20000 + index, None if index == 0 else 1 if failed else 0)
        processes.append(process)
        if index == 0:
            assert command[:2] == [str(node), str(vite_entry)]
            assert (
                env["VITE_API_TARGET"] == env["VITE_SYSTEM_API_TARGET"] == "http://127.0.0.1:59999"
            )
        else:
            script = Path(command[1]).name
            assert script == launcher.RUNNERS[index - 1][0]
            run_output = Path(command[3])
            run_output.mkdir()
            result = "FAIL" if failed else "PASS"
            (run_output / "lifecycle.json").write_text(
                json.dumps(
                    {
                        "runner": script,
                        "pid": process.pid,
                        "result": result,
                        "cleanup_complete": True,
                    }
                )
            )
            (run_output / "report.json").write_text(json.dumps({"result": result}))
        return process

    def fake_version(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == [str(node), "--version"]
        assert kwargs["check"] is True and kwargs["timeout"] == 5
        return subprocess.CompletedProcess(command, 0, stdout="vfixture-node\n", stderr="")

    class FakeSocket:
        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 59999)

        def settimeout(self, timeout: float) -> None:
            assert timeout > 0

        def connect_ex(self, address: tuple[str, int]) -> int:
            assert address == ("127.0.0.1", 5291)
            return errno.ECONNREFUSED

        def close(self) -> None:
            pass

        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: Any) -> None:
            self.close()

    nonce = "a" * 32

    class ReadyResponse:
        status = 200

        def read(self, size: int) -> bytes:
            return nonce.encode()

        def __enter__(self) -> ReadyResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

    def ready_open(url: str, *, timeout: float) -> ReadyResponse:
        assert url == f"http://127.0.0.1:5291/__browser_mocks_ready/{nonce}"
        return ReadyResponse()

    real_request, real_url_error = launcher.urllib.request, launcher.urllib.error
    monkeypatch.setattr(launcher, "tempfile", SimpleNamespace(mkdtemp=make_owned_root))
    monkeypatch.setattr(launcher, "shutil", SimpleNamespace(rmtree=remove_owned_root))
    monkeypatch.setattr(
        launcher,
        "subprocess",
        SimpleNamespace(
            Popen=fake_popen,
            run=fake_version,
            TimeoutExpired=subprocess.TimeoutExpired,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
        ),
    )
    monkeypatch.setattr(
        launcher,
        "socket",
        SimpleNamespace(
            AF_INET=launcher.socket.AF_INET,
            SOCK_STREAM=launcher.socket.SOCK_STREAM,
            socket=lambda *_args: FakeSocket(),
        ),
    )
    monkeypatch.setattr(
        launcher,
        "signal",
        SimpleNamespace(
            SIGINT=signal.SIGINT,
            SIGTERM=signal.SIGTERM,
            getsignal=lambda _sig: signal.SIG_DFL,
            signal=lambda _sig, _handler: signal.SIG_DFL,
        ),
    )
    monkeypatch.setattr(launcher, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=nonce)))
    monkeypatch.setattr(
        launcher,
        "urllib",
        SimpleNamespace(
            request=SimpleNamespace(
                ProxyHandler=real_request.ProxyHandler,
                build_opener=lambda *_args: SimpleNamespace(open=ready_open),
            ),
            error=real_url_error,
        ),
    )
    try:
        assert launcher.main() == (1 if business_fails else 0)
        report = json.loads((output / "summary.json").read_text())
        assert len(roots) == 1 and len(launches) == 1 + len(launcher.RUNNERS)
        root = roots[0]
        assert root.parent == Path("/tmp")
        assert report["temporary_root"] == str(root)
        assert report["vite_tmpdir"] == str(root / "0")
        assert report["vite_port_accepting_after_cleanup"] is False
        for index, (_command, env) in enumerate(launches):
            temporary = Path(env["TMPDIR"])
            assert temporary == root / str(index)
            assert len(os.fsencode(str(temporary))) < 60
            assert not temporary.is_relative_to(output)
            assert Path(env["HOME"]).is_relative_to(output)
        assert [entry["tmpdir"] for entry in report["runners"]] == [
            str(root / str(index)) for index in range(1, 1 + len(launcher.RUNNERS))
        ]
        assert processes[0].signals == [signal.SIGTERM]
        assert all(process.returncode is not None for process in processes)
        assert report["result"] == ("FAIL" if business_fails else "PASS")
        if business_fails:
            assert report["temporary_cleanup"] == "retained"
            assert root.is_dir() and removed == []
        else:
            assert report["temporary_cleanup"] == "removed"
            assert not root.exists() and removed == [root]
    finally:
        # Only the roots created and recorded by this unit test may be removed.
        for root in roots:
            if root.exists():
                real_rmtree(root)


def test_every_browser_entrypoint_uses_the_lifecycle_owner(
    monkeypatch: pytest.MonkeyPatch, launcher: ModuleType
) -> None:
    fake = ModuleType("browser_lifecycle")
    calls: list[Any] = []
    setattr(fake, "BrowserRun", object)
    setattr(fake, "run_browser_main", calls.append)
    monkeypatch.setitem(launcher.sys.modules, "browser_lifecycle", fake)
    # A real script invocation includes its directory for shared fixture imports.
    monkeypatch.syspath_prepend(str(launcher.FRONTEND / "tests"))
    for script, _suffix in launcher.RUNNERS:
        runpy.run_path(str(launcher.FRONTEND / "tests" / script), run_name="__main__")
        assert len(calls) == 1
        main = calls.pop()
        assert inspect.iscoroutinefunction(main)
        inspect.signature(main).bind(object())
