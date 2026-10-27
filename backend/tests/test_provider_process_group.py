"""Provider subprocesses run in their own process group and are reaped as a tree.

Provider CLIs (Claude/Cursor/Codex) spawn their own children — bash tool
shells, MCP servers — which must not be orphaned when the transport tears the
direct child down.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, List

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.providers import process as process_module
from claude_hub.services.agent_stream.providers.process import ProcessSession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


class _ProcessTransport(ProcessSession):
    """Concrete minimal ProcessSession: only process lifecycle is exercised."""

    @property
    def _runtime(self) -> Any:  # pragma: no cover - never touched by these tests
        raise AssertionError("_runtime must not be used")

    def _build_command(self) -> List[str]:  # pragma: no cover - never touched
        return []

    async def _send_text(self, text: str) -> None:  # pragma: no cover - never touched
        raise AssertionError("_send_text must not be used")


def _transport() -> _ProcessTransport:
    session = ManagedSession(
        id="sess-1",
        workspace_id="ws-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path="/tmp",
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    return _ProcessTransport(session)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dead_and_reaped(pid: int) -> bool:
    """True once ``pid`` is fully gone — a zombie still answers signal 0."""
    return not _pid_alive(pid)


def _child_pids(ppid: int) -> set[int]:
    """Direct children of ``ppid`` read from procfs (no pgrep dependency)."""
    children: set[int] = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                # stat: pid (comm) state ppid ... — comm may contain spaces.
                fields = handle.read().split(b")", 1)[1].split()
            if int(fields[1]) == ppid:
                children.add(int(name))
        except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
            continue
    return children


def _comm(pid: int) -> str:
    """Process name from procfs ('' once it exits); identifies exec'd sleep."""
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""


async def _wait_for_child_exec(ppid: int, expected_comm: str, timeout: float = 5.0) -> int:
    """Wait until a child of ``ppid`` has exec'd ``expected_comm``.

    Signalling before the shell installs its trap / execs would race the
    spawn, so TERM-ignoring fixtures wait for ``sleep`` to be the live image.
    """

    def _executed() -> bool:
        return any(_comm(pid) == expected_comm for pid in _child_pids(ppid))

    assert await _wait_for(_executed, timeout)
    return next(pid for pid in _child_pids(ppid) if _comm(pid) == expected_comm)


async def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


@asynccontextmanager
async def _spawned(transport: _ProcessTransport, cmd: List[str]):
    """Spawn ``cmd`` under the transport and hard-kill the group on failure.

    Pids must be snapshotted by the test BEFORE teardown: after the leader is
    reaped its descendants are reparented, so ``_child_pids(leader)`` returns
    an empty set and post-teardown assertions on it are vacuously true.
    """
    proc = await transport._spawn_subprocess(cmd, env=None)
    transport._process = proc
    pgid = proc.pid
    try:
        yield proc
    finally:
        # Runs even when an assertion fails mid-test: never leak the tree.
        if proc.returncode is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()


async def test_terminate_kills_grandchild_process_group() -> None:
    transport = _transport()
    try:
        async with _spawned(transport, ["sh", "-c", "sleep 60 & wait"]) as proc:
            assert transport._process_group == proc.pid
            assert os.getpgid(proc.pid) == proc.pid
            assert await _wait_for(lambda: bool(_child_pids(proc.pid)))
            # Snapshot BEFORE teardown — the grandchild is reparented once the
            # leader is reaped and would no longer show up under its pid.
            grandchild = next(iter(_child_pids(proc.pid)))

            await transport._terminate_process()

            assert proc.returncode is not None
            assert await _wait_for(lambda: _dead_and_reaped(proc.pid), timeout=2.0)
            assert await _wait_for(lambda: _dead_and_reaped(grandchild), timeout=2.0)
            assert transport._process is None
            assert transport._process_group == 0
    finally:
        await transport.stop()


async def test_terminate_without_group_targets_direct_child() -> None:
    transport = _transport()
    try:
        async with _spawned(transport, ["sleep", "60"]) as proc:
            # Simulate a process that was not spawned in its own session
            # (non-POSIX fallback path): only the direct child is signalled.
            transport._process_group = 0
            await transport._terminate_process()
            assert proc.returncode is not None
            assert not _pid_alive(proc.pid)
    finally:
        await transport.stop()


async def test_terminate_sigkills_group_after_grace_period() -> None:
    transport = _transport()
    try:
        async with _spawned(
            transport,
            # The child ignores SIGTERM and respawns its grandchild whenever
            # that child exits, so the group survives the grace period and
            # SIGKILL is required to reap the parent.
            ["sh", "-c", "trap '' TERM; while true; do sleep 60 & wait; done"],
        ) as proc:
            assert await _wait_for(lambda: bool(_child_pids(proc.pid)))
            # Snapshot the current grandchild before teardown; looking it up
            # afterward via _child_pids(proc.pid) is vacuous after reparenting.
            surviving_children = set(_child_pids(proc.pid))
            assert surviving_children

            await transport._terminate_process()

            assert proc.returncode is not None
            assert not _pid_alive(proc.pid)
            for child in surviving_children:
                assert await _wait_for(lambda: _dead_and_reaped(child), timeout=2.0)
    finally:
        await transport.stop()


