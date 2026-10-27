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


def _patch_ttyd_cleanup(monkeypatch: pytest.MonkeyPatch, cleanup: Any = None) -> None:
    monkeypatch.setattr(
        workspaces_module,
        "ttyd_manager",
        SimpleNamespace(
            schedule_pending_stream_cleanup=cleanup if cleanup is not None else (lambda: None)
        ),
    )


def _make_fake(calls: list[tuple[str, str]], **overrides: Any) -> SimpleNamespace:
    """Fake manager exposing every attribute the monitor loop touches.

    Defaults are no-ops; the batched status fetch and the final drain record a
    call so per-round ordering stays observable.
    """

    async def _collect_statuses(_sessions: list[Any]) -> dict[str, Any]:
        calls.append(("refresh_session_statuses", ""))
        return {}

    async def _refresh_one(_session: Any, _statuses: dict[str, Any]) -> bool:
        return False

    def _mark_failures(_workspace_id: Any, _now: Any) -> bool:
        return False

    def _expire(_session_id: str) -> None:
        return None

    async def _lifecycle(workspace_id: str) -> None:
        calls.append(("tick_task_lifecycles", workspace_id))

    def _due(_workspace: Any, _now: Any) -> bool:
        return bool(overrides.get("resident_due", False))

    async def _run_resident(workspace: Any) -> None:
        calls.append(("tick_resident_agents", getattr(workspace, "id", "")))

    async def _recover_chat_runs() -> None:
        return None

    def _disable_deleted(_now: Any) -> None:
        return None

    async def _reap(_now: Any) -> None:
        return None

    async def _fire(task: Any, _now: Any) -> None:
        calls.append(("fire_scheduled_task", task.id))

    async def _converge(_now: Any) -> None:
        return None

    async def _drain() -> None:
        calls.append(("tick_scheduled_tasks", ""))

    fake = SimpleNamespace(
        sessions={},
        workspaces={},
        scheduled_tasks={},
        _scheduled_chat_recovery_pending=False,
        _runtime_now=lambda: 0.0,
        _monitor_refresh_sessions=lambda _workspace_id=None: list(
            overrides.get("sessions", {}).values()
        ),
        _collect_session_agent_statuses=overrides.get(
            "_collect_session_agent_statuses", _collect_statuses
        ),
        _refresh_one_session_status=overrides.get("_refresh_one_session_status", _refresh_one),
        _mark_runtime_task_failures=overrides.get("_mark_runtime_task_failures", _mark_failures),
        _save_state=lambda: None,
        _expire_processing_leases=_expire,
        dispatch_workspace=overrides.get(
            "dispatch_workspace",
            lambda _workspace_id, **_kwargs: calls.append(("dispatch_workspace", _workspace_id)),
        ),
        _tick_task_lifecycles=overrides.get("_tick_task_lifecycles", _lifecycle),
        _resident_agent_due=_due,
        _run_resident_agent=overrides.get("_run_resident_agent", _run_resident),
        _recover_scheduled_chat_runs=_recover_chat_runs,
        _disable_deleted_chat_targets=_disable_deleted,
        _reap_stale_scheduled_chat_runs=_reap,
        _fire_scheduled_task=overrides.get("_fire_scheduled_task", _fire),
        _converge_orphaned_hub_tasks=_converge,
        _drain_scheduled_chat_runs=_drain,
        _kick_feedback_automation=overrides.get(
            "_kick_feedback_automation",
            lambda: calls.append(("kick_feedback_automation", "")),
        ),
    )
    if "sessions" in overrides:
        fake.sessions = overrides["sessions"]
    if "workspaces" in overrides:
        fake.workspaces = overrides["workspaces"]
    if "scheduled_tasks" in overrides:
        fake.scheduled_tasks = overrides["scheduled_tasks"]
    if "chat_recovery_pending" in overrides:
        fake._scheduled_chat_recovery_pending = overrides["chat_recovery_pending"]
    if "runtime_now" in overrides:
        fake._runtime_now = overrides["runtime_now"]
    return fake


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


