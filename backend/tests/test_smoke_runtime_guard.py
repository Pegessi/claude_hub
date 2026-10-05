"""Pure fault-injection tests for the private smoke process guard."""

from __future__ import annotations

import errno
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import smoke_runtime_guard as guard


def _no_children(*args):
    raise ChildProcessError(errno.ECHILD, "no children")


def _child(pid=11, ppid=77, start=100, state="S"):
    return {"pid": pid, "ppid": ppid, "start_ticks": start, "state": state}


def _stat(root: Path, pid: int, ppid: int, start: int) -> None:
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
    (directory / "stat").write_text(f"{pid} (fixture) " + " ".join(fields))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    root = tmp_path / "proc"
    thread = root / "77" / "task" / "77"
    thread.mkdir(parents=True)
    (thread / "children").write_text("")
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(guard, "_PROC_ROOT", root)
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guard, "_subreaper", Mock(return_value=True))
    monkeypatch.setattr(
        guard,
        "os",
        SimpleNamespace(
            getpid=lambda: 77,
            scandir=os.scandir,
            WNOHANG=os.WNOHANG,
            waitpid=Mock(side_effect=_no_children),
            pidfd_open=Mock(side_effect=AssertionError("unexpected pidfd_open")),
            close=Mock(),
        ),
    )
    monkeypatch.setattr(
        guard,
        "signal",
        SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIGKILL=signal.SIGKILL,
            pidfd_send_signal=Mock(side_effect=AssertionError("unexpected signal")),
        ),
    )
    return SimpleNamespace(root=root, clock=clock)


@pytest.mark.parametrize("fault", ["open", "signal", "subreaper", "unverified"])
def test_preflight_checks_real_operations_and_closes_fd(monkeypatch, fault):
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    if fault == "open":
        guard.os.pidfd_open.side_effect = OSError(errno.ENOSYS, "unsupported")
    elif fault == "signal":
        guard.signal.pidfd_send_signal.side_effect = PermissionError(errno.EPERM, "denied")
    elif fault == "subreaper":
        monkeypatch.setattr(
            guard, "_subreaper", Mock(side_effect=PermissionError(errno.EPERM, "denied"))
        )
    else:
        monkeypatch.setattr(guard, "_subreaper", Mock(return_value=False))
    with pytest.raises((OSError, RuntimeError)):
        guard.require_runtime_capabilities()
    guard.os.pidfd_open.assert_called_once_with(77, 0)
    assert guard.os.close.call_count == (0 if fault == "open" else 1)
    if fault != "open":
        guard.signal.pidfd_send_signal.assert_called_once_with(41, 0, None, 0)


def test_preflight_requires_children_file(isolated):
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    (isolated.root / "77" / "task" / "77" / "children").unlink()
    with pytest.raises(RuntimeError, match="children"):
        guard.require_runtime_capabilities()
    guard.os.close.assert_called_once_with(41)


def test_scanner_covers_all_threads_and_deduplicates(isolated):
    root = isolated.root
    (root / "77" / "task" / "77" / "children").write_text("11 12")
    other = root / "77" / "task" / "88"
    other.mkdir()
    (other / "children").write_text("12 13")
    (root / "77" / "task" / "99").mkdir()
    _stat(root, 11, 77, 101)
    _stat(root, 12, 77, 102)
    errors = set()
    children, complete = guard._direct_children(77, 1.0, errors)
    assert sorted(item["pid"] for item in children) == [11, 12]
    assert complete and not errors


def test_scanner_does_not_authorize_a_parent_chain(isolated):
    root = isolated.root
    (root / "77" / "task" / "77" / "children").write_text("11 12")
    _stat(root, 11, 77, 101)
    _stat(root, 12, 11, 102)
    errors = set()
    children, complete = guard._direct_children(77, 1.0, errors)
    assert [item["pid"] for item in children] == [11]
    assert not complete and "scan:parent_changed" in errors


@pytest.mark.parametrize("change", ["parent", "start"])
def test_signal_revalidates_identity_after_pidfd_open(monkeypatch, change):
    original = _child()
    current = dict(original)
    current["ppid" if change == "parent" else "start_ticks"] += 1
    monkeypatch.setattr(guard, "_identity", Mock(return_value=current))
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    errors = set()
    action = guard._signal_direct(original, signal.SIGKILL, 77, errors)
    assert action["outcome"] == "identity_changed"
    assert "signal:identity_changed" in errors
    guard.signal.pidfd_send_signal.assert_not_called()
    guard.os.close.assert_called_once_with(41)


