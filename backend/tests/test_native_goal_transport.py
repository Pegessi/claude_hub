"""Native Goal protocol contracts, using only fake RPC and notification streams."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream import tailer as tailer_module
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    TraexNativeSession,
)
from claude_hub.services.agent_stream.providers.contracts import ProviderGoal, ProviderRpcError
from claude_hub.services.agent_stream.tailer import SessionTailer, _GoalProtocolSanitizer


def _session() -> ManagedSession:
    return ManagedSession(
        id="native-goal-session",
        workspace_id="native-goal-workspace",
        tab_id="native-goal-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        title="Native Goal fixture",
        workspace_path="/tmp",
        tmux_session="unused-native-goal",
        target=ExecutionTarget.LOCAL,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _goal(status: str = "active") -> ProviderGoal:
    return {
        "threadId": "main-thread",
        "objective": "Complete the fixture",
        "status": status,
        "tokenBudget": None,
        "tokensUsed": 42,
        "timeUsedSeconds": 7,
        "createdAt": 100,
        "updatedAt": 107,
    }


def _replying_rpc(
    native: CodexNativeSession, reply: Optional[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Exercise the real JSON-RPC error branch without creating a subprocess."""
    requests: list[Dict[str, Any]] = []

    def write(data: bytes) -> None:
        request = json.loads(data)
        requests.append(request)
        if reply is not None:
            future = native._pending_requests.pop(request["id"])
            future.set_result({"id": request["id"], **reply})

    process = MagicMock(spec=asyncio.subprocess.Process)
    process.stdin = MagicMock()
    process.stdin.write.side_effect = write
    process.stdin.drain = AsyncMock()
    native._process = process
    native._thread_id = "main-thread"
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [ClaudeNativeSession, CursorNativeSession])
async def test_non_native_goal_contract_never_launches_a_provider(provider_type: Any) -> None:
    native = provider_type(_session())
    assert await native.goal_snapshot() is None
    with pytest.raises(NotImplementedError):
        await native.goal_set({"objective": "Goal"}, origin="user")
    with pytest.raises(NotImplementedError):
        await native.goal_clear(origin="user")
    assert native._process is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [CodexNativeSession, TraexNativeSession])
@pytest.mark.parametrize("goal", [None, _goal()], ids=["absent", "present"])
async def test_supported_snapshot_distinguishes_absence(
    monkeypatch: pytest.MonkeyPatch, provider_type: Any, goal: Any
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fixture/provider")
    native = provider_type(_session())
    requests = _replying_rpc(native, {"result": {"goal": goal}})
    assert native.capabilities().goal_execution_owner is None
    assert await native.goal_snapshot() == {"thread_id": "main-thread", "goal": goal}
    assert native.capabilities().goal_execution_owner == "provider_native"
    assert [item["method"] for item in requests] == ["thread/goal/get"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_error", [None, "unrelated provider failure"])
async def test_only_method_not_found_establishes_unsupported(
    monkeypatch: pytest.MonkeyPatch, prior_error: Optional[str]
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fixture/provider")
    native = CodexNativeSession(_session())
    requests = _replying_rpc(native, {"error": {"code": -32601, "message": "Method not found"}})
    native._last_error = prior_error
    assert await native.goal_snapshot() is None
    assert native.last_error == prior_error
    assert native.capabilities().goal_execution_owner == "hub_managed"
    assert await native.goal_snapshot() is None
    assert len(requests) == 1
    with pytest.raises(NotImplementedError):
        await native.goal_set({"status": "active"}, origin="user")
    with pytest.raises(NotImplementedError):
        await native.goal_clear(origin="user")
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        {"code": -32001, "message": "Unauthorized"},
        {"code": -32600, "message": "Invalid thread"},
        {"code": -32000, "message": "method not found in account policy"},
        {"code": "-32601", "message": "Malformed error code"},
    ],
    ids=["auth", "thread", "misleading-message", "invalid-code"],
)
async def test_rpc_errors_do_not_enable_hub_fallback(error: Dict[str, Any]) -> None:
    native = CodexNativeSession(_session())
    _replying_rpc(native, {"error": error})
    with pytest.raises(ProviderRpcError):
        await native.goal_snapshot()
    assert native._provider_goal_api_available is None


@pytest.mark.asyncio
async def test_timeout_does_not_enable_hub_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_module, "_STARTUP_GRACE_S", 0.01)
    native = CodexNativeSession(_session())
    _replying_rpc(native, None)
    with pytest.raises(RuntimeError, match="timed out"):
        await native.goal_snapshot()
    assert native._provider_goal_api_available is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"goal": []},
        {"goal": {**_goal(), "threadId": "other"}},
        {"goal": {**_goal(), "status": []}},
        {"goal": {**_goal(), "status": "cancelled"}},
        {"goal": {**_goal(), "tokensUsed": True}},
        {"goal": {**_goal(), "createdAt": "100"}},
    ],
    ids=[
        "null-response",
        "list-response",
        "missing-goal",
        "list-goal",
        "other-thread",
        "list-status",
        "unknown-status",
        "boolean-usage",
        "string-time",
    ],
)
async def test_malformed_snapshot_is_not_absence_or_unsupported(result: Any) -> None:
    native = CodexNativeSession(_session())
    _replying_rpc(native, {"result": result})
    with pytest.raises(RuntimeError):
        await native.goal_snapshot()
    assert native._provider_goal_api_available is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"]
)
async def test_native_goal_statuses_and_accounting_are_preserved(status: str) -> None:
    native = CodexNativeSession(_session())
    _replying_rpc(native, {"result": {"goal": _goal(status)}})
    snapshot = await native.goal_snapshot()
    assert snapshot is not None and snapshot["goal"] == _goal(status)


