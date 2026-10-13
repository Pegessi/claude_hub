"""HTTP contract and condensation regressions; every provider request is mocked."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    TerminalTabCreate,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.maso_events import MasoEventAdapter
from claude_hub.services.agent_stream.maso_http import MasoHttpSession
from claude_hub.services.agent_stream.native import create_native_session
from claude_hub.services.agent_stream.registry import get_adapter_for_session
from claude_hub.services.agent_stream.tailer import (
    SessionTailer,
    TailerManager,
    TranscriptForkError,
)
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess


def session(**updates: Any) -> ManagedSession:
    return ManagedSession(
        id=f"maso-test-{uuid.uuid4().hex}",
        workspace_id="maso-workspace",
        tab_id="maso-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="MASO test",
        workspace_path="/feature/worktree",
        tmux_session="inert",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ).model_copy(update=updates)


class EventStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    def emit(self, kind: str, data: dict, event_id: int = 1) -> None:
        self.queue.put_nowait(
            f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(data)}\n\n".encode()
        )

    async def __aiter__(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        self.closed = True


class MasoServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.streams: list[EventStream] = []
        self.running = False
        self.model = "seed"
        self.fail_send = False
        self.abort_entered: asyncio.Event | None = None
        self.abort_release: asyncio.Event | None = None
        self.pending: dict[str, str] = {}
        self.fail_abort = False
        self.delayed_enqueue = False
        self.delayed_message: dict | None = None
        self.send_entered: asyncio.Event | None = None
        self.send_release: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((request.method, path, body))
        if path == "/api/models":
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "seed",
                        "model_name": "seed-backend",
                        "selector_primary_key": "seed",
                        "selector_variants": [
                            {"key": "seed", "mode": "none", "label": "Disabled"},
                            {"key": "seed-high", "mode": "high", "label": "High"},
                        ],
                    },
                    {"key": "seed-high", "selector_primary_key": "seed"},
                    {"key": "retired", "deprecated": True},
                ],
            )
        if path == "/api/models/default":
            return httpx.Response(200, json={"key": "seed"})
        if path == "/api/sessions":
            return httpx.Response(201, json={"session_id": "opaque-sid", "agent_id": "main"})
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "agent_id": "main",
                    "is_running": self.running,
                    "status": "running" if self.running else "idle",
                    "model": self.model,
                },
            )
        if path.endswith("/model"):
            self.model = body["model"]
            return httpx.Response(200, json={"model": self.model})
        if path.endswith("/control"):
            if self.fail_abort:
                return httpx.Response(503, json={"error": "stop unavailable"})
            if self.abort_entered is not None and self.abort_release is not None:
                self.abort_entered.set()
                await self.abort_release.wait()
            self.running = bool(self.pending)
            return httpx.Response(200, json={"result": "aborting"})
        if request.method == "DELETE" and "/messages/pending/" in path:
            self.pending.pop(path.rsplit("/", 1)[-1], None)
            return httpx.Response(204)
        if path.endswith("/confirm"):
            return httpx.Response(200, json={"result": "confirmed"})
        if path.endswith("/stream"):
            stream = EventStream()
            self.streams.append(stream)
            # Old authoritative output and an in-flight snapshot are a
            # connection baseline, not fresh assistant output for the new turn.
            stream.emit("turn_end", {"agent_id": "main", "content": "old answer"}, 9)
            stream.emit(
                "in_flight_snapshot",
                {"streams": [{"agent_id": "main", "content": "old"}]},
                0,
            )
            stream.emit("indicator_snapshot", {"running": []}, 0)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        if path.endswith("/messages"):
            assert self.streams, "subscribe before posting the prompt"
            if self.send_entered is not None and self.send_release is not None:
                self.send_entered.set()
                await self.send_release.wait()
            if self.fail_send:
                if self.delayed_enqueue:
                    self.delayed_message = body
                else:
                    self.pending[body["message_id"]] = body["content"]
                raise httpx.ReadTimeout("ambiguous accepted POST with sk-sensitive-provider-body")
            self.running = True
            self.streams[-1].emit("agent_start", {"agent_id": "main"})
            self.streams[-1].emit("turn_start", {"agent_id": "main", "turn": 0})
            return httpx.Response(
                202, json={"status": "queued", "message": {"id": body["message_id"]}}
            )
        raise AssertionError(f"unexpected request {request.method} {path}")


def transport(server: MasoServer, record: ManagedSession | None = None) -> MasoHttpSession:
    result = MasoHttpSession(record or session())
    result._client = httpx.AsyncClient(
        base_url="http://maso.test",
        transport=httpx.MockTransport(server.handle),
        trust_env=False,
    )
    return result


def raw(kind: str, **data: Any) -> dict:
    return {"type": kind, "root_agent_id": "main", "data": {"agent_id": "main", **data}}


def test_offsets_snapshots_and_compression_are_not_new_turns() -> None:
    adapter = MasoEventAdapter()
    ctx = NormalizeContext(
        session_id="s",
        tab_id="t",
        agent_type=AgentType.MASO,
        run_epoch=1,
        turn_id="turn",
    )
    records = [
        raw("turn_start", turn=0),
        raw("llm_token", content="你😀", content_offset=0),
        raw("llm_token", content="😀好", content_offset=1),
        raw("llm_token", content="unseen gap", content_offset=9),
        raw("turn_end", id="block-1", content="你😀好，完整"),
        raw("context_condensing", estimated_tokens=100),
        raw("context_condensed", summary="do not display or replay this context"),
        raw("agent_end", agent_id="child", stop_reason="completed"),
        raw("turn_start", turn=1),
        raw("turn_end", id="block-2", content="之后"),
    ]
    events = [event for record in records for event in adapter.normalize_line(record, ctx)]
    assert (
        "".join(
            e.payload["text"]
            for e in events
            if e.type == AgentStreamEventType.TEXT_DELTA and not e.payload.get("subagent_thread")
        )
        == "你😀好，完整之后"
    )
    assert not any(e.type == AgentStreamEventType.TURN_COMPLETED for e in events)
    statuses = [e for e in events if e.payload.get("provider_status", "").startswith("context_")]
    assert [e.payload["text"] for e in statuses] == [
        "Condensing context…",
        "Context condensed",
    ]
    assert statuses[0].message_id == statuses[1].message_id
    assert (
        adapter.normalize_line(raw("agent_end", stop_reason="completed"), ctx)[0].type
        == AgentStreamEventType.TURN_COMPLETED
    )


@pytest.mark.asyncio
async def test_discovery_is_http_only_and_variants_use_provider_keys() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.start()
        await provider.prepare_capabilities()
        caps = provider.capabilities()
        assert caps.structured and not caps.supports_goals
        assert [m.id for m in caps.available_models] == ["seed"]
        assert (
            caps.available_models[0].supported_reasoning_efforts[1].provider_model_id == "seed-high"
        )
        assert caps.current_model == "seed"
        assert not any(path == "/api/sessions" for _, path, _ in server.calls)
        assert isinstance(create_native_session(session()), MasoHttpSession)
        assert isinstance(get_adapter_for_session(session()), MasoEventAdapter)
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_compression_keeps_display_history_and_session_across_turns(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "state"))
    server = MasoServer()
    record = session()
    provider = transport(server, record)
    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
    )
    queue = await tailer.subscribe_completion()

    async def complete() -> list:
        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), 2)
            events.append(event)
            if event.type == AgentStreamEventType.TURN_COMPLETED:
                return events

    try:
        await tailer.send_message("remember the marker", [], "first")
        stream = server.streams[-1]
        stream.emit(
            "llm_token",
            {"agent_id": "main", "content": "before", "content_offset": 0},
            0,
        )
        stream.emit("turn_end", {"agent_id": "main", "id": "a1", "content": "before"})
        stream.emit("context_condensing", {"agent_id": "main"})
        stream.emit(
            "context_condensed",
            {"agent_id": "main", "summary": "private model context"},
        )
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        assert (await complete())[-1].turn_id == "first"
        await tailer.send_message("next question", [], "second")
        server.streams[-1].emit("turn_end", {"agent_id": "main", "id": "a2", "content": "after"})
        server.running = False
        server.streams[-1].emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        assert (await complete())[-1].turn_id == "second"
        page = await tailer._store.read_since(0, 100)
        texts = [
            e.payload["text"] for e in page.events if e.type == AgentStreamEventType.TEXT_DELTA
        ]
        assert texts == ["before", "after"]
        calls = [body for method, path, body in server.calls if path.endswith("/messages")]
        assert len([1 for _, path, _ in server.calls if path == "/api/sessions"]) == 1
        assert calls[1]["content"] == "next question"  # no Hub transcript/summary replay
        assert not provider.turn_in_flight
    finally:
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


@pytest.mark.asyncio
async def test_ambiguous_post_is_not_retried_and_guard_is_released() -> None:
    server = MasoServer()
    server.fail_send = True
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await provider.send_message("single prompt", [])
        assert not provider.turn_in_flight
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        assert any(path.endswith("/control") for _, path, _ in server.calls)
        assert server.streams[-1].closed
        assert not server.pending and not server.running
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("no retry", [])
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_cancel_rejects_late_notifications_and_resumes_same_session() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("first", [])
        old_generation = provider._stdout_generation
        await provider.cancel_active_turn()
        assert not provider.accepts_notification({"generation": old_generation})
        assert not provider.turn_in_flight
        provider.update_env({"MASO_MODEL": "seed-high"})
        await provider.send_message("second", [])
        assert server.model == "seed-high"
        assert len([1 for _, path, _ in server.calls if path == "/api/sessions"]) == 1
        assert "<claude_hub_instructions>" not in server.calls[-1][2]["content"]
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_verified_session_resume_does_not_create_or_replay_hub_context() -> None:
    server = MasoServer()
    server.running = True  # execution left running by the previous Hub
    provider = transport(
        server, session(agent_session_id="opaque-sid", agent_session_id_verified=True)
    )
    try:
        await provider.send_message("new prompt", [])
        assert not any(path == "/api/sessions" for _, path, _ in server.calls)
        assert any(path.endswith("/control") for _, path, _ in server.calls)
        assert provider.active_thread_id == "opaque-sid"
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_stop_fences_late_events_before_abort_is_confirmed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "state"))
    server = MasoServer()
    server.abort_entered = asyncio.Event()
    server.abort_release = asyncio.Event()
    record = session()
    provider = transport(server, record)
    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
    )
    queue = await tailer.subscribe_completion()
    try:
        await tailer.send_message("stop me", [], "stopped")
        generation = provider._stdout_generation
        assert await tailer.cancel_turn("stopped")
        await asyncio.wait_for(server.abort_entered.wait(), 2)
        assert server.streams[-1].closed
        late_events: list[tuple[str, dict[str, Any]]] = [
            ("llm_token", {"content": "late output", "content_offset": 0}),
            ("agent_end", {"stop_reason": "aborted"}),
        ]
        for kind, data in late_events:
            late = {**raw(kind, **data), "generation": generation}
            assert not provider.accepts_notification(late)
            await provider._stdout_queue.put((generation, late))
        server.abort_release.set()
        await tailer._await_turn_teardown()
        page = await tailer._store.read_since(0, 100)
        assert all(e.turn_id == "stopped" for e in page.events)
        assert not any(e.type == AgentStreamEventType.TEXT_DELTA for e in page.events)
        assert len([e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]) == 1
    finally:
        server.abort_release.set()
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


@pytest.mark.asyncio
async def test_tool_confirmation_routes_to_http_without_another_prompt() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("work", [])
        server.streams[-1].emit(
            "confirmation_required",
            {
                "agent_id": "child",
                "tool_call_id": "call-1",
                "tool": "bash",
                "args": "dangerous command",
            },
        )
        while True:
            record = await asyncio.wait_for(provider.read_line(), 2)
            if record is not None and record["type"] == "confirmation_required":
                break
        adapter = MasoEventAdapter()
        ctx = NormalizeContext(
            session_id="s",
            tab_id="t",
            agent_type=AgentType.MASO,
            run_epoch=1,
            turn_id="turn",
        )
        card = adapter.normalize_line(record, ctx)[0]
        assert card.type == AgentStreamEventType.APPROVAL_REQUIRED
        assert card.payload["subagent_thread"] == "child"
        assert await provider.answer_pending_question(
            [{"questionId": "maso-confirm:call-1", "selected": ["Reject"]}]
        )
        assert server.calls[-1][2] == {
            "tool_call_id": "call-1",
            "confirmed": False,
            "agent_id": "child",
        }
        assert provider.turn_in_flight
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_stream_disconnect_fails_and_stops_run_without_retrying_prompt() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("first", [])
        server.streams[-1].queue.put_nowait(None)
        while True:
            record = await asyncio.wait_for(provider.read_line(), 2)
            assert record is not None
            if record["type"] == "hub_transport_error":
                break
        assert not server.running
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        provider.acknowledge_turn_complete()
        await provider.send_message("try next turn", [])
        assert len(server.streams) == 2
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_chat_only_and_unsupported_edit_are_rejected_before_mutation() -> None:
    with pytest.raises(ValidationError, match="HTTP Chat"):
        TerminalTabCreate(name="bad", agent_type=AgentType.MASO)
    assert TerminalTabCreate(name="good", agent_type=AgentType.MASO, session_kind=SessionKind.CHAT)
    process = TTYDProcess("id", 12345, "unchanged", agent_type=AgentType.TERMINAL)
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {"id": process}
    manager._start_locks = {}
    with pytest.raises(ValueError, match="MASO provider"):
        await manager.update_tab("id", name="changed", agent_type=AgentType.MASO)
    assert process.name == "unchanged" and process.agent_type == AgentType.TERMINAL
    tailers = TailerManager(lambda _id: session())
    with pytest.raises(TranscriptForkError, match="MASO HTTP"):
        await tailers.edit_resend(session(), "edit", "new", "old")
    assert not tailers._tailers  # unsupported edit must never stop/create a transport


@pytest.mark.asyncio
async def test_abort_failure_recovers_cleanup_before_any_new_prompt() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("first", [])
        server.fail_abort = True
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await provider.cancel_active_turn()
        assert not provider.turn_in_flight
        assert provider._needs_abort and provider._delivery_path.exists()
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await provider.send_message("must not reach service", [])
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        server.fail_abort = False
        await provider.send_message("second", [])
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 2
    finally:
        server.fail_abort = False
        await provider.stop()


@pytest.mark.asyncio
async def test_restart_with_pending_delivery_removes_only_owned_queue_item() -> None:
    server = MasoServer()
    record = session(agent_session_id="opaque-sid", agent_session_id_verified=True)
    first = transport(server, record)
    message_id = str(uuid.uuid4())
    first._save_delivery(message_id)
    server.pending[message_id] = "old unconsumed prompt"
    assert first._client is not None
    await first._client.aclose()
    restarted = transport(server, record)
    try:
        await restarted.send_message("new prompt", [])
        assert not server.pending
        assert not any(path == "/api/sessions" for _, path, _ in server.calls)
        delete = next(i for i, (method, _, _) in enumerate(server.calls) if method == "DELETE")
        send = next(i for i, (_, path, _) in enumerate(server.calls) if path.endswith("/messages"))
        assert delete < send
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_post_enqueued_after_timeout_keeps_chat_blocked_across_restart() -> None:
    server = MasoServer()
    server.fail_send = True
    server.delayed_enqueue = True
    record = session()
    provider = transport(server, record)
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await provider.send_message("delayed prompt", [])
        assert not server.pending
        # The service handler resumes after DELETE+abort already saw idle.
        assert server.delayed_message is not None
        server.pending[server.delayed_message["message_id"]] = "delayed prompt"
        await provider.stop()
        resumed = transport(
            server,
            record.model_copy(
                update={"agent_session_id": "opaque-sid", "agent_session_id_verified": True}
            ),
        )
        try:
            with pytest.raises(RuntimeError, match="Further sends are refused"):
                await resumed.send_message("do not wake the old prompt", [])
            assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
            assert resumed._delivery_uncertain
        finally:
            await resumed.stop()
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_stream_drops_before_successful_post_response_preserves_uncertainty() -> None:
    server = MasoServer()
    server.send_entered = asyncio.Event()
    server.send_release = asyncio.Event()
    provider = transport(server)
    send = asyncio.create_task(provider.send_message("delayed acceptance", []))
    try:
        await asyncio.wait_for(server.send_entered.wait(), 2)
        server.streams[-1].queue.put_nowait(None)
        for _ in range(100):
            if provider._stream_failed_generation == provider._stdout_generation:
                break
            await asyncio.sleep(0.001)
        assert provider._stream_failed_generation == provider._stdout_generation
        server.send_release.set()
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await send
        assert not server.running
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("never automatically retry", [])
    finally:
        server.send_release.set()
        await asyncio.gather(send, return_exceptions=True)
        await provider.stop()