async def test_fast_leader_lazy_group_escalates_within_shared_deadline() -> None:
    """M1 regression: leader dies on SIGTERM, TERM-ignoring grandchild lingers.

    Escalation must key off group liveness, not the direct child's exit: the
    grandchild is reparented to init when the leader is reaped and would never
    see SIGKILL under the old ``wait_for(proc.wait())`` logic. The full
    five-second grace budget elapses (not a second budget restarted after the
    leader exits), after which the whole group is SIGKILLed.
    """
    transport = _transport()
    try:
        async with _spawned(
            transport,
            ["sh", "-c", 'sh -c "trap \\"\\" TERM; exec sleep 30" & wait'],
        ) as proc:
            # Wait for the exec'd sleep (trap installed): an early TERM could
            # otherwise race the fork and kill the inner shell pre-trap.
            grandchild = await _wait_for_child_exec(proc.pid, "sleep")

            start = asyncio.get_running_loop().time()
            await transport._terminate_process()
            elapsed = asyncio.get_running_loop().time() - start

            assert proc.returncode is not None
            assert await _wait_for(lambda: _dead_and_reaped(proc.pid), timeout=2.0)
            # The grandchild must be killed by the group SIGKILL, not merely
            # orphaned: wait until it is fully reaped (ESRCH, no zombie).
            assert await _wait_for(lambda: _dead_and_reaped(grandchild), timeout=3.0)
            # One shared 5s grace budget; allow scheduling slack but not a
            # second full grace window after the leader's prompt exit.
            assert 4.5 <= elapsed <= 8.0, f"teardown took {elapsed:.2f}s"
    finally:
        await transport.stop()


async def test_cancelled_teardown_sigkills_and_reaps_group() -> None:
    """M2 regression: cancellation during grace escalates and reaps at once.

    The awaiting teardown is cancelled well before the grace deadline. The
    TERM-ignoring leader and its TERM-ignoring child must then be SIGKILLed
    and fully reaped (no zombies), while CancelledError still propagates.
    """
    transport = _transport()
    try:
        async with _spawned(
            transport,
            [
                "sh",
                "-c",
                'trap "" TERM; sh -c "trap \\"\\" TERM; exec sleep 30" & wait',
            ],
        ) as proc:
            child = await _wait_for_child_exec(proc.pid, "sleep")
            children = {child}

            teardown = asyncio.create_task(
                transport._terminate_subprocess(proc, transport._process_group)
            )
            await asyncio.sleep(0.5)
            teardown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await teardown

            # Direct child SIGKILLed and reaped by the shielded final reap.
            assert proc.returncode == -9
            assert await _wait_for(lambda: _dead_and_reaped(proc.pid), timeout=3.0)
            # Descendants are killed by the group SIGKILL and reaped (by the
            # subreaper/init after reparenting) — no zombies left behind.
            for child in children:
                assert await _wait_for(lambda: _dead_and_reaped(child), timeout=3.0)
    finally:
        await transport.stop()


async def test_double_teardown_is_idempotent() -> None:
    transport = _transport()
    try:
        async with _spawned(transport, ["sh", "-c", "sleep 60 & wait"]) as proc:
            await transport._terminate_process()
            assert proc.returncode is not None
            # A second teardown with no process must be a no-op, and repeated
            # stop() calls must not cross-fire either.
            await transport._terminate_process()
            await transport.stop()
            await transport.stop()
            assert transport._process is None
            assert transport._process_group == 0
    finally:
        await transport.stop()


async def test_permission_error_falls_back_to_direct_child_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """killpg raising PermissionError routes signals to the direct child."""
    transport = _transport()
    proc = None
    try:
        proc = await transport._spawn_subprocess(["sleep", "60"], env=None)
        transport._process = proc

        def _denied(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", _denied)

        await transport._terminate_process()
        assert proc.returncode is not None
        assert not _pid_alive(proc.pid)
    finally:
        monkeypatch.undo()
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (OSError, asyncio.TimeoutError):
                pass
        await transport.stop()


async def test_permission_error_escalates_direct_child_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-child fallback still escalates to SIGKILL after grace."""
    transport = _transport()
    proc = None
    try:
        proc = await transport._spawn_subprocess(
            # exec preserves the ignored SIGTERM disposition, so the direct
            # child ignores terminate() and must be killed with SIGKILL; no
            # grandchild is left for the fallback path to orphan.
            ["sh", "-c", "trap '' TERM; exec sleep 60"],
            env=None,
        )
        transport._process = proc
        # Do not signal until the trap is installed and sleep is the image.
        assert await _wait_for(lambda: _comm(proc.pid) == "sleep", timeout=3.0)

        def _denied(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", _denied)
        # Keep the test fast without weakening the shared-deadline semantics.
        monkeypatch.setattr(process_module, "_TERMINATE_GRACE_SECONDS", 0.4)

        await transport._terminate_process()
        assert proc.returncode == -9
        assert not _pid_alive(proc.pid)
    finally:
        monkeypatch.undo()
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (OSError, asyncio.TimeoutError):
                pass
        await transport.stop()


def test_process_lookup_error_treated_as_group_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ESRCH from killpg means the group has already drained."""

    def _gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", _gone)
    # Signal attempted (TERM escalated to nothing, wait reaps the leader)...
    assert ProcessSession._signal_process_group(424242, signal.SIGTERM) is True
    assert ProcessSession._signal_process_group(424242, signal.SIGKILL) is True
    # ...and liveness polling reports the group empty.
    assert ProcessSession._process_group_alive(424242) is False


def test_permission_error_marks_group_signalling_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _denied(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", _denied)
    assert ProcessSession._signal_process_group(424242, signal.SIGTERM) is False
    # An unkillable foreign member still counts as alive for the deadline.
    assert ProcessSession._process_group_alive(424242) is True
