"""Provider subprocesses run in their own process group and are reaped as a tree.

Provider CLIs (Claude/Cursor/Codex) spawn their own children — bash tool
shells, MCP servers — which must not be orphaned when the transport tears the
direct child down.
"""

from __future__ import annotations

import asyncio
import os
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


async def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


async def test_terminate_kills_grandchild_process_group() -> None:
    transport = _transport()
    try:
        proc = await transport._spawn_subprocess(["sh", "-c", "sleep 60 & wait"], env=None)
        transport._process = proc
        try:
            assert transport._process_group == proc.pid
            assert os.getpgid(proc.pid) == proc.pid
            assert await _wait_for(lambda: bool(_child_pids(proc.pid)))
            grandchild = next(iter(_child_pids(proc.pid)))

            await transport._terminate_process()

            assert proc.returncode is not None
            assert await _wait_for(lambda: not _pid_alive(proc.pid), timeout=2.0)
            assert await _wait_for(lambda: not _pid_alive(grandchild), timeout=2.0)
            assert transport._process is None
            assert transport._process_group == 0
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                await proc.wait()
    finally:
        await transport.stop()


async def test_terminate_without_group_targets_direct_child() -> None:
    transport = _transport()
    try:
        proc = await transport._spawn_subprocess(["sleep", "60"], env=None)
        transport._process = proc
        try:
            # Simulate a process that was not spawned in its own session
            # (non-POSIX fallback path): only the direct child is signalled.
            transport._process_group = 0
            await transport._terminate_process()
            assert proc.returncode is not None
            assert not _pid_alive(proc.pid)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    finally:
        await transport.stop()


async def test_terminate_sigkills_group_after_grace_period() -> None:
    transport = _transport()
    try:
        # The child ignores SIGTERM and respawns its grandchild whenever that
        # child exits, so the group survives the grace period and SIGKILL is
        # required to reap the parent.
        proc = await transport._spawn_subprocess(
            ["sh", "-c", "trap '' TERM; while true; do sleep 60 & wait; done"],
            env=None,
        )
        transport._process = proc
        try:
            assert await _wait_for(lambda: bool(_child_pids(proc.pid)))
            await transport._terminate_process()
            assert proc.returncode is not None
            assert not _pid_alive(proc.pid)
            assert all(not _pid_alive(pid) for pid in _child_pids(proc.pid))
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                await proc.wait()
    finally:
        await transport.stop()
