"""Deterministic signal-transition and cleanup-result tests for the private smoke."""

from __future__ import annotations

import ast
import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import manual_chat_workflow_smoke as smoke


@pytest.fixture(autouse=True)
def no_real_runtime_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("this test attempted a real signal, child, or proc operation")

    monkeypatch.setattr(
        smoke,
        "signal",
        SimpleNamespace(
            SIGINT=2,
            SIGTERM=15,
            SIG_BLOCK=0,
            SIG_SETMASK=2,
            signal=forbidden,
            pthread_sigmask=forbidden,
        ),
    )
    monkeypatch.setattr(smoke, "_spawn", forbidden)
    monkeypatch.setattr(smoke.guard, "require_runtime_capabilities", forbidden)
    monkeypatch.setattr(smoke.guard, "stop_owned_descendants", forbidden)


@pytest.mark.parametrize("first", [2, 15])
def test_first_interrupt_defers_every_subsequent_signal_before_raising(first):
    state = smoke._CleanupSignals()
    with pytest.raises(KeyboardInterrupt):
        state(first, None)
    assert state.deferred is True
    for _ in range(1000):
        state(smoke.signal.SIGINT, None)
        state(smoke.signal.SIGTERM, None)
    assert state.received == {"SIGINT": True, "SIGTERM": True}
    assert len(state.received) == 2


def test_first_signal_during_cleanup_is_recorded_without_raising():
    state = smoke._CleanupSignals()
    state.deferred = True
    state(smoke.signal.SIGTERM, None)
    state(999, None)
    assert state.received == {"SIGINT": False, "SIGTERM": True}
    assert len(state.received) == 2


def test_install_blocks_both_signals_until_both_handlers_are_ready(monkeypatch):
    state = smoke._CleanupSignals()
    handlers = {}
    calls = []

    def mask(how, values):
        calls.append((how, set(values)))
        if how == smoke.signal.SIG_BLOCK:
            return {smoke.signal.SIGINT, 10}
        assert handlers == {smoke.signal.SIGINT: state, smoke.signal.SIGTERM: state}
        assert set(values) == {10}  # Preserve other masks, enable our two handlers.
        handlers[smoke.signal.SIGINT](smoke.signal.SIGINT, None)
        raise AssertionError("the first pending signal should interrupt startup")

    monkeypatch.setattr(smoke.signal, "pthread_sigmask", mask)
    monkeypatch.setattr(
        smoke.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler)
    )
    with pytest.raises(KeyboardInterrupt):
        state.install()
    assert calls == [
        (smoke.signal.SIG_BLOCK, {smoke.signal.SIGINT, smoke.signal.SIGTERM}),
        (smoke.signal.SIG_SETMASK, {10}),
    ]
    # A second pending signal cannot throw another exception during unwinding.
    handlers[smoke.signal.SIGTERM](smoke.signal.SIGTERM, None)
    assert state.received == {"SIGINT": True, "SIGTERM": True}


def test_partial_install_failure_does_not_unblock_old_handlers(monkeypatch):
    state = smoke._CleanupSignals()
    mask = Mock(return_value=set())
    calls = []

    def install(number, handler):
        calls.append(number)
        if len(calls) == 2:
            raise OSError("synthetic installation failure")

    monkeypatch.setattr(smoke.signal, "pthread_sigmask", mask)
    monkeypatch.setattr(smoke.signal, "signal", install)
    with pytest.raises(OSError):
        state.install()
    mask.assert_called_once_with(
        smoke.signal.SIG_BLOCK, {smoke.signal.SIGINT, smoke.signal.SIGTERM}
    )
    assert state.deferred is True


def test_repeated_signals_cannot_skip_cleanup_or_result_output(monkeypatch, tmp_path, capsys):
    runtime, artifact = tmp_path / "runtime", tmp_path / "artifact"
    runtime.mkdir()
    artifact.mkdir()
    state = smoke._CleanupSignals()
    with pytest.raises(KeyboardInterrupt):
        state(smoke.signal.SIGINT, None)
    errors = ["scenario:KeyboardInterrupt"]
    steps = []

    def stop(*args):
        state(smoke.signal.SIGTERM, None)
        steps.append("stack")
        return {"complete": True, "processes_verified": True}

    def close():
        state(smoke.signal.SIGINT, None)
        steps.append("listener")

    def sensitive(*args):
        state(smoke.signal.SIGTERM, None)
        steps.append("sensitive")
        return True

    real_write = smoke._write_result

    def write(path, result):
        state(smoke.signal.SIGINT, None)
        steps.append("result")
        real_write(path, result)

    monkeypatch.setattr(smoke, "_stop_stack", stop)
    monkeypatch.setattr(smoke, "_remove_sensitive", sensitive)
    monkeypatch.setattr(smoke, "_write_result", write)
    cleanup = smoke._final_cleanup(
        [],
        {},
        runtime,
        runtime / "socket",
        SimpleNamespace(close=close),
        None,
        errors,
    )
    # Existing scenario errors do not make the cleanup operation itself unhealthy.
    assert cleanup["complete"] is True
    result = {
        "scenario_passed": True,
        "cleanup": cleanup,
        "errors": errors,
        "termination_signals": state.received,
    }
    assert smoke._finish_result(result, runtime, artifact) is False
    assert steps[:3] == ["stack", "listener", "sensitive"]
    assert "result" in steps and runtime.exists()
    saved = json.loads((artifact / "result.json").read_text())
    printed = json.loads(capsys.readouterr().out)
    assert saved["status"] == printed["status"] == "failed"
    assert saved["termination_signals"] == {"SIGINT": True, "SIGTERM": True}


def test_new_cleanup_error_still_marks_cleanup_incomplete(monkeypatch, tmp_path):
    def stop(*args):
        args[4].append("new-cleanup-error")
        return {"complete": True, "processes_verified": True}

    monkeypatch.setattr(smoke, "_stop_stack", stop)
    monkeypatch.setattr(smoke, "_remove_sensitive", Mock(return_value=True))
    errors = ["older-scenario-error"]
    result = smoke._final_cleanup([], {}, tmp_path, tmp_path / "socket", None, None, errors)
    assert result["complete"] is False
    assert errors == ["older-scenario-error", "new-cleanup-error"]


def test_route_proxy_names_exclude_no_proxy_by_meaning():
    assert set(smoke._ROUTE_PROXY_NAMES) == {
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
    assert all(name.lower() != "no_proxy" for name in smoke._ROUTE_PROXY_NAMES)


def test_main_enters_deferral_before_finally_and_outputs_from_finally():
    main = ast.parse(inspect.getsource(smoke.main)).body[0]
    assert isinstance(main, ast.FunctionDef)
    protected = next(node for node in main.body if isinstance(node, ast.Try) and node.finalbody)

    def defers(node):
        return (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "interrupts"
            and node.targets[0].attr == "deferred"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        )

    assert defers(protected.body[-2])
    assert defers(protected.handlers[0].body[0])
    assert defers(protected.finalbody[0])
    assert isinstance(protected.finalbody[-1], ast.Raise)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_finish_result"
        for node in ast.walk(protected.finalbody[-1])
    )
    installation = next(
        index
        for index, node in enumerate(main.body)
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "install"
        )
    )
    allocation = next(
        index
        for index, node in enumerate(main.body)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "runtime" for target in node.targets
            )
        )
    )
    assert installation < allocation