def test_close_failure_does_not_erase_signal_outcome(monkeypatch):
    monkeypatch.setattr(guard, "_identity", Mock(return_value=_child()))
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    guard.os.close.side_effect = OSError(errno.EIO, "close failed")
    errors = set()
    action = guard._signal_direct(_child(), signal.SIGTERM, 77, errors)
    assert action["outcome"] == "sent"
    assert action["close_error"] in errors


@pytest.mark.parametrize("fault", ["scan", "open", "signal", "poll"])
def test_errors_survive_later_empty_scans(monkeypatch, fault):
    scans = 0

    def scan(controller_pid, deadline, errors):
        nonlocal scans
        scans += 1
        if scans == 1:
            if fault == "scan":
                errors.add("children_read:PermissionError:13")
                return [], False
            return [_child()], True
        return [], True

    monkeypatch.setattr(guard, "_direct_children", scan)
    monkeypatch.setattr(guard, "_identity", Mock(return_value=_child()))
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    guard.os.waitpid = Mock(side_effect=[(0, 0), ChildProcessError(errno.ECHILD, "no children")])
    processes = []
    if fault == "open":
        guard.os.pidfd_open.side_effect = PermissionError(errno.EPERM, "denied")
    elif fault == "signal":
        guard.signal.pidfd_send_signal.side_effect = PermissionError(errno.EPERM, "denied")
    elif fault == "poll":
        process = SimpleNamespace(pid=11, returncode=None)
        first = True

        def poll():
            nonlocal first
            if first:
                first = False
                raise PermissionError(errno.EPERM, "denied")
            process.returncode = 0
            return 0

        process.poll = poll
        processes = [process]
    result = guard.stop_owned_descendants(processes, term_timeout=1, kill_timeout=1)
    assert result["remaining_at_last_scan"] == []
    assert result["errors"]
    assert result["complete"] is False


def test_scan_and_reap_have_count_limits(monkeypatch, isolated):
    root = isolated.root
    (root / "77" / "task" / "77" / "children").write_text("11 12")
    _stat(root, 11, 77, 101)
    _stat(root, 12, 77, 102)
    monkeypatch.setattr(guard, "_MAX_CHILDREN", 1)
    errors = set()
    children, complete = guard._direct_children(77, 1.0, errors)
    assert len(children) == 1 and not complete
    assert "scan:child_budget" in errors
    monkeypatch.setattr(guard, "_MAX_REAPS", 2)
    guard.os.waitpid = Mock(side_effect=[(99, 0), (100, 0), AssertionError("reap bound exceeded")])
    reaped = []
    assert guard._reap_adopted(1.0, errors, reaped) is False
    assert reaped == [99, 100]
    assert guard.os.waitpid.call_count == 2
    assert "reap:count_budget" in errors


def test_deadline_is_checked_between_signals(monkeypatch, isolated):
    monkeypatch.setattr(guard, "_direct_children", lambda *args: ([_child(11), _child(12)], True))
    guard.os.waitpid = Mock(return_value=(0, 0))

    def slow_signal(identity, signum, controller_pid, errors):
        isolated.clock.now += 1
        return {"pid": identity["pid"], "outcome": "sent"}

    monkeypatch.setattr(guard, "_signal_direct", slow_signal)
    actions = []
    empty, _ = guard._phase(signal.SIGKILL, [], 0.5, set(), actions, [])
    assert not empty
    assert [action["pid"] for action in actions] == [11]


def test_finished_popen_does_not_claim_a_reused_adopted_pid():
    first = SimpleNamespace(pid=11, returncode=0, poll=Mock())
    second = SimpleNamespace(pid=11, returncode=7, poll=Mock())
    guard.os.waitpid = Mock(side_effect=[(11, 0), ChildProcessError(errno.ECHILD, "no children")])
    result = guard.stop_owned_descendants([first, second])
    assert result["complete"] is True
    assert result["adopted_pids_reaped"] == [11]
    assert result["known_returncodes"] == [
        {"pid": 11, "returncode": 0},
        {"pid": 11, "returncode": 7},
    ]
    first.poll.assert_not_called()
    second.poll.assert_not_called()