@pytest.mark.asyncio
async def test_snapshot_rejects_thread_rotation_during_probe() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"

    async def request(_method: str, _params: Dict[str, Any]) -> Dict[str, Any]:
        native._thread_id = "replacement-thread"
        return {"goal": _goal()}

    native._send_request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="thread changed"):
        await native.goal_snapshot()
    assert native._provider_goal_api_available is None


@pytest.mark.asyncio
async def test_stale_probe_error_cannot_disable_replacement_thread() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"

    async def request(_method: str, _params: Dict[str, Any]) -> Dict[str, Any]:
        native._thread_id = "replacement-thread"
        raise ProviderRpcError("Method not found", code=-32601)

    native._send_request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="thread changed"):
        await native.goal_snapshot()
    assert native._provider_goal_api_available is None


@pytest.mark.asyncio
async def test_probe_requires_an_adopted_thread_and_never_starts_it() -> None:
    native = CodexNativeSession(_session())
    native.start = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="initialized"):
        await native.goal_snapshot()
    native.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_handshake_resets_support_without_eager_goal_probe() -> None:
    native = CodexNativeSession(_session())
    native._provider_goal_api_available = False
    native._send_request = AsyncMock(return_value={})  # type: ignore[method-assign]
    native._send_notification = AsyncMock()  # type: ignore[method-assign]
    await native._initialize_protocol()
    assert native._provider_goal_api_available is None
    assert [item.args[0] for item in native._send_request.await_args_list] == ["initialize"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "automatic"])
async def test_set_returns_provider_result_and_sends_explicit_origin(origin: Any) -> None:
    native = CodexNativeSession(_session())
    result = _goal("paused")
    requests = _replying_rpc(native, {"result": {"goal": result}})
    assert await native.goal_set({"status": "paused"}, origin=origin) == result
    assert requests[0]["params"] == {
        "threadId": "main-thread",
        "status": "paused",
        "origin": origin,
    }
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", [True, False])
async def test_clear_preserves_supported_noop(cleared: bool) -> None:
    native = CodexNativeSession(_session())
    requests = _replying_rpc(native, {"result": {"cleared": cleared}})
    assert await native.goal_clear(origin="user") is cleared
    assert requests[0]["params"] == {"threadId": "main-thread", "origin": "user"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "clear"])
async def test_mutation_method_not_found_cannot_be_success(operation: str) -> None:
    native = CodexNativeSession(_session())
    _replying_rpc(native, {"error": {"code": -32601, "message": "Method not found"}})
    native._provider_goal_api_available = True
    with pytest.raises(NotImplementedError):
        if operation == "set":
            await native.goal_set({"status": "paused"}, origin="user")
        else:
            await native.goal_clear(origin="user")
    # A missing mutation method does not prove that reading/native execution is absent.
    assert native._provider_goal_api_available is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "clear"])