async def _run_exact_rounds(fake: Any, rounds: int, calls: list[tuple[str, str]]) -> None:
    """Run exactly ``rounds`` monitor rounds, gated at the final drain step.

    Unlike ``_stop_after_rounds`` this cannot overshoot when the monitor
    interval is tiny: every round blocks inside its drain until the test
    releases it, so dispatch attempt counts stay deterministic.
    """
    round_finished = asyncio.Event()
    may_continue = asyncio.Event()

    async def gated_drain() -> None:
        calls.append(("tick_scheduled_tasks", ""))
        round_finished.set()
        await may_continue.wait()
        may_continue.clear()

    fake._drain_scheduled_chat_runs = gated_drain
    task = asyncio.create_task(_WorkspacesMixin._background_monitor_loop(fake))
    try:
        for index in range(rounds):
            await asyncio.wait_for(round_finished.wait(), timeout=5)
            round_finished.clear()
            # Leave the final round parked in the drain so no extra round (and
            # therefore no extra guarded step) can run before cancellation.
            if index < rounds - 1:
                may_continue.set()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_dispatch_failure_does_not_skip_lifecycle_or_other_workspaces(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, str]] = []

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        calls.append(("dispatch_workspace", workspace_id))
        if workspace_id == "ws-a":
            raise RuntimeError(f"boom dispatch {workspace_id}")

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a"), "ws-b": SimpleNamespace(id="ws-b")},
        dispatch_workspace=dispatch,
        resident_due=True,
    )

    with caplog.at_level(logging.ERROR):
        await _stop_after_rounds(fake, 1, calls)

    # Dispatch was attempted for both workspaces and the failing one did not
    # skip its own lifecycle step, the other workspace, or the later steps.
    assert [call for call in calls if call[0] != "schedule_pending_stream_cleanup"] == [
        ("refresh_session_statuses", ""),
        ("dispatch_workspace", "ws-a"),
        ("tick_task_lifecycles", "ws-a"),
        ("dispatch_workspace", "ws-b"),
        ("tick_task_lifecycles", "ws-b"),
        ("tick_resident_agents", "ws-a"),
        ("tick_resident_agents", "ws-b"),
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

    async def collect(_sessions: list[Any]) -> dict[str, Any]:
        refresh_started.set()
        # Block until the monitor task is cancelled from outside, exactly like
        # stop_background_monitor does at shutdown.
        await asyncio.Event().wait()
        return {}

    _patch_ttyd_cleanup(
        monkeypatch, cleanup=lambda: calls.append(("schedule_pending_stream_cleanup", ""))
    )
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a")},
        _collect_session_agent_statuses=collect,
    )

    task = asyncio.create_task(_WorkspacesMixin._background_monitor_loop(fake))
    await asyncio.wait_for(refresh_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The loop ended at the cancelled step without running later steps.
    assert calls == [("schedule_pending_stream_cleanup", "")]


@pytest.mark.asyncio
async def test_hung_resident_is_cancelled_per_workspace_and_round_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS", 0.05)
    calls: list[tuple[str, str]] = []
    resident_saw_cancellation = False

    async def run_resident(_workspace: Any) -> None:
        nonlocal resident_saw_cancellation
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resident_saw_cancellation = True
            raise

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a")},
        resident_due=True,
        _run_resident_agent=run_resident,
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
    message = timeout_logs[0].getMessage()
    assert "tick_resident_agents" in message
    assert "workspace_id=ws-a" in message


@pytest.mark.asyncio
async def test_hung_scheduled_task_does_not_cancel_later_tasks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Per-scope budgets: task-a hanging must burn only its own timeout; task-b
    # and the global sweep/drain phases still run within the same round.
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS", 0.05)
    calls: list[tuple[str, str]] = []
    observed = {"task_a_cancelled": False}

    task_a = SimpleNamespace(id="task-a", enabled=True, next_run_at=0.0)
    task_b = SimpleNamespace(id="task-b", enabled=True, next_run_at=0.0)

    async def fire(task: Any, _now: Any) -> None:
        if task.id == "task-a":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observed["task_a_cancelled"] = True
                raise
        calls.append(("fire_scheduled_task", task.id))

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        runtime_now=lambda: 100.0,
        scheduled_tasks={"task-a": task_a, "task-b": task_b},
        _fire_scheduled_task=fire,
    )

    with caplog.at_level(logging.WARNING):
        await _stop_after_rounds(fake, 1, calls)

    assert observed["task_a_cancelled"]
    # task-b fired despite task-a's timeout, and later phases still ran.
    assert ("fire_scheduled_task", "task-b") in calls
    assert ("tick_scheduled_tasks", "") in calls
    assert ("kick_feedback_automation", "") in calls
    timeout_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "timed out" in record.getMessage()
    ]
    assert len(timeout_logs) == 1
    message = timeout_logs[0].getMessage()
    assert "tick_scheduled_tasks" in message
    assert "task_id=task-a" in message
    assert "task-b" not in message