def test_kill_phase_discovers_newly_adopted_child(monkeypatch):
    children = [_child(11)]
    monkeypatch.setattr(guard, "_direct_children", lambda *args: (list(children), True))
    monkeypatch.setattr(
        guard, "_identity", lambda pid: next(item for item in children if item["pid"] == pid)
    )
    guard.os.pidfd_open = Mock(side_effect=lambda pid, flags: pid)

    def waitpid(*args):
        if children:
            return 0, 0
        return _no_children()

    def send(descriptor, signum, siginfo, flags):
        if signum == signal.SIGKILL:
            children[:] = [_child(12)] if descriptor == 11 else []

    guard.os.waitpid = Mock(side_effect=waitpid)
    guard.signal.pidfd_send_signal = Mock(side_effect=send)
    result = guard.stop_owned_descendants([], term_timeout=0.1, kill_timeout=0.3)
    assert result["complete"] is True
    killed = [item["pid"] for item in result["actions"] if item["signal"] == "SIGKILL"]
    assert killed == [11, 12]


def test_live_known_parent_prevents_false_empty_or_unowned_reaping(monkeypatch):
    process = SimpleNamespace(pid=11, returncode=None, poll=Mock(return_value=None))
    children = [_child(11), _child(12, state="Z")]
    monkeypatch.setattr(guard, "_direct_children", lambda *args: (children, True))
    monkeypatch.setattr(guard, "_identity", Mock(return_value=children[0]))
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    result = guard.stop_owned_descendants([process], term_timeout=0, kill_timeout=0.1)
    assert result["complete"] is False
    assert result["known_returncodes"] == [{"pid": 11, "returncode": None}]
    assert all(action["pid"] != 12 for action in result["actions"])
    guard.os.waitpid.assert_not_called()


def test_popen_can_finish_without_errors(monkeypatch):
    process = SimpleNamespace(pid=11, returncode=None)
    polls = 0

    def poll():
        nonlocal polls
        polls += 1
        if polls == 2:
            process.returncode = 0
        return process.returncode

    process.poll = poll
    monkeypatch.setattr(
        guard,
        "_direct_children",
        lambda *args: ([_child()] if process.returncode is None else [], True),
    )
    monkeypatch.setattr(guard, "_identity", Mock(return_value=_child()))
    guard.os.pidfd_open = Mock(return_value=41)
    guard.signal.pidfd_send_signal = Mock()
    result = guard.stop_owned_descendants([process], term_timeout=1, kill_timeout=1)
    assert result["complete"] is True
    assert polls == 2
    assert result["known_returncodes"] == [{"pid": 11, "returncode": 0}]


@pytest.mark.parametrize("name", ["term", "kill"])
@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0])
def test_invalid_timeouts_are_recorded_and_bounded(monkeypatch, name, value):
    deadlines = []

    def phase(signum, processes, deadline, errors, actions, reaped):
        deadlines.append(deadline)
        return False, []

    monkeypatch.setattr(guard, "_phase", phase)
    arguments = {"term_timeout": 0.1, "kill_timeout": 0.1}
    arguments[f"{name}_timeout"] = value
    result = guard.stop_owned_descendants([], **arguments)
    assert result["complete"] is False
    assert f"timeout:{name}" in result["errors"]
    assert deadlines == ([10, 0.1] if name == "term" else [0.1, 5])


def test_kill_has_budget_after_term_exhausts_its_share(monkeypatch):
    monkeypatch.setattr(guard, "_MAX_ACTIONS", 2)
    monkeypatch.setattr(guard, "_direct_children", lambda *args: ([_child(11), _child(12)], True))
    monkeypatch.setattr(guard, "_identity", lambda pid: _child(pid))
    guard.os.waitpid = Mock(return_value=(0, 0))
    guard.os.pidfd_open = Mock(side_effect=lambda pid, flags: pid)
    guard.signal.pidfd_send_signal = Mock()
    result = guard.stop_owned_descendants([], term_timeout=0.1, kill_timeout=0.1)
    assert [action["signal"] for action in result["actions"]] == ["SIGTERM", "SIGKILL"]
    assert result["complete"] is False
    assert "signal:SIGTERM:count_budget" in result["errors"]
    assert "signal:SIGKILL:count_budget" in result["errors"]
