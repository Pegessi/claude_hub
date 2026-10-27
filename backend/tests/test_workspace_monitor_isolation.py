"""Background monitor steps are isolated: one failure/hang must not skip the rest.

The real monitor runs on ``_WorkspacesMixin``; these tests drive
``_background_monitor_loop`` directly with a fake manager that only implements
the attributes the loop touches, so step order and failure containment are
asserted without spinning up tmux/ttyd.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest

# The ``workspace_manager`` singleton exported by ``claude_hub.services``
# shadows the package attribute of the same name, so the submodule must be
# fetched through importlib rather than a dotted ``import ... as`` binding.
workspaces_module = importlib.import_module("claude_hub.services.workspace_manager._workspaces")
_WorkspacesMixin = workspaces_module._WorkspacesMixin


def _patch_ttyd_cleanup(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str]]) -> None:
    monkeypatch.setattr(
        workspaces_module,
        "ttyd_manager",
        SimpleNamespace(
            schedule_pending_stream_cleanup=lambda: calls.append(
                ("schedule_pending_stream_cleanup", "")
            )
        ),
    )


async def _stop_after_rounds(
    fake: Any, rounds: int, calls: list[tuple[str, str]], marker: str = "tick_scheduled_tasks"
) -> None:
    """Run the loop until ``marker`` fired ``rounds`` times, then cancel it."""
    task = asyncio.create_task(_WorkspacesMixin._background_monitor_loop(fake))
    try:
        for _ in range(1000):
            if sum(1 for step, _ in calls if step == marker) >= rounds:
                return
            await asyncio.sleep(0.005)
        pytest.fail("monitor loop did not complete the expected rounds")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_dispatch_failure_does_not_skip_lifecycle_or_other_workspaces(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, str]] = []

    async def refresh(**_kwargs: Any) -> None:
        calls.append(("refresh_session_statuses", ""))

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        calls.append(("dispatch_workspace", workspace_id))
        if workspace_id == "ws-a":
            raise RuntimeError(f"boom dispatch {workspace_id}")

    async def lifecycle(workspace_id: str) -> None:
        calls.append(("tick_task_lifecycles", workspace_id))

    async def resident() -> None:
        calls.append(("tick_resident_agents", ""))

    async def scheduled() -> None:
        calls.append(("tick_scheduled_tasks", ""))

    def feedback() -> None:
        calls.append(("kick_feedback_automation", ""))

    _patch_ttyd_cleanup(monkeypatch, calls)
    fake = SimpleNamespace(
        sessions={},
        workspaces={"ws-a": object(), "ws-b": object()},
        _refresh_session_statuses=refresh,
        dispatch_workspace=dispatch,
        _tick_task_lifecycles=lifecycle,
        _tick_resident_agents=resident,
        _tick_scheduled_tasks=scheduled,
        _kick_feedback_automation=feedback,
    )

    with caplog.at_level(logging.ERROR):
        await _stop_after_rounds(fake, 1, calls)

    # Dispatch was attempted for both workspaces and the failing one did not
    # skip its own lifecycle step, the other workspace, or the global steps.
    assert [call for call in calls if call[0] != "schedule_pending_stream_cleanup"] == [
        ("refresh_session_statuses", ""),
        ("dispatch_workspace", "ws-a"),
        ("tick_task_lifecycles", "ws-a"),
        ("dispatch_workspace", "ws-b"),
        ("tick_task_lifecycles", "ws-b"),
        ("tick_resident_agents", ""),
        ("tick_scheduled_tasks", ""),
        ("kick_feedback_automation", ""),
    ]

    failure_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "dispatch_workspace" in record.getMessage()
    ]
    assert len(failure_logs) == 1
    message = failure_logs[0].getMessage()
    assert "workspace_id=ws-a" in message
    assert failure_logs[0].exc_info is not None
    # The outer backstop must not have fired: the guard contained the failure.
    assert not any(
        "Workspace background monitor failed" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cancelled_error_stops_monitor_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    refresh_started = asyncio.Event()

    async def refresh(**_kwargs: Any) -> None:
        refresh_started.set()
        # Block until the monitor task is cancelled from outside, exactly like
        # stop_background_monitor does at shutdown.
        await asyncio.Event().wait()

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        calls.append(("dispatch_workspace", workspace_id))

    async def lifecycle(_workspace_id: str) -> None:
        return None

    async def resident() -> None:
        return None

    async def scheduled() -> None:
        calls.append(("tick_scheduled_tasks", ""))

    _patch_ttyd_cleanup(monkeypatch, calls)
    fake = SimpleNamespace(
        sessions={},
        workspaces={"ws-a": object()},
        _refresh_session_statuses=refresh,
        dispatch_workspace=dispatch,
        _tick_task_lifecycles=lifecycle,
        _tick_resident_agents=resident,
        _tick_scheduled_tasks=scheduled,
        _kick_feedback_automation=lambda: None,
    )

    task = asyncio.create_task(_WorkspacesMixin._background_monitor_loop(fake))
    await asyncio.wait_for(refresh_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The loop ended at the cancelled step without running later steps.
    assert calls == [("schedule_pending_stream_cleanup", "")]


@pytest.mark.asyncio
async def test_hung_step_is_cancelled_and_round_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS", 0.05)
    calls: list[tuple[str, str]] = []
    resident_started = asyncio.Event()
    resident_saw_cancellation = False

    async def refresh(**_kwargs: Any) -> None:
        calls.append(("refresh_session_statuses", ""))

    async def resident() -> None:
        nonlocal resident_saw_cancellation
        resident_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resident_saw_cancellation = True
            raise

    async def scheduled() -> None:
        calls.append(("tick_scheduled_tasks", ""))

    def feedback() -> None:
        calls.append(("kick_feedback_automation", ""))

    _patch_ttyd_cleanup(monkeypatch, calls)
    fake = SimpleNamespace(
        sessions={},
        workspaces={},
        _refresh_session_statuses=refresh,
        dispatch_workspace=None,
        _tick_task_lifecycles=None,
        _tick_resident_agents=resident,
        _tick_scheduled_tasks=scheduled,
        _kick_feedback_automation=feedback,
    )

    with caplog.at_level(logging.WARNING):
        await _stop_after_rounds(fake, 1, calls)

    assert resident_saw_cancellation
    assert ("tick_scheduled_tasks", "") in calls
    assert ("kick_feedback_automation", "") in calls
    timeout_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "timed out" in record.getMessage()
    ]
    assert len(timeout_logs) == 1
    assert "tick_resident_agents" in timeout_logs[0].getMessage()


@pytest.mark.asyncio
async def test_repeated_identical_step_failures_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    calls: list[tuple[str, str]] = []

    async def refresh(**_kwargs: Any) -> None:
        return None

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        raise RuntimeError("persistent boom")

    async def lifecycle(workspace_id: str) -> None:
        calls.append(("tick_task_lifecycles", workspace_id))

    async def resident() -> None:
        return None

    async def scheduled() -> None:
        calls.append(("tick_scheduled_tasks", ""))

    _patch_ttyd_cleanup(monkeypatch, calls)
    fake = SimpleNamespace(
        sessions={},
        workspaces={"ws-a": object()},
        _refresh_session_statuses=refresh,
        dispatch_workspace=dispatch,
        _tick_task_lifecycles=lifecycle,
        _tick_resident_agents=resident,
        _tick_scheduled_tasks=scheduled,
        _kick_feedback_automation=lambda: None,
    )

    with caplog.at_level(logging.ERROR):
        await _stop_after_rounds(fake, 3, calls)

    dispatch_failures = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.getMessage().startswith("Workspace background monitor step dispatch_workspace")
    ]
    # The same step failed on every round, but within the suppression window the
    # traceback is emitted only once.
    assert len(dispatch_failures) == 1
    assert "workspace_id=ws-a" in dispatch_failures[0].getMessage()