@pytest.mark.asyncio
async def test_repeated_identical_step_failures_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    calls: list[tuple[str, str]] = []

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        raise RuntimeError("persistent boom")

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a")},
        dispatch_workspace=dispatch,
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


@pytest.mark.asyncio
async def test_success_evicts_rate_limit_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Fail round 1, succeed round 2, fail round 3: the second failure must log
    # a fresh traceback instead of being suppressed as a repeat.
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    calls: list[tuple[str, str]] = []
    dispatch_attempts = {"count": 0}

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        dispatch_attempts["count"] += 1
        if dispatch_attempts["count"] in (1, 3):
            raise RuntimeError("intermittent boom")

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a")},
        dispatch_workspace=dispatch,
    )

    with caplog.at_level(logging.ERROR):
        await _run_exact_rounds(fake, 3, calls)

    dispatch_failures = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.getMessage().startswith("Workspace background monitor step dispatch_workspace")
    ]
    assert len(dispatch_failures) == 2
    assert dispatch_attempts["count"] == 3
    # Neither failure may claim suppressed repeats: the intervening success
    # evicted the rate-limit bucket.
    assert all("suppressed" not in record.getMessage() for record in dispatch_failures)


@pytest.mark.asyncio
async def test_sync_guard_contains_raising_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A raising synchronous step is contained exactly like an awaitable one:
    # the rest of the round still runs and the outer backstop stays quiet.
    def raising_cleanup() -> None:
        raise RuntimeError("boom cleanup")

    calls: list[tuple[str, str]] = []
    _patch_ttyd_cleanup(monkeypatch, cleanup=raising_cleanup)
    fake = _make_fake(calls)

    with caplog.at_level(logging.ERROR):
        await _stop_after_rounds(fake, 1, calls)

    assert ("refresh_session_statuses", "") in calls
    assert ("tick_scheduled_tasks", "") in calls
    failure_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.getMessage().startswith(
            "Workspace background monitor step schedule_pending_stream_cleanup"
        )
    ]
    assert len(failure_logs) == 1
    assert failure_logs[0].exc_info is not None
    assert not any(
        "Workspace background monitor failed" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_suppressed_count_flushes_after_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With a tiny error-log window, the first failure logged after the window
    # reports how many identical repeats were hidden in between.
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 0.05)
    calls: list[tuple[str, str]] = []

    async def dispatch(workspace_id: str, **_kwargs: Any) -> None:
        raise RuntimeError("persistent boom")

    _patch_ttyd_cleanup(monkeypatch)
    fake = _make_fake(
        calls,
        workspaces={"ws-a": SimpleNamespace(id="ws-a")},
        dispatch_workspace=dispatch,
    )

    with caplog.at_level(logging.ERROR):
        await _stop_after_rounds(fake, 60, calls)

    dispatch_failures = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.getMessage().startswith("Workspace background monitor step dispatch_workspace")
    ]
    assert len(dispatch_failures) >= 2
    summaries = [
        record.getMessage()
        for record in dispatch_failures
        if "prior identical failure log(s) suppressed" in record.getMessage()
    ]
    assert summaries, "expected a post-window suppressed-count summary"
    assert any("workspace_id=ws-a" in message for message in summaries)