async def test_malformed_mutation_response_is_not_success(operation: str) -> None:
    native = CodexNativeSession(_session())
    _replying_rpc(native, {"result": {}})
    with pytest.raises(RuntimeError):
        if operation == "set":
            await native.goal_set({"status": "paused"}, origin="user")
        else:
            await native.goal_clear(origin="user")


@pytest.mark.asyncio
async def test_native_turn_id_capture_ignores_child_and_discarded_starts() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    await native._handle_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "main-thread", "turn": {"id": "native-turn"}},
        }
    )
    await native.read_line()
    assert native._provider_turn_id == "native-turn"
    native._discard_turn_id = "discarded-turn"
    for thread, turn in (("child-thread", "child-turn"), ("main-thread", "discarded-turn")):
        await native._handle_notification(
            {
                "method": "turn/started",
                "params": {"threadId": thread, "turn": {"id": turn}},
            }
        )
        if not native._notification_queue.empty():
            await native.read_line()
        assert native._provider_turn_id == "native-turn"


def _turn_record(method: str, turn_id: str) -> Dict[str, Any]:
    return {"method": method, "params": {"threadId": "main-thread", "turn": {"id": turn_id}}}


@pytest.mark.asyncio
async def test_native_ids_follow_consumption_and_replayed_retired_starts_are_ignored() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    for method, turn in (
        ("turn/started", "first"),
        ("turn/completed", "first"),
        ("turn/started", "second"),
    ):
        await native._handle_notification(_turn_record(method, turn))
    assert native._provider_turn_id is None
    await native.read_line()
    assert native._provider_turn_id == "first"
    await native.read_line()
    assert native._provider_turn_id == "first"
    await native.read_line()
    assert native._provider_turn_id == "second"
    await native._handle_notification(_turn_record("turn/started", "first"))
    assert native._notification_queue.empty()
    assert native._provider_turn_id == "second"


@pytest.mark.asyncio
async def test_turn_start_response_cannot_overwrite_a_consumed_native_continuation() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    native._started = True
    native._process = MagicMock(spec=asyncio.subprocess.Process)

    async def request(_method: str, _params: Dict[str, Any]) -> Dict[str, Any]:
        for method, turn in (
            ("turn/started", "first"),
            ("turn/completed", "first"),
            ("turn/started", "second"),
        ):
            await native._handle_notification(_turn_record(method, turn))
            await native.read_line()
        return {"turn": {"id": "first"}}

    native._send_request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    await native._send_text("user input")
    assert native._provider_turn_id == "second"


def test_retired_native_start_guard_is_bounded() -> None:
    native = CodexNativeSession(_session())
    for number in range(100):
        native._remember_retired_turn(_turn_record("turn/completed", str(number)))
    assert len(native._retired_provider_turn_ids) == 64
    assert not native.accepts_notification(_turn_record("turn/started", "99"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True], ids=["successor", "cold-start"])