def test_error_log_state_rate_limits_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Identical failures inside the window are suppressed; the first failure
    # past the window emits with the count of hidden repeats.
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    state = workspaces_module._StepErrorLogState()

    assert state.record_failure("step", "scope", "RuntimeError", now=0.0) == (True, 0)
    assert state.record_failure("step", "scope", "RuntimeError", now=10.0) == (False, 0)
    assert state.record_failure("step", "scope", "RuntimeError", now=20.0) == (False, 0)
    assert state.record_failure("step", "scope", "RuntimeError", now=60.5) == (True, 2)
    # After the flush the bucket is fresh: suppression starts over.
    assert state.record_failure("step", "scope", "RuntimeError", now=70.0) == (False, 0)


def test_error_log_state_distinguishes_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    state = workspaces_module._StepErrorLogState()

    assert state.record_failure("step-a", "", "RuntimeError", now=0.0) == (True, 0)
    assert state.record_failure("step-b", "", "RuntimeError", now=0.0) == (True, 0)
    assert state.record_failure("step", "scope-a", "RuntimeError", now=0.0) == (True, 0)
    assert state.record_failure("step", "scope-b", "TimeoutError", now=0.0) == (True, 0)
    # A different failure type under the same (step, scope) gets its own bucket.
    assert state.record_failure("step", "scope-b", "RuntimeError", now=0.0) == (True, 0)
    assert state.record_failure("step", "scope-b", "TimeoutError", now=10.0) == (False, 0)


def test_error_log_state_success_clears_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    state = workspaces_module._StepErrorLogState()

    state.record_failure("step", "scope-a", "RuntimeError", now=0.0)
    state.record_failure("step", "scope-a", "TimeoutError", now=0.0)
    state.record_failure("step", "scope-b", "RuntimeError", now=0.0)

    state.clear_scope("step", "scope-a")

    # Every failure-type bucket for the recovered scope is evicted, so the next
    # failure logs fresh; the other scope's bucket is untouched.
    assert state.record_failure("step", "scope-a", "RuntimeError", now=10.0) == (True, 0)
    assert state.record_failure("step", "scope-b", "RuntimeError", now=10.0) == (False, 0)


def test_error_log_state_prunes_idle_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bucket whose failure stream stopped (its scope entity was deleted)
    # must age out, while a bucket still inside the retention window survives.
    monkeypatch.setattr(workspaces_module, "WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS", 60.0)
    state = workspaces_module._StepErrorLogState()

    # scope-gone fails at t=0 with two suppressed repeats, then goes silent.
    state.record_failure("step", "scope-gone", "RuntimeError", now=0.0)
    state.record_failure("step", "scope-gone", "RuntimeError", now=10.0)
    state.record_failure("step", "scope-gone", "RuntimeError", now=20.0)
    # scope-fresh fails again at t=450, so its bucket is active at t=500.
    state.record_failure("step", "scope-fresh", "RuntimeError", now=0.0)
    state.record_failure("step", "scope-fresh", "RuntimeError", now=450.0)

    state.prune_idle(now=400.0, retention_seconds=480.0)
    # Neither bucket is old enough yet: the stale scope still carries its
    # stranded suppressed-repeat count, which flushes on its next failure.
    assert state.record_failure("step", "scope-gone", "RuntimeError", now=400.0) == (True, 2)
    # That flush refreshed its bucket; a second prune must not drop it now.
    state.prune_idle(now=500.0, retention_seconds=480.0)
    assert state.record_failure("step", "scope-gone", "RuntimeError", now=410.0) == (False, 0)
    assert state.record_failure("step", "scope-fresh", "RuntimeError", now=455.0) == (False, 0)

    # After a fresh silent gap longer than retention, only the idle bucket is
    # pruned: the next failure for the deleted scope logs as brand new (its
    # ancient repeat count is discarded), proving per-scope identifiers cannot
    # accumulate in the map forever; an active bucket is unaffected.
    # scope-fresh flushes again at t=900 (reporting the repeat hidden at
    # 455); scope-gone has been idle since t=400.
    assert state.record_failure("step", "scope-fresh", "RuntimeError", now=900.0) == (True, 1)
    state.prune_idle(now=1000.0, retention_seconds=480.0)
    assert state.record_failure("step", "scope-gone", "RuntimeError", now=1000.0) == (True, 0)
    assert state.record_failure("step", "scope-fresh", "RuntimeError", now=905.0) == (False, 0)