async def test_stop_drains_queued_native_starts_and_preserves_other_threads(cold: bool) -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    if not cold:
        native._begin_turn()
        native._provider_turn_id = "first"
        native._notification_queue.put_nowait(_turn_record("turn/completed", "first"))
    native._notification_queue.put_nowait(_turn_record("turn/started", "second"))
    child = _turn_record("turn/started", "child-turn")
    child["params"]["threadId"] = "child-thread"
    goal = {
        "method": "thread/goal/updated",
        "params": {"threadId": "main-thread", "turnId": "second", "goal": _goal("paused")},
    }
    native._notification_queue.put_nowait(child)
    native._notification_queue.put_nowait(goal)

    async def interrupt(_method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert native.turn_in_flight
        assert params == {"threadId": "main-thread", "turnId": "second"}
        await native._handle_notification(_turn_record("turn/completed", "second"))
        return {}

    request = AsyncMock(side_effect=interrupt)
    native._send_request = request  # type: ignore[method-assign]
    await native.cancel_active_turn()
    request.assert_awaited_once()
    assert not native.turn_in_flight
    assert native._provider_turn_id is None
    assert await native.read_line() == child
    assert await native.read_line() == goal
    for turn in ("first", "second") if not cold else ("second",):
        await native._handle_notification(_turn_record("turn/started", turn))
    assert native._notification_queue.empty()


@pytest.mark.asyncio
async def test_old_completion_does_not_confirm_successor_interrupt() -> None:
    native = CodexNativeSession(_session())
    native._thread_id, native._provider_turn_id = "main-thread", "first"
    native._begin_turn()
    native._notification_queue.put_nowait(_turn_record("turn/started", "second"))
    interrupted = asyncio.Event()

    async def interrupt(_method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert params["turnId"] == "second"
        await native._handle_notification(_turn_record("turn/completed", "first"))
        interrupted.set()
        return {}

    native._send_request = AsyncMock(side_effect=interrupt)  # type: ignore[method-assign]
    stopped = asyncio.create_task(native.cancel_active_turn())
    try:
        await asyncio.wait_for(interrupted.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not stopped.done()
        assert native.turn_in_flight
        assert native._provider_turn_id == "second"
        await native._handle_notification(_turn_record("turn/completed", "second"))
        await asyncio.wait_for(stopped, timeout=1)
        assert not native.turn_in_flight
        assert native._notification_queue.empty()
    finally:
        if not stopped.done():
            stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_fences_successor_received_during_interrupt_and_pending_read() -> None:
    native = CodexNativeSession(_session())
    native._thread_id, native._provider_turn_id = "main-thread", "first"
    native._begin_turn()
    reader = asyncio.create_task(native.read_line())
    await asyncio.sleep(0)
    interrupted = []

    async def interrupt(_method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        turn_id = params["turnId"]
        interrupted.append(turn_id)
        await native._handle_notification(_turn_record("turn/completed", turn_id))
        if turn_id == "first":
            await native._handle_notification(_turn_record("turn/started", "second"))
        return {}

    native._send_request = AsyncMock(side_effect=interrupt)  # type: ignore[method-assign]
    try:
        await native.cancel_active_turn()
        assert interrupted == ["first", "second"]
        assert not native.turn_in_flight
        assert not reader.done()
        native._notification_queue.put_nowait(None)
        assert await asyncio.wait_for(reader, timeout=1) is None
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize(
    "failure", ["rpc-error", "missing-completion", "thread-changed", "process-changed"]
)
async def test_unconfirmed_native_stop_keeps_guard_and_does_not_restart(
    monkeypatch: pytest.MonkeyPatch, cold: bool, failure: str
) -> None:
    monkeypatch.setattr(native_module, "_STARTUP_GRACE_S", 0.01)
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    if not cold:
        native._provider_turn_id = "first"
        native._begin_turn()
    native._notification_queue.put_nowait(_turn_record("turn/started", "second"))
    restart = AsyncMock()
    native.restart_for_recovery = restart  # type: ignore[method-assign]

    async def interrupt(_method: str, _params: Dict[str, Any]) -> Dict[str, Any]:
        if failure == "rpc-error":
            raise RuntimeError("interrupt rejected")
        if failure == "thread-changed":
            native._thread_id = "replacement-thread"
        elif failure == "process-changed":
            native._process = MagicMock(spec=asyncio.subprocess.Process)
        return {}

    native._send_request = AsyncMock(side_effect=interrupt)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stop could not be confirmed"):
        await asyncio.wait_for(native.cancel_active_turn(), timeout=1)
    assert native.turn_in_flight
    assert native._provider_turn_id == "second"
    restart.assert_not_awaited()
    assert native._cancel_state is None


@pytest.mark.asyncio
async def test_continuing_native_successors_cannot_extend_stop_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_module, "_STARTUP_GRACE_S", 0.005)
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    native._notification_queue.put_nowait(_turn_record("turn/started", "0"))
    restart = AsyncMock()
    native.restart_for_recovery = restart  # type: ignore[method-assign]

    async def interrupt(_method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        turn_id = params["turnId"]
        await native._handle_notification(_turn_record("turn/completed", turn_id))
        await native._handle_notification(_turn_record("turn/started", str(int(turn_id) + 1)))
        return {}

    native._send_request = AsyncMock(side_effect=interrupt)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stop could not be confirmed"):
        await asyncio.wait_for(native.cancel_active_turn(), timeout=1)
    assert native.turn_in_flight
    assert len(native._discarded_provider_turn_ids) <= 64
    restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_without_inflight_or_queued_main_turn_does_not_launch_provider() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    request, restart = AsyncMock(), AsyncMock()
    native._send_request = request  # type: ignore[method-assign]
    native.restart_for_recovery = restart  # type: ignore[method-assign]
    await native.cancel_active_turn()
    assert not native.turn_in_flight
    request.assert_not_awaited()
    restart.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_id", [False, True], ids=["successor", "unidentified-start"])
async def test_retry_unconfirmed_native_stop_cannot_fall_back_to_ordinary_restart(
    missing_id: bool,
) -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    record = _turn_record("turn/started", "successor")
    if missing_id:
        record["params"]["turn"].pop("id")
    native._notification_queue.put_nowait(record)
    request = AsyncMock(side_effect=RuntimeError("interrupt rejected"))
    restart = AsyncMock()
    native._send_request = request  # type: ignore[method-assign]
    native.restart_for_recovery = restart  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop could not be confirmed"):
            await native.cancel_active_turn()
        assert native.turn_in_flight
        restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_accepts_matching_completion_received_after_unconfirmed_native_stop() -> None:
    native = CodexNativeSession(_session())
    native._thread_id = "main-thread"
    native._notification_queue.put_nowait(_turn_record("turn/started", "successor"))
    request = AsyncMock(side_effect=RuntimeError("interrupt rejected"))
    native._send_request = request  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stop could not be confirmed"):
        await native.cancel_active_turn()
    await native._handle_notification(_turn_record("turn/completed", "successor"))
    await native.cancel_active_turn()
    assert not native.turn_in_flight
    assert not native._native_stop_requires_confirmation
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_stop_clears_native_confirmation_requirement() -> None:
    native = CodexNativeSession(_session())
    native._native_stop_requires_confirmation = True
    await native.stop()
    assert not native._native_stop_requires_confirmation


class _NotificationCodex(CodexNativeSession):
    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self._end_turn()

    async def cancel_active_turn(self) -> None:
        self._end_turn()


@pytest.mark.asyncio
async def test_native_start_resets_old_protocol_and_notifies_after_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_module = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path)
    session = _session()
    native = _NotificationCodex(session)
    native._thread_id = "main-thread"
    observed: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    tailer: SessionTailer

    async def observer(event: AgentStreamEvent) -> None:
        page = await tailer._store.read_since(-1, limit=100)
        assert any(
            row.type == event.type and row.turn_id == event.turn_id and row.payload == event.payload
            for row in page.events
        )
        observed.put_nowait(event)

    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        session_getter=lambda: session,
        adapter=CodexJsonlAdapter(),
        native_transport=native,
        post_persist_observers=[observer],
    )
    tailer._terminalize_native_runtime("cancelled")
    tailer._goal_protocol_turn = True
    tailer._goal_protocol_sanitizer = _GoalProtocolSanitizer()
    await tailer.start()
    try:
        await native._handle_notification(
            {
                "method": "turn/started",
                "params": {"threadId": "main-thread", "turn": {"id": "native-turn"}},
            }
        )
        started = await asyncio.wait_for(observed.get(), timeout=2)
        assert started.type == AgentStreamEventType.TURN_STARTED
        assert tailer.native_terminal_status is None
        assert not tailer._goal_protocol_turn
        assert tailer._goal_protocol_sanitizer is None
        snapshot = tailer_module._native_runtime_snapshot(tailer)
        assert snapshot is not None and snapshot.status == AgentRuntimeStatus.WORKING
        await native._handle_notification(
            {
                "method": "thread/goal/updated",
                "params": {"threadId": "main-thread", "turnId": None, "goal": _goal()},
            }
        )
        updated = await asyncio.wait_for(observed.get(), timeout=2)
        assert updated.payload["provider_notification"] == "thread/goal/updated"
        await native._handle_notification(
            {
                "method": "thread/goal/cleared",
                "params": {"threadId": "child-thread"},
            }
        )
        await native._handle_notification(
            {
                "method": "thread/goal/cleared",
                "params": {"threadId": "main-thread"},
            }
        )
        cleared = await asyncio.wait_for(observed.get(), timeout=2)
        assert cleared.payload["goal"]["threadId"] == "main-thread"
        await native._handle_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "main-thread",
                    "turn": {"id": "native-turn", "status": "completed"},
                },
            }
        )
        completed = await asyncio.wait_for(observed.get(), timeout=2)
        assert completed.type == AgentStreamEventType.TURN_COMPLETED
        assert observed.empty()
    finally:
        await tailer.stop()
