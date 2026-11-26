"""HTTP contract and condensation regressions; every provider request is mocked."""

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ChatMode,
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
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import (
    SessionTailer,
    TailerManager,
    TranscriptForkError,
)
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

PNG_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


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
        env={"MASO_BASE_URL": "http://127.0.0.1:9796"},
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
        self.exit_entered: asyncio.Event | None = None
        self.exit_release: asyncio.Event | None = None
        self.fail_exit = False
        self.image_uploads: list[dict[str, Any]] = []
        self.image_buffers: dict[str, bytearray] = {}
        self.uploaded_images: list[bytes] = []
        self.image_refs: list[str] = []
        self.fail_image_chunk: int | None = None
        self.image_metadata: dict[str, Any] | None = None
        self.image_upload_entered: asyncio.Event | None = None
        self.image_upload_release: asyncio.Event | None = None
        self.session_creations = 0
        self.runtime_overrides: dict[str, Any] = {"session_memory.extend": True}
        self.fail_runtime_options = False
        self.ignore_cron_policy = False
        self.fail_status = False
        self.fail_pending_get = False
        self.malformed_status = False
        self.children: list[dict[str, Any]] = []
        self.indicator: dict[str, Any] = {"running": [], "condensing": {}}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/chunk"):
            params = dict(request.url.params)
            self.calls.append((request.method, path, params))
            self.image_uploads.append(
                {
                    **params,
                    "content": request.content,
                    "host": request.url.host,
                    "mime": request.headers.get("content-type"),
                    "timeout": request.extensions.get("timeout"),
                }
            )
            if self.image_upload_entered is not None and self.image_upload_release is not None:
                self.image_upload_entered.set()
                await self.image_upload_release.wait()
            if self.fail_image_chunk == len(self.image_uploads):
                raise httpx.ReadTimeout("image upload unavailable")
            buffer = self.image_buffers.setdefault(params["upload_id"], bytearray())
            buffer.extend(request.content)
            if params["final"] != "1":
                return httpx.Response(204)
            image = bytes(buffer)
            self.uploaded_images.append(image)
            ref = params["upload_id"]
            self.image_refs.append(ref)
            mime = "image/png" if image.startswith(b"\x89PNG") else "image/gif"
            metadata = {"ref": ref, "media_type": mime, "bytes": len(image)}
            return httpx.Response(
                200,
                json=(self.image_metadata if self.image_metadata is not None else metadata),
            )
        body = json.loads(request.content) if request.content else {}
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
            self.session_creations += 1
            session_id = (
                "opaque-sid"
                if self.session_creations == 1
                else f"opaque-sid-{self.session_creations}"
            )
            if not self.ignore_cron_policy:
                self.runtime_overrides.update(body.get("runtime_options", {}))
            return httpx.Response(201, json={"session_id": session_id, "agent_id": "main"})
        if path.endswith("/status"):
            if self.fail_status:
                return httpx.Response(503, json={"error": "status unavailable"})
            if self.malformed_status:
                return httpx.Response(200, json={"status": "idle"})
            return httpx.Response(
                200,
                json={
                    "agent_id": "main",
                    "is_running": self.running,
                    "children": self.children,
                    "indicator": self.indicator,
                    "status": "running" if self.running else "idle",
                    "model": self.model,
                },
            )
        if path.endswith("/runtime-options"):
            if self.fail_runtime_options:
                return httpx.Response(503, json={"error": "runtime options unavailable"})
            if request.method == "PATCH" and not self.ignore_cron_policy:
                self.runtime_overrides = body["overrides"]
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "overrides": self.runtime_overrides,
                    "options": [
                        {
                            "key": "cron.enabled",
                            "effective": self.runtime_overrides.get("cron.enabled", True),
                        }
                    ],
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
        if request.method == "GET" and "/messages/pending/" in path:
            if self.fail_pending_get:
                return httpx.Response(503, json={"error": "pending lookup unavailable"})
            message_id = path.rsplit("/", 1)[-1]
            if message_id in self.pending:
                return httpx.Response(
                    200,
                    json={"id": message_id, "content": self.pending[message_id]},
                )
            return httpx.Response(
                404,
                json={"detail": "This message has already been received or removed."},
            )
        if request.method == "DELETE" and "/messages/pending/" in path:
            self.pending.pop(path.rsplit("/", 1)[-1], None)
            return httpx.Response(204)
        if path.endswith("/exit"):
            if self.fail_exit:
                return httpx.Response(503, json={"error": "exit unavailable"})
            if self.exit_entered is not None and self.exit_release is not None:
                self.exit_entered.set()
                await self.exit_release.wait()
            # The old finalizer may emit after registry idle, but never after
            # this session's close has awaited its finalizers.
            for stream in self.streams:
                stream.emit("agent_end", {"agent_id": "main", "stop_reason": "aborted"})
            self.running = False
            return httpx.Response(200, json={"status": "exit"})
        if path.endswith("/confirm"):
            return httpx.Response(200, json={"result": "confirmed"})
        if path.endswith("/stream"):
            stream = EventStream()
            self.streams.append(stream)
            # Old authoritative output and an in-flight snapshot are a
            # connection baseline, not fresh assistant output for the new turn.
            stream.emit("post_llm_response", {"agent_id": "main", "content": "old answer"}, 8)
            stream.emit("turn_end", {"agent_id": "main", "turn": 0, "outcome": "completed"}, 9)
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
            self.streams[-1].emit(
                "message_injected",
                {"agent_id": "main", "injected": [{"id": body["message_id"]}]},
            )
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


async def test_automatic_admission_revoked_while_stream_opens_posts_no_message() -> None:
    server = MasoServer()
    provider = transport(
        server,
        session(agent_session_id="original-session", agent_session_id_verified=True),
    )
    revoked = False
    original = server.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal revoked
        response = await original(request)
        if request.url.path.endswith("/stream"):
            revoked = True
        return response

    assert provider._client is not None
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="http://maso.test",
        transport=httpx.MockTransport(handle),
        trust_env=False,
    )

    def admit() -> None:
        if revoked:
            raise RuntimeError("automatic continuation was stopped")

    provider.require_resumed_conversation("original-session", before_delivery=admit)
    try:
        with pytest.raises(RuntimeError, match="continuation was stopped"):
            await provider.send_message("Please continue.", [])
        assert not any(path.endswith("/messages") for _, path, _ in server.calls)
        assert not provider._delivery_path.exists()
        assert not provider._delivery_uncertain and not provider.turn_in_flight
        provider.require_resumed_conversation(None)
        await provider.send_message("Manual follow-up", [])
        assert sum(path.endswith("/messages") for _, path, _ in server.calls) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_remote_images_are_uploaded_in_order_before_atomic_multimodal_message() -> None:
    server = MasoServer()
    provider = MasoHttpSession(
        session(
            env={
                "MASO_BASE_URL": "https://remote-maso.example",
                "MASO_MODEL": "seed-high",
            }
        )
    )
    provider._client = httpx.AsyncClient(
        base_url="https://remote-maso.example",
        transport=httpx.MockTransport(server.handle),
        trust_env=False,
    )
    original = PNG_IMAGE + b"\0" * (1024 * 1024)
    try:
        await provider.prepare_capabilities()
        assert provider.supports_images and provider.capabilities().supports_images
        await provider.send_message("Inspect both images", [original, PNG_IMAGE])
        assert server.uploaded_images == [original, PNG_IMAGE]
        assert [chunk["seq"] for chunk in server.image_uploads] == ["0", "1", "2", "0"]
        assert [chunk["final"] for chunk in server.image_uploads] == [
            "0",
            "0",
            "1",
            "1",
        ]
        assert all(len(chunk["content"]) <= 512 * 1024 for chunk in server.image_uploads)
        assert all(
            chunk["host"] == "remote-maso.example" and chunk["mime"] == "application/octet-stream"
            for chunk in server.image_uploads
        )
        assert all(chunk["timeout"]["read"] == 30 for chunk in server.image_uploads)
        assert len({chunk["upload_id"] for chunk in server.image_uploads}) == 2
        uploads = [
            i for i, (_, path, _) in enumerate(server.calls) if path.endswith("/images/chunk")
        ]
        stream = next(i for i, (_, path, _) in enumerate(server.calls) if path.endswith("/stream"))
        assert max(uploads) < stream
        sent = server.calls[-1][2]
        assert sent["content"][0]["type"] == "text"
        assert sent["content"][0]["text"].endswith("Inspect both images")
        assert sent["content"][1:] == [
            {"type": "image", "image_ref": ref} for ref in server.image_refs
        ]
        assert sent["send_timing"] == "next_turn" and sent["message_id"]
        assert len(json.dumps(sent)) < 32 * 1024
        assert "data:image/" not in json.dumps(sent)
        assert "/feature/worktree" not in json.dumps(sent)
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_image_only_first_turn_defers_guidance_and_next_text_has_no_stale_images() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("", [PNG_IMAGE])
        assert server.calls[-1][2]["content"] == [
            {"type": "image", "image_ref": server.image_refs[0]}
        ]
        assert not provider._hub_runtime_guidance_injected
        provider.acknowledge_turn_complete()
        server.running = False
        await provider.send_message("Next text", [])
        sent = server.calls[-1][2]["content"]
        assert isinstance(sent, str) and sent.endswith("Next text")
        assert "CLAUDE_HUB_TAB_ID=maso-tab" in sent
        assert len(server.uploaded_images) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_image,error",
    [
        (b"BM" + b"x" * 32, "PNG, JPEG, GIF or WebP"),
        (PNG_IMAGE + b"x" * (8 * 1024 * 1024), "8 MiB"),
        (b"not an image", "PNG, JPEG, GIF or WebP"),
    ],
    ids=["unsupported-bmp", "oversized-png", "invalid-image"],
)
async def test_all_images_are_validated_before_any_provider_request(bad_image, error) -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        with pytest.raises(ValueError, match=error):
            await provider.send_message("Invalid second image", [PNG_IMAGE, bad_image])
        assert not server.calls and not provider.turn_in_flight
        assert not provider._staged_images and not provider._delivery_path.exists()
        await provider.send_message("Text after rejection", [])
        assert isinstance(server.calls[-1][2]["content"], str)
        assert not server.image_uploads
    finally:
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["first_chunk", "second_image", "metadata", "wrong_ref"])
async def test_upload_failure_does_not_quarantine_or_deliver_a_partial_turn(
    failure,
) -> None:
    server = MasoServer()
    if failure in {"metadata", "wrong_ref"}:
        server.image_metadata = {
            "ref": "../invalid" if failure == "metadata" else uuid.uuid4().hex,
            "media_type": "image/png",
            "bytes": len(PNG_IMAGE),
        }
    else:
        server.fail_image_chunk = 1 if failure == "first_chunk" else 2
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError, match="image upload metadata|POST request failed"):
            await provider.send_message("Upload must finish", [PNG_IMAGE, PNG_IMAGE])
        assert not any(path.endswith("/messages") for _, path, _ in server.calls)
        assert not provider.turn_in_flight and not provider._delivery_uncertain
        assert not provider._delivery_path.exists() and not provider._staged_images
        server.fail_image_chunk = None
        server.image_metadata = None
        await provider.send_message("Safe retry", [])
        assert isinstance(server.calls[-1][2]["content"], str)
        assert "CLAUDE_HUB_TAB_ID=maso-tab" in server.calls[-1][2]["content"]
        assert len([1 for _, path, _ in server.calls if path == "/api/sessions"]) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_transport_stop_during_upload_cannot_start_a_late_model_turn() -> None:
    server = MasoServer()
    server.image_upload_entered = asyncio.Event()
    server.image_upload_release = asyncio.Event()
    provider = transport(server)
    send = asyncio.create_task(provider.send_message("Cancelled image", [PNG_IMAGE]))
    try:
        await asyncio.wait_for(server.image_upload_entered.wait(), 2)
        await provider.cancel_active_turn()
        server.image_upload_release.set()
        with pytest.raises(RuntimeError, match="cancelled before message delivery"):
            await asyncio.wait_for(send, 2)
        assert not any(path.endswith("/messages") for _, path, _ in server.calls)
        assert not provider._delivery_path.exists()
        await provider.send_message("After Stop", [])
        assert server.calls[-1][2]["content"].endswith("After Stop")
    finally:
        server.image_upload_release.set()
        await provider.stop()
        if not send.done():
            send.cancel()


@pytest.mark.asyncio
async def test_cancelled_upload_releases_guard_and_restores_first_turn_guidance() -> None:
    server = MasoServer()
    server.image_upload_entered = asyncio.Event()
    server.image_upload_release = asyncio.Event()
    provider = transport(server)
    send = asyncio.create_task(provider.send_message("Interrupted upload", [PNG_IMAGE]))
    try:
        await asyncio.wait_for(server.image_upload_entered.wait(), 2)
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send
        assert not provider.turn_in_flight and not provider._delivery_path.exists()
        assert not provider._hub_runtime_guidance_injected and not provider._staged_images
        server.image_upload_release.set()
        await provider.send_message("Safe next turn", [])
        assert "CLAUDE_HUB_TAB_ID=maso-tab" in server.calls[-1][2]["content"]
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
    finally:
        server.image_upload_release.set()
        await provider.stop()


@pytest.mark.asyncio
async def test_maso_tailer_retains_preview_and_sends_original_without_echo_duplication() -> None:
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    record = session()
    server = MasoServer()
    provider = transport(server, record)
    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
    )
    original = PNG_IMAGE + b"\0" * (1024 * 1024)
    try:
        await tailer.send_message("Image preview", [original], "image-turn", previews=[PNG_IMAGE])
        assert server.uploaded_images == [original]
        page = await tailer._store.read_since(-1, 100)
        started = [
            event for event in page.events if event.type == AgentStreamEventType.TURN_STARTED
        ]
        assert len(started) == 1
        metadata = started[0].payload["attachments"]
        assert len(metadata) == 1 and metadata[0]["mime_type"] == "image/png"
        cache = AgentStreamAttachmentStore(record.workspace_id, record.id)
        assert await cache.read(metadata[0]["id"]) == (PNG_IMAGE, "image/png")
        serialized = json.dumps([event.model_dump(mode="json") for event in page.events])
        assert "data:image/" not in serialized and "image_ref" not in serialized
        ctx = NormalizeContext(
            session_id=record.id,
            tab_id=record.tab_id,
            agent_type=AgentType.MASO,
            run_epoch=1,
        )
        assert (
            MasoEventAdapter().normalize_line(
                raw(
                    "message_injected",
                    images=["data:image/png;base64,echo"],
                    content="Image preview",
                ),
                ctx,
            )
            == []
        )
    finally:
        await tailer.stop()


@pytest.mark.parametrize("response_event", ["post_llm_response", "turn_end"])
def test_offsets_snapshots_and_compression_are_not_new_turns(
    response_event: str,
) -> None:
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
        raw(response_event, id="block-1", content="你😀好，完整"),
        raw("context_condensing", estimated_tokens=100),
        raw("context_condensed", summary="do not display or replay this context"),
        raw("agent_end", agent_id="child", stop_reason="completed"),
        raw("turn_start", turn=1),
        raw(response_event, id="block-2", content="之后"),
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
        adapter.normalize_line(raw("hub_tree_settled", stop_reason="completed"), ctx)[0].type
        == AgentStreamEventType.TURN_COMPLETED
    )


@pytest.mark.parametrize("prefix", ["", "Final", "Final answer."])
@pytest.mark.asyncio
async def test_authoritative_response_reaches_history_before_root_completion(
    tmp_path, monkeypatch, prefix: str
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
    try:
        await tailer.send_message("Review the change", [], "review")
        stream = server.streams[-1]
        stream.emit("post_llm_response", {"agent_id": "main", "has_tool_calls": True})
        stream.emit("tool_use", {"agent_id": "main", "tool_call_id": "read", "tool": "Read"})
        stream.emit(
            "tool_result",
            {
                "agent_id": "main",
                "tool_call_id": "read",
                "tool": "Read",
                "output": "source",
            },
        )
        stream.emit("turn_end", {"agent_id": "main", "turn": 1, "outcome": "tool_calls"})
        stream.emit("agent_start", {"agent_id": "child"})
        stream.emit("turn_start", {"agent_id": "child", "turn": 1})
        stream.emit(
            "post_llm_response",
            {"agent_id": "child", "content": "Child result.", "id": "c1"},
        )
        stream.emit("agent_end", {"agent_id": "child", "stop_reason": "completed"})
        stream.emit("turn_start", {"agent_id": "main", "turn": 2})
        if prefix:
            stream.emit(
                "llm_token",
                {"agent_id": "main", "content": prefix, "content_offset": 0},
            )
        response = {
            "agent_id": "main",
            "id": "answer",
            "turn": 0,
            "content": "Final answer.",
            "reasoning_content": "Verified.",
        }
        stream.emit("post_llm_response", response)
        stream.emit("post_llm_response", response)
        stream.emit("turn_end", {"agent_id": "main", "turn": 2, "outcome": "completed"})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        completed = await asyncio.wait_for(queue.get(), 2)
        assert completed.payload["assistant_text"] == "Final answer."
        page = await tailer._store.read_since(-1, 100)
        roots = [event for event in page.events if not event.payload.get("subagent_thread")]
        assert (
            "".join(
                event.payload["text"]
                for event in roots
                if event.type == AgentStreamEventType.TEXT_DELTA
            )
            == "Final answer."
        )
        assert [
            event.payload["text"]
            for event in roots
            if event.type == AgentStreamEventType.THINKING_DELTA
        ] == ["Verified."]
        assert [event for event in roots if event.type == AgentStreamEventType.TURN_COMPLETED] == [
            completed
        ]
        assert roots[-1] == completed
        assert [
            event.payload["text"]
            for event in page.events
            if event.type == AgentStreamEventType.TEXT_DELTA
            and event.payload.get("subagent_thread") == "child"
        ] == ["Child result."]
        assert not any("/history" in path for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


def test_dropped_response_does_not_publish_snapshot_text() -> None:
    ctx = NormalizeContext(session_id="s", tab_id="t", agent_type=AgentType.MASO, run_epoch=1)
    assert (
        MasoEventAdapter().normalize_line(
            raw(
                "post_llm_response",
                dropped=True,
                content="Discarded",
                reasoning_content="Discarded",
            ),
            ctx,
        )
        == []
    )


@pytest.mark.asyncio
async def test_discovery_is_http_only_and_variants_use_provider_keys() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.start()
        await provider.prepare_capabilities()
        caps = provider.capabilities()
        assert caps.structured and caps.supports_goals
        assert caps.goal_execution_owner == "hub_managed"
        assert caps.goal_usage_quality == "unavailable"
        assert provider.goal_completion_requires_provider_event
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
async def test_goal_waits_for_root_agent_end_and_hides_protocol(tmp_path, monkeypatch) -> None:
    from claude_hub.services.agent_stream import tailer as tailer_module

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(tailer_module, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(tailer_module, "GOAL_TERMINAL_GRACE_S", 0.0)
    record = session()
    server = MasoServer()
    provider = transport(server, record)
    observed = []

    async def observer(event) -> None:
        observed.append(event)

    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
        post_persist_observers=[observer],
    )
    queue = await tailer.subscribe_completion()
    response = (
        "Visible progress.\n"
        '<goal-checkpoint>{"verified_progress":[],"decisions":[],"remaining":["next"],'
        '"blocker":null,"next_step":"continue"}</goal-checkpoint>\n'
        '<goal-status state="continue">next</goal-status>'
    )
    try:
        await tailer.send_message(
            "Continue the active Goal.",
            [],
            "goal-turn",
            visible_text="Continue active Goal",
            turn_metadata={"origin": "goal", "protocol": "goal-continuation-v1"},
        )
        stream = server.streams[-1]
        stream.emit(
            "post_llm_response",
            {"agent_id": "main", "id": "answer", "content": response},
        )
        stream.emit("turn_end", {"agent_id": "main", "turn": 1, "outcome": "completed"})
        stream.emit("agent_start", {"agent_id": "child"})
        stream.emit("agent_end", {"agent_id": "child", "stop_reason": "completed"})

        for _ in range(100):
            page = await tailer._store.read_since(-1, 100)
            if any(event.type == AgentStreamEventType.TEXT_DELTA for event in page.events):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("MASO Goal response was not persisted")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), 0.1)
        assert provider.turn_in_flight
        assert tailer._goal_terminal_at is None
        assert not any(
            path.endswith("/control") or path.endswith("/exit") for _, path, _ in server.calls
        )

        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        completed = await asyncio.wait_for(queue.get(), 2)
        await asyncio.sleep(0)
        page = await tailer._store.read_since(-1, 100)
        visible = "".join(
            str(event.payload.get("text") or "")
            for event in page.events
            if event.type == AgentStreamEventType.TEXT_DELTA
            and not event.payload.get("subagent_thread")
        )

        assert completed.type == AgentStreamEventType.TURN_COMPLETED
        assert completed.payload["status"] == "completed"
        assert visible.strip() == "Visible progress."
        assert "goal-status" not in completed.payload["assistant_text"]
        assert observed[-1].payload["_goal_protocol_text"] == response
        assert not any("/auto" in path for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


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
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
        assert server.streams[-1].closed
        assert server.pending and not server.running
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
async def test_rotation_adopts_only_maso_assigned_session_id() -> None:
    server = MasoServer()
    provider = transport(
        server,
        session(agent_session_id="old-provider-session", agent_session_id_verified=True),
    )
    persisted: list[str] = []
    provider._conversation_id_persist = persisted.append
    try:
        result = await provider.rotate_conversation()
        assert result == "opaque-sid"
        assert provider._conversation_id == "old-provider-session"
        assert persisted == []
        provider.adopt_rotated_conversation(result)
        assert provider._conversation_id == "opaque-sid"
        assert provider._conversation_id_verified is True
        assert persisted == ["opaque-sid"]
        assert ("POST", "/api/sessions/old-provider-session/exit", {}) in server.calls
        assert len([call for call in server.calls if call[1] == "/api/sessions"]) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_rejected_maso_rotation_discards_candidate_without_persisting() -> None:
    server = MasoServer()
    provider = transport(
        server,
        session(agent_session_id="old-provider-session", agent_session_id_verified=True),
    )
    persisted: list[str] = []
    provider._conversation_id_persist = persisted.append
    try:
        candidate = await provider.rotate_conversation()
        await provider.discard_rotated_conversation(candidate)
        assert provider._conversation_id == "old-provider-session"
        assert persisted == []
        assert ("POST", "/api/sessions/opaque-sid/exit", {}) in server.calls
        assert provider._pending_rotation is None
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_maso_rotation_discard_survives_repeated_cancellation() -> None:
    server = MasoServer()
    provider = transport(server)
    candidate = await provider.rotate_conversation()
    server.exit_entered = asyncio.Event()
    server.exit_release = asyncio.Event()
    cleanup = asyncio.create_task(provider._discard_rotation(candidate))
    await server.exit_entered.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    server.exit_release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert provider._pending_rotation is None
    assert (
        len(
            [
                path
                for method, path, _ in server.calls
                if method == "POST" and path.endswith("/exit")
            ]
        )
        == 1
    )
    await provider.stop()


@pytest.mark.asyncio
async def test_maso_stop_retries_retained_candidate_after_exit_failure() -> None:
    server = MasoServer()
    provider = transport(server)
    candidate = await provider.rotate_conversation()
    server.fail_exit = True
    with pytest.raises(RuntimeError, match="returned HTTP 503"):
        await provider.stop()
    assert provider._pending_rotation is not None
    assert provider._pending_rotation[0] == candidate
    assert provider._client is not None and not provider._client.is_closed

    server.fail_exit = False
    await provider.stop()
    assert provider._pending_rotation is None
    assert provider._client is not None and provider._client.is_closed


@pytest.mark.asyncio
async def test_maso_next_rotation_cleans_retained_candidate_before_creating_another() -> None:
    server = MasoServer()
    provider = transport(server)
    first = await provider.rotate_conversation()
    server.fail_exit = True
    with pytest.raises(RuntimeError, match="returned HTTP 503"):
        await provider.discard_rotated_conversation(first)
    assert provider._pending_rotation is not None

    server.fail_exit = False
    second = await provider.rotate_conversation()
    assert second == "opaque-sid-2"
    assert provider._pending_rotation is not None
    assert provider._pending_rotation[0] == second
    await provider.discard_rotated_conversation(second)
    await provider.stop()


@pytest.mark.asyncio
async def test_rotation_status_failure_cleans_known_created_session() -> None:
    server = MasoServer()
    server.fail_status = True
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError, match="status returned HTTP 503"):
            await provider.rotate_conversation()
        assert provider._conversation_id is None
        assert provider._pending_rotation is None
        assert ("POST", "/api/sessions/opaque-sid/exit", {}) in server.calls
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_malformed_rotation_status_retains_candidate_when_cleanup_fails() -> None:
    server = MasoServer()
    server.malformed_status = True
    server.fail_exit = True
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError, match="invalid session status"):
            await provider.rotate_conversation()
        assert provider._conversation_id is None
        assert provider._pending_rotation is not None
        assert provider._pending_rotation[0] == "opaque-sid"

        server.malformed_status = False
        server.fail_exit = False
        candidate = await provider.rotate_conversation()
        assert candidate == "opaque-sid-2"
        await provider.discard_rotated_conversation(candidate)
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_ordinary_creation_reuses_posted_id_after_status_failure() -> None:
    server = MasoServer()
    server.fail_status = True
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError, match="status returned HTTP 503"):
            await provider._ensure_session()
        assert provider._conversation_id == "opaque-sid"
        assert provider._conversation_id_verified is True
        assert server.session_creations == 1

        server.fail_status = False
        await provider._ensure_session()
        assert provider._conversation_id == "opaque-sid"
        assert server.session_creations == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_failed_maso_rotation_preserves_old_identity() -> None:
    server = MasoServer()
    server.fail_exit = True
    provider = transport(
        server,
        session(agent_session_id="old-provider-session", agent_session_id_verified=True),
    )
    try:
        with pytest.raises(RuntimeError, match="MASO POST .*exit returned HTTP 503"):
            await provider.rotate_conversation()
        assert provider._conversation_id == "old-provider-session"
        assert provider._conversation_id_verified is True
        assert not any(path == "/api/sessions" for _, path, _ in server.calls)
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
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
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
        stopping = asyncio.create_task(tailer.cancel_turn("stopped"))
        await asyncio.wait_for(server.abort_entered.wait(), 2)
        assert not stopping.done()  # MASO Stop confirms teardown before returning.
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
        assert await asyncio.wait_for(stopping, 2)
        page = await tailer._store.read_since(0, 100)
        assert all(e.turn_id == "stopped" for e in page.events)
        assert not any(e.type == AgentStreamEventType.TEXT_DELTA for e in page.events)
        assert len([e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]) == 1
    finally:
        server.abort_release.set()
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


@pytest.mark.asyncio
async def test_next_turn_waits_for_cancel_finalizers_after_registry_is_idle() -> None:
    server = MasoServer()
    server.exit_entered = asyncio.Event()
    server.exit_release = asyncio.Event()
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
    second = None
    try:
        await tailer.send_message("first", [], "first")
        stopping = asyncio.create_task(tailer.cancel_turn("first"))
        await asyncio.wait_for(server.exit_entered.wait(), 2)
        assert not stopping.done()
        assert not server.running  # idle is observable before old agent_end
        second = asyncio.create_task(tailer.send_message("second", [], "second"))
        await asyncio.sleep(0)
        assert not second.done()
        assert len(server.streams) == 1
        server.exit_release.set()
        assert await asyncio.wait_for(stopping, 2)
        await asyncio.wait_for(second, 2)
        assert len(server.streams) == 2
        await asyncio.sleep(0)
        assert provider.turn_in_flight and server.running
        server.streams[-1].emit("turn_end", {"agent_id": "main", "content": "second answer"})
        server.running = False
        server.streams[-1].emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        while True:
            event = await asyncio.wait_for(queue.get(), 2)
            if event.type == AgentStreamEventType.TURN_COMPLETED and event.turn_id == "second":
                assert event.payload["status"] == "completed"
                break
        page = await tailer._store.read_since(0, 100)
        assert len([e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]) == 2
    finally:
        server.exit_release.set()
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


@pytest.mark.asyncio
async def test_failed_exit_blocks_new_prompt_until_finalizer_fence_succeeds() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("first", [])
        server.fail_exit = True
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await provider.cancel_active_turn()
        assert not server.running and provider._needs_abort and provider._delivery_path.exists()
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await provider.send_message("blocked", [])
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        server.fail_exit = False
        await provider.send_message("second", [])
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 2
    finally:
        server.fail_exit = False
        await provider.stop()


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
async def test_stream_disconnect_quarantines_without_aborting_or_retrying_prompt() -> None:
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
        assert server.running
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        provider.acknowledge_turn_complete()
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("do not silently interrupt children", [])
        assert len(server.streams) == 1
    finally:
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["root-stream", "child-stream", "status-monitor"])
async def test_transport_uncertainty_preserves_goal_for_explicit_recovery(
    tmp_path, monkeypatch, failure: str
) -> None:
    from unittest.mock import AsyncMock

    from claude_hub.api import agent_stream as api
    from claude_hub.models import GoalDispatchState, GoalRunCreate, GoalRunStatus
    from claude_hub.services import goal_run
    from claude_hub.services.goal_run import GoalRunController, GoalRunStore

    tab_id = f"goal-{uuid.uuid4().hex}"
    record = session(id=f"terminal-tab-{tab_id}", tab_id=tab_id)
    server = MasoServer()
    provider = transport(server, record)
    observed = asyncio.Event()

    async def observer(event) -> None:
        await api._notify_goal_turn_completed(event)
        if event.type == AgentStreamEventType.TURN_COMPLETED:
            observed.set()

    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
        post_persist_observers=[observer],
    )

    async def dispatch(goal, prompt):
        await tailer.send_message(
            prompt,
            [],
            goal.pending_step_id,
            turn_metadata={"origin": "goal", "protocol": "goal-continuation-v1"},
        )
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: manager)
    monkeypatch.setattr(api.workspace_manager, "on_scheduled_chat_turn_completed", AsyncMock())
    completion = await tailer.subscribe_completion()
    try:
        goal = await manager.create_and_start(
            record.tab_id, GoalRunCreate(objective="Verify the feature", client_request_id="start")
        )
        stream = server.streams[-1]
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Recovered response."})
        for child in ("child-one", "child-two", "child-three"):
            stream.emit("agent_start", {"agent_id": child})
        if failure != "root-stream":
            server.running = False
            stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        stream.emit(
            "tool_result",
            {"agent_id": "child-one", "tool_call_id": "check", "tool": "Read", "output": "ok"},
        )
        if failure == "status-monitor":
            server.fail_status = True
        else:
            stream.queue.put_nowait(None)
        async with asyncio.timeout(3):
            while True:
                terminal = await completion.get()
                if terminal.type == AgentStreamEventType.TURN_COMPLETED:
                    break
        await asyncio.wait_for(observed.wait(), 3)
        assert terminal.payload["status"] == "failed"
        assert terminal.payload["delivery_uncertain"] is True
        assert terminal.payload["quarantined_turn_id"] == goal.current_turn_id
        parked = manager.get(goal.id)
        assert parked.status == GoalRunStatus.PAUSED
        assert parked.dispatch_state == GoalDispatchState.UNCERTAIN
        assert parked.current_turn_id == goal.current_turn_id
        assert parked.completed_at is None
        assert parked.turns_completed == 1
        # Losing observation cannot authorize resending a prompt or stopping
        # remote children. The same quarantine survives the Goal transition.
        assert provider._delivery_path.exists()
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
        page = await tailer._store.read_since(-1, 100)
        errors = [e for e in page.events if e.type == AgentStreamEventType.ERROR]
        terminals = [e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]
        assert len(errors) == len(terminals) == 1
        assert errors[0].stream_sequence + 1 == terminals[0].stream_sequence
    finally:
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_chat_only_and_unsupported_edit_are_rejected_before_mutation() -> None:
    with pytest.raises(ValidationError, match="HTTP Chat"):
        TerminalTabCreate(name="bad", agent_type=AgentType.MASO)
    assert TerminalTabCreate(name="good", agent_type=AgentType.MASO, session_kind=SessionKind.CHAT)
    process = TTYDProcess("id", 12345, "unchanged", agent_type=AgentType.TERMINAL)
    manager = TTYDManager.__new__(TTYDManager)
    manager._maso_archive_source = None
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
async def test_maso_cwd_and_plan_rejected_before_remote_execution() -> None:
    with pytest.raises(ValidationError, match="default mode"):
        TerminalTabCreate(
            name="bad",
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            chat_mode=ChatMode.PLAN,
        )
    process = TTYDProcess(
        "id",
        12345,
        "unchanged",
        cwd="/checkout/a",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
    )
    manager = TTYDManager.__new__(TTYDManager)
    manager._maso_archive_source = None
    manager.processes = {"id": process}
    manager._start_locks = {}
    with pytest.raises(ValueError, match="working directory"):
        await manager.update_tab("id", name="changed", cwd="/checkout/b")
    assert process.name == "unchanged" and process.cwd == "/checkout/a"
    provider = transport(MasoServer(), session(chat_mode=ChatMode.PLAN))
    try:
        with pytest.raises(ValueError, match="default mode"):
            await provider.send_message("must not run", [])
        assert provider._client is not None
        assert not provider.turn_in_flight
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_maso_creation_pins_endpoint_across_backend_default_change(
    tmp_path, monkeypatch
) -> None:
    import importlib

    module = importlib.import_module("claude_hub.services.ttyd_manager")

    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(TTYDManager, "_load_order", lambda _self: None)
    monkeypatch.setattr(TTYDManager, "_ensure_tab_in_order", lambda *_args: None)

    async def inert_start(_self):
        return False

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", inert_start)
    monkeypatch.setattr(TTYDProcess, "start", inert_start)

    async def available_service(_env):
        return [], "seed"

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.maso_http.check_maso_service",
        available_service,
    )
    monkeypatch.setenv("MASO_BASE_URL", "http://maso-a.test:9796")
    manager = TTYDManager()
    tab = await manager.create_tab(
        name="pinned",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
    )
    assert tab.env["MASO_BASE_URL"] == "http://maso-a.test:9796"
    manager.processes[tab.id].agent_session_id = "opaque-sid"
    manager.processes[tab.id].agent_session_id_verified = True
    manager._save_state()
    monkeypatch.setenv("MASO_BASE_URL", "http://maso-b.test:9796")
    restored = TTYDManager().processes[tab.id]
    await restored.switch_env({"MASO_MODEL": "seed-high", "MASO_BASE_URL": ""})
    assert restored.env == {
        "MASO_BASE_URL": "http://maso-a.test:9796",
        "MASO_MODEL": "seed-high",
    }
    provider = transport(
        MasoServer(),
        session(
            env=restored.env,
            agent_session_id="opaque-sid",
            agent_session_id_verified=True,
        ),
    )
    try:
        assert provider._base == "http://maso-a.test:9796"
        provider.update_env({"MASO_MODEL": "seed", "MASO_BASE_URL": ""})
        assert provider.session.env["MASO_BASE_URL"] == "http://maso-a.test:9796"
    finally:
        await provider.stop()
    with pytest.raises(ValueError, match="endpoint is unverified"):
        transport(
            MasoServer(),
            session(
                env={},
                agent_session_id="opaque-sid",
                agent_session_id_verified=True,
            ),
        )

    # A legacy unsupported mode is still serializable for GET/list. Creation
    # and execution reject it without silently changing its persisted mode.
    manager.processes[tab.id].chat_mode = ChatMode.PLAN
    manager._save_state()
    legacy = TTYDManager()
    assert legacy.list_tabs()[0].chat_mode == ChatMode.PLAN
    with pytest.raises(ValueError, match="default mode"):
        await legacy.create_tab(
            name="bad plan",
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            chat_mode=ChatMode.PLAN,
        )


@pytest.mark.asyncio
async def test_stop_retires_confirmation_card_and_refuses_stale_answer() -> None:
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
    try:
        await tailer.send_message("work", [], "owned-turn")
        server.streams[-1].emit(
            "confirmation_required",
            {
                "agent_id": "main",
                "tool_call_id": "call-stale",
                "tool": "bash",
                "args": {},
            },
        )
        while (
            await asyncio.wait_for(queue.get(), 2)
        ).type != AgentStreamEventType.APPROVAL_REQUIRED:
            pass
        assert await tailer.cancel_turn("owned-turn")
        await tailer._await_turn_teardown()
        answer = json.dumps(
            {
                "type": "ask_question_response",
                "answers": [
                    {
                        "questionId": "maso-confirm:call-stale",
                        "selected": ["Approve"],
                    }
                ],
            }
        )
        with pytest.raises(ValueError, match="no longer pending"):
            await tailer.send_message(answer, [], "stale-answer")
        page = await tailer._store.read_since(0, 100)
        resolved = [e for e in page.events if e.type == AgentStreamEventType.APPROVAL_RESOLVED]
        assert len(resolved) == 1 and resolved[0].turn_id == "owned-turn"
        assert len([1 for _, path, _ in server.calls if path.endswith("/messages")]) == 1
        assert not any(path.endswith("/confirm") for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe_completion(queue)
        await tailer.stop()


@pytest.mark.asyncio
async def test_cold_orphan_recovery_retires_only_unresolved_confirmation_cards() -> None:
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
    ctx = NormalizeContext(
        session_id=record.id,
        tab_id=record.tab_id,
        agent_type=AgentType.MASO,
        run_epoch=7,
        turn_id="crashed-turn",
    )
    await tailer._store.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"text": "old"}))
    adapter = MasoEventAdapter()
    for call_id in ("answered", "pending"):
        card = adapter.normalize_line(
            raw(
                "confirmation_required",
                tool_call_id=call_id,
                tool="bash",
                args={},
            ),
            ctx,
        )[0]
        await tailer._store.append(card)
    await tailer._store.append(
        ctx.event(
            AgentStreamEventType.APPROVAL_RESOLVED,
            {"tool_call_id": "answered"},
            call_id="answered",
        )
    )
    try:
        assert not tailer._pending_approvals
        await tailer.start()
        page = await tailer._store.read_since(0, 100)
        resolutions = [e for e in page.events if e.type == AgentStreamEventType.APPROVAL_RESOLVED]
        assert [e.call_id for e in resolutions] == ["answered", "pending"]
        assert all(e.turn_id == "crashed-turn" and e.run_epoch == 7 for e in resolutions)
        completion = next(e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED)
        assert completion.payload["status"] == "cancelled"
        assert resolutions[-1].stream_sequence < completion.stream_sequence
        assert not server.calls and not tailer._pending_approvals
        await tailer.start()
        assert (await tailer._store.read_since(0, 100)).events == page.events
    finally:
        await tailer.stop()


def test_cancelled_adapter_epochs_do_not_accumulate() -> None:
    adapter = MasoEventAdapter()
    for epoch in range(1, 5):
        ctx = NormalizeContext(
            session_id="s",
            tab_id="t",
            agent_type=AgentType.MASO,
            run_epoch=epoch,
            turn_id=f"cancelled-{epoch}",
        )
        adapter.normalize_line(raw("llm_token", content="x" * 4096, content_offset=0), ctx)
        assert len(adapter._messages) == 1
        assert next(iter(adapter._messages))[1] == epoch
    adapter.normalize_line(raw("hub_tree_settled", stop_reason="completed"), ctx)
    assert not adapter._messages


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
async def test_restart_with_pending_delivery_preserves_native_work_and_queued_input() -> None:
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
        assert server.pending == {message_id: "old unconsumed prompt"}
        assert not any(path == "/api/sessions" for _, path, _ in server.calls)
        assert not any(
            method == "DELETE" or path.endswith(("/control", "/exit"))
            for method, path, _ in server.calls
        )
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
        # The service handler resumes after the request timeout.
        assert server.delayed_message is not None
        server.pending[server.delayed_message["message_id"]] = "delayed prompt"
        await provider.stop()
        resumed = transport(
            server,
            record.model_copy(
                update={
                    "agent_session_id": "opaque-sid",
                    "agent_session_id_verified": True,
                }
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
        assert server.running
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("never automatically retry", [])
    finally:
        server.send_release.set()
        await asyncio.gather(send, return_exceptions=True)
        await provider.stop()


@pytest.mark.asyncio
async def test_new_and_rotated_sessions_disable_native_cron() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("first", [])
        await provider.cancel_active_turn()
        candidate = await provider.rotate_conversation()
        provider.adopt_rotated_conversation(candidate)
        await provider.send_message("after rotation", [])
        creations = [
            body
            for method, path, body in server.calls
            if method == "POST" and path == "/api/sessions"
        ]
        assert len(creations) == 2
        assert all(body["runtime_options"] == {"cron.enabled": False} for body in creations)
        assert ("POST", "/api/sessions/opaque-sid/exit", {}) in server.calls
        assert any(path == f"/api/sessions/{candidate}/messages" for _, path, _ in server.calls)
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_resume_disables_native_cron_before_delivery_and_preserves_other_options() -> None:
    server = MasoServer()
    provider = transport(
        server, session(agent_session_id="existing", agent_session_id_verified=True)
    )
    try:
        await provider.send_message("scheduled work belongs to Hub", [])
        patch = (
            "PATCH",
            "/api/sessions/existing/runtime-options",
            {
                "overrides": {"session_memory.extend": True, "cron.enabled": False},
            },
        )
        assert patch in server.calls
        message_index = next(
            i for i, call in enumerate(server.calls) if call[1].endswith("/messages")
        )
        assert server.calls.index(patch) < message_index
        assert server.session_creations == 0
        assert server.runtime_overrides == {
            "session_memory.extend": True,
            "cron.enabled": False,
        }
    finally:
        await provider.stop()


@pytest.mark.parametrize(
    "failure", ["unsupported", "ignored"], ids=["no-api", "policy-not-applied"]
)
@pytest.mark.asyncio
async def test_native_cron_policy_failure_is_fail_closed_and_retryable(
    failure: str,
) -> None:
    server = MasoServer()
    server.fail_runtime_options = failure == "unsupported"
    server.ignore_cron_policy = failure == "ignored"
    provider = transport(server)
    try:
        with pytest.raises(RuntimeError):
            await provider.send_message("do not deliver without policy", [])
        assert not any(path.endswith("/messages") for _, path, _ in server.calls)
        assert not provider.turn_in_flight
        server.fail_runtime_options = server.ignore_cron_policy = False
        await provider.send_message("retry safely", [])
        assert server.session_creations == 1
        assert server.runtime_overrides["cron.enabled"] is False
    finally:
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_reason", ["completed", "error", "aborted"])
async def test_native_children_keep_one_hub_turn_through_multiple_root_wakeups(
    child_reason,
) -> None:
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
    live = await tailer.subscribe()
    completion = await tailer.subscribe_completion()

    async def read_live_until(kind: AgentStreamEventType, text: str = "") -> list:
        received = []
        async with asyncio.timeout(3):
            while True:
                event = await live.get()
                received.append(event)
                if event.type == kind and (not text or event.payload.get("text") == text):
                    return received

    try:
        await tailer.send_message("Delegate and summarize the reports", [], "delegation")
        stream = server.streams[-1]
        stream.emit("agent_start", {"agent_id": "child-one"})
        stream.emit("agent_start", {"agent_id": "child-two"})
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Waiting for reports."})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        await read_live_until(AgentStreamEventType.TEXT_DELTA, "Waiting for reports.")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(completion.get(), 0.1)
        assert provider.turn_in_flight and not stream.closed

        for index, child in enumerate(("child-one", "child-two"), 1):
            # Each child SendMessage can wake the root before the child ends.
            server.running = True
            stream.emit("agent_start", {"agent_id": "main"})
            stream.emit("turn_start", {"agent_id": "main", "turn": 0})
            if child_reason == "error":
                stream.emit("agent_error", {"agent_id": child, "error": "request failed"})
            stream.emit("agent_end", {"agent_id": child, "stop_reason": child_reason})
            stream.emit(
                "tool_use",
                {"agent_id": "main", "tool_call_id": f"read-{index}", "tool": "Read"},
            )
            stream.emit(
                "tool_result",
                {
                    "agent_id": "main",
                    "tool_call_id": f"read-{index}",
                    "tool": "Read",
                    "output": "ok",
                },
            )
            stream.emit("post_llm_response", {"agent_id": "main", "content": f"Report {index}."})
            await read_live_until(AgentStreamEventType.TEXT_DELTA, f"Report {index}.")
            page = await tailer._store.read_since(-1, 100)
            assert any(e.payload.get("text") == f"Report {index}." for e in page.events)
            server.running = False
            stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
            if index == 1:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(completion.get(), 0.1)
                assert provider.turn_in_flight and not stream.closed

        terminal = await asyncio.wait_for(completion.get(), 3)
        assert terminal.type == AgentStreamEventType.TURN_COMPLETED
        assert terminal.turn_id == "delegation"
        assert terminal.payload["status"] == "completed"
        await read_live_until(AgentStreamEventType.TURN_COMPLETED)
        page = await tailer._store.read_since(-1, 100)
        assert len([e for e in page.events if e.type == AgentStreamEventType.TURN_STARTED]) == 1
        assert len([e for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]) == 1
        texts = [e for e in page.events if e.type == AgentStreamEventType.TEXT_DELTA]
        assert [e.payload["text"] for e in texts] == [
            "Waiting for reports.",
            "Report 1.",
            "Report 2.",
        ]
        assert len({e.message_id for e in texts}) == 3
        assert all(e.turn_id == "delegation" for e in page.events)
        assert (
            len([e for e in page.events if e.type == AgentStreamEventType.TOOL_CALL_STARTED]) == 2
        )
        assert not provider.turn_in_flight and not provider._delivery_path.exists()
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe(live)
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_late_root_wakeup_after_quiescence_is_live_persisted_and_headless_safe(
    monkeypatch,
) -> None:
    from claude_hub.services.agent_stream import tailer as tailer_module

    monkeypatch.setattr(tailer_module, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(tailer_module, "IDLE_TTL_S", 0)
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
    completion = await tailer.subscribe_completion()
    live = await tailer.subscribe()
    try:
        await tailer.send_message("Start", [], "initial")
        stream = server.streams[-1]
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Initial."})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        assert (await asyncio.wait_for(completion.get(), 2)).turn_id == "initial"
        tailer.unsubscribe_completion(completion)
        tailer.unsubscribe(live)
        await asyncio.sleep(0.05)
        assert tailer.is_running() and not stream.closed

        # /status has no pending-RPC fence. Keep the SSE subscription even if
        # the scheduler starts this root beyond the quiescence window.
        server.running = True
        stream.emit("agent_start", {"agent_id": "main"})
        stream.emit("turn_start", {"agent_id": "main", "turn": 0})
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Late report."})
        for _ in range(100):
            page = await tailer._store.read_since(-1, 100)
            if any(e.payload.get("text") == "Late report." for e in page.events):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Headless root wake-up was not persisted")
        assert provider.turn_in_flight
        starts = [e for e in page.events if e.type == AgentStreamEventType.TURN_STARTED]
        assert len(starts) == 2 and starts[-1].turn_id != "initial"
        assert starts[-1].run_epoch == 2
        wake_id = starts[-1].turn_id
        assert all(
            e.turn_id == wake_id for e in page.events if e.payload.get("text") == "Late report."
        )

        # Reattach while still running: no message POST or provider reconnect.
        completion = await tailer.subscribe_completion()
        live = await tailer.subscribe()
        stream.emit(
            "tool_use",
            {"agent_id": "main", "tool_call_id": "wake-tool", "tool": "Read"},
        )
        stream.emit(
            "tool_result",
            {
                "agent_id": "main",
                "tool_call_id": "wake-tool",
                "tool": "Read",
                "output": "ok",
            },
        )
        stream.emit("turn_start", {"agent_id": "main", "turn": 1})
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Wake finished."})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        terminal = await asyncio.wait_for(completion.get(), 2)
        assert terminal.turn_id == wake_id
        assert terminal.payload["final_text"] == "Wake finished."
        received = []
        while not live.empty():
            received.append(live.get_nowait())
        assert any(e.payload.get("text") == "Wake finished." for e in received)
        assert any(e.type == AgentStreamEventType.TOOL_CALL_STARTED for e in received)
        assert received[-1].type == AgentStreamEventType.TURN_COMPLETED
        assert len(server.streams) == 1
        assert sum(path.endswith("/messages") for _, path, _ in server.calls) == 1
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe(live)
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity",
    ["nested-running", "nested-waiting", "paused", "condensing", "root-finalizer"],
)
async def test_root_end_waits_for_authoritative_tree_activity(activity: str) -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("Work", [])
        stream = server.streams[-1]
        server.running = activity == "root-finalizer"
        if activity.startswith("nested"):
            server.children = [
                {
                    "status": "idle",
                    "children": [{"status": activity.split("-")[1], "children": []}],
                }
            ]
        elif activity == "paused":
            server.indicator["running"] = ["paused-child"]
        elif activity == "condensing":
            server.indicator["condensing"] = {"main": "Condenser"}
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None and event["type"] != "hub_tree_settled"
            if event["type"] == "hub_tree_waiting":
                break
        server.running = False
        server.children = []
        server.indicator = {"running": [], "condensing": {}}
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None
            if event["type"] == "hub_tree_settled":
                break
        provider.acknowledge_turn_complete()
        assert not stream.closed
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_stop_during_idle_child_wait_fences_automatic_root_wake() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("Delegate", [])
        stream = server.streams[-1]
        stream.emit("agent_start", {"agent_id": "child"})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None
            if event["type"] == "hub_tree_waiting":
                break
        generation = provider._stdout_generation
        await provider.cancel_active_turn()
        assert stream.closed and not provider.turn_in_flight
        assert not provider.accepts_notification({**raw("agent_start"), "generation": generation})
        assert any(path.endswith("/control") for _, path, _ in server.calls)
        assert any(path.endswith("/exit") for _, path, _ in server.calls)
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_status_monitor_failure_cannot_hang_or_abort_children() -> None:
    server = MasoServer()
    provider = transport(server)
    try:
        await provider.send_message("Delegate", [])
        stream = server.streams[-1]
        stream.emit("agent_start", {"agent_id": "child"})
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        server.fail_status = True
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None
            if event["type"] == "hub_transport_error":
                break
        assert stream.closed and server.running
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
        provider.acknowledge_turn_complete()
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("No destructive retry", [])
    finally:
        server.fail_status = False
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["preparation", "post"])
async def test_native_wake_cannot_retire_a_new_user_submission(
    blocked_phase: str,
) -> None:
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
    completion = await tailer.subscribe_completion()
    entered, release = asyncio.Event(), asyncio.Event()
    send = None

    async def before_send() -> None:
        entered.set()
        await release.wait()

    try:
        await tailer.send_message("Initial", [], "initial")
        stream = server.streams[-1]
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        await asyncio.wait_for(completion.get(), 2)
        if blocked_phase == "post":
            server.send_entered, server.send_release = entered, release
        send = asyncio.create_task(
            tailer.send_message(
                "User next",
                [],
                "next",
                before_send=before_send if blocked_phase == "preparation" else None,
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        stream.emit("agent_start", {"agent_id": "main"})
        stream.emit("turn_start", {"agent_id": "main", "turn": 0})
        stream.emit(
            "post_llm_response",
            {"agent_id": "main", "content": "Native report, not user response"},
        )
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        await asyncio.sleep(0.8)
        release.set()
        await asyncio.wait_for(send, 2)
        await asyncio.sleep(0.1)
        assert completion.empty(), "Unrelated native end completed the newly submitted user prompt"
        assert tailer._active_turn_id == "next" and provider.turn_in_flight
        stream.emit("post_llm_response", {"agent_id": "main", "content": "Actual user answer."})
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        terminal = await asyncio.wait_for(completion.get(), 2)
        assert terminal.turn_id == "next" and terminal.payload["status"] == "completed"
        assert "Actual user answer." in terminal.payload["assistant_text"]
        page = await tailer._store.read_since(-1, 100)
        starts = [e.turn_id for e in page.events if e.type == AgentStreamEventType.TURN_STARTED]
        ends = [e.turn_id for e in page.events if e.type == AgentStreamEventType.TURN_COMPLETED]
        assert starts == ends == ["initial", "next"]
        assert not any(e.turn_id is None for e in page.events)
    finally:
        release.set()
        if send is not None:
            await asyncio.gather(send, return_exceptions=True)
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_late_child_only_wake_has_owned_stoppable_turn(stop: bool) -> None:
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
    completion = await tailer.subscribe_completion()
    try:
        await tailer.send_message("Initial", [], "initial")
        stream = server.streams[-1]
        server.running = False
        stream.emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        await asyncio.wait_for(completion.get(), 2)
        stream.emit("agent_start", {"agent_id": "child"})
        stream.emit("turn_start", {"agent_id": "child", "turn": 0})
        stream.emit(
            "tool_use",
            {"agent_id": "child", "tool_call_id": "child-task", "tool": "Bash"},
        )
        stream.emit("post_llm_response", {"agent_id": "child", "content": "Child woke."})
        for _ in range(100):
            page = await tailer._store.read_since(-1, 100)
            if any(e.payload.get("text") == "Child woke." for e in page.events):
                break
            await asyncio.sleep(0.01)
        assert provider.turn_in_flight and tailer._active_turn_id
        wake_id = tailer._active_turn_id
        assert all(e.turn_id == wake_id for e in page.events if e.payload.get("subagent_thread"))
        if stop:
            assert await tailer.cancel_turn(wake_id)
            await tailer._await_turn_teardown()
        else:
            stream.emit(
                "tool_result",
                {
                    "agent_id": "child",
                    "tool_call_id": "child-task",
                    "tool": "Bash",
                    "output": "done",
                },
            )
            stream.emit("agent_end", {"agent_id": "child", "stop_reason": "completed"})
        terminal = await asyncio.wait_for(completion.get(), 2)
        assert terminal.turn_id == wake_id
        assert terminal.payload["status"] == ("cancelled" if stop else "completed")
        assert not provider.turn_in_flight
    finally:
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_input_retained_before_agent_start_can_finish_failed_turn() -> None:
    server = MasoServer()
    record = session()
    original = server.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content)
            stream = server.streams[-1]
            stream.emit(
                "message_injected",
                {"agent_id": "main", "injected": [{"id": body["message_id"]}]},
            )
            stream.emit("agent_error", {"agent_id": "main", "error": "before_input failed"})
            stream.emit("agent_end", {"agent_id": "main", "stop_reason": "error"})
            return httpx.Response(202, json={"status": "queued"})
        return await original(request)

    provider = MasoHttpSession(record)
    provider._client = httpx.AsyncClient(
        base_url="http://maso.test", transport=httpx.MockTransport(handle)
    )
    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=provider,
    )
    completion = await tailer.subscribe_completion()
    try:
        await tailer.send_message("Fails before agent_start", [], "input-error")
        async with asyncio.timeout(2):
            while True:
                event = await completion.get()
                if event.type == AgentStreamEventType.TURN_COMPLETED:
                    break
        assert event.turn_id == "input-error" and event.payload["status"] == "failed"
        assert not provider.turn_in_flight and provider._awaiting_message_id is None
        assert not any(path.endswith(("/control", "/exit")) for _, path, _ in server.calls)
    finally:
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_uncertain_delivery_reconciles_only_when_remote_idle_and_pending_absent() -> None:
    server = MasoServer()
    record = session()
    provider = transport(server, record)
    try:
        await provider.send_message("first prompt", [])
        first_message_id = provider._message_id
        assert first_message_id is not None
        server.streams[-1].queue.put_nowait(None)
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None
            if event["type"] == "hub_transport_error":
                break
        provider.acknowledge_turn_complete()
        assert provider._delivery_uncertain and provider._delivery_path.exists()

        # Remote still running: must stay fail-closed.
        assert server.running
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("blocked while running", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()

        # Remote idle, but status endpoint returns 503: must stay fail-closed.
        server.running = False
        server.fail_status = True
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("blocked when status 503", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        server.fail_status = False

        # Remote idle, but pending lookup returns 503: must stay fail-closed.
        server.fail_pending_get = True
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("blocked when pending 503", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        server.fail_pending_get = False

        # Remote idle, but message is still in pending broker queue (200): must stay fail-closed.
        server.pending[first_message_id] = "first prompt"
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("blocked while pending queued", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()

        # Remote child still active: must stay fail-closed even when pending is absent (404).
        server.pending.clear()
        server.children = [{"status": "running", "children": []}]
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("blocked while child active", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        server.children = []

        # Remote confirmed idle and pending/{message_id} returns 404: reconcile and send new prompt.
        await provider.send_message("second prompt", [])
        assert not provider._delivery_uncertain
        assert provider._last_error is None
        posted = [body for method, path, body in server.calls if path.endswith("/messages")]
        assert len(posted) == 2
        assert posted[1]["content"] == "second prompt"
        assert posted[1]["message_id"] != first_message_id
        assert (
            "GET",
            f"/api/sessions/opaque-sid/messages/pending/{first_message_id}",
            {},
        ) in server.calls
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_stop_clears_uncertain_delivery_when_turn_not_in_flight() -> None:
    server = MasoServer()
    server.fail_send = True
    record = session()
    provider = transport(server, record)
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await provider.send_message("ambiguous prompt", [])
        quarantined_id = provider._message_id
        assert quarantined_id is not None
        assert not provider.turn_in_flight
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        assert quarantined_id in server.pending

        # If abort fails during Stop, quarantine remains fail-closed.
        server.fail_abort = True
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await provider.cancel_active_turn()
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        assert provider._needs_abort
        server.fail_abort = False

        # Explicit Stop with turn_in_flight=False deletes pending, aborts, exits, confirms idle, and clears sidecar.
        await provider.cancel_active_turn()
        assert not provider._delivery_uncertain
        assert not provider._needs_abort
        assert not provider._delivery_path.exists()
        assert quarantined_id not in server.pending
        assert (
            "DELETE",
            f"/api/sessions/opaque-sid/messages/pending/{quarantined_id}",
            {},
        ) in server.calls
        assert (
            "POST",
            "/api/sessions/opaque-sid/control",
            {"action": "abort"},
        ) in server.calls
        assert ("POST", "/api/sessions/opaque-sid/exit", {}) in server.calls
        assert provider._conversation_id == "opaque-sid-2"

        server.fail_send = False
        await provider.send_message("fresh prompt after stop", [])
        posted = [body for _, path, body in server.calls if path.endswith("/messages")]
        assert len(posted) == 2
        assert posted[-1]["content"].endswith("fresh prompt after stop")
        assert (
            "POST",
            "/api/sessions/opaque-sid-2/messages",
            posted[-1],
        ) in server.calls
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_unacknowledged_post_quarantine_blocks_auto_reconcile_and_stop_fences_delayed_enqueue() -> (
    None
):
    server = MasoServer()
    server.fail_send = True
    server.delayed_enqueue = True
    record = session()
    persisted_ids: list[str] = []
    provider = MasoHttpSession(record, conversation_id_persist=persisted_ids.append)
    provider._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:9796", transport=httpx.MockTransport(server.handle)
    )
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await provider.send_message("delayed prompt", [])
        assert provider._delivery_uncertain and not provider._post_acknowledged
        assert not server.running and not server.pending

        # Auto-reconciliation must NOT clear an unacknowledged POST even when
        # remote status is idle and pending returns 404, because the delayed
        # handler can still enqueue onto opaque-sid later.
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("must stay blocked before stop", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()

        # Explicit Stop rotates to a fresh MASO session so any late enqueue on
        # opaque-sid cannot be woken by subsequent prompts.
        await provider.cancel_active_turn()
        assert not provider._delivery_uncertain and not provider._delivery_path.exists()
        assert provider._conversation_id == "opaque-sid-2"
        assert persisted_ids[-1] == "opaque-sid-2"

        # Now the delayed handler finally enqueues onto the old session.
        assert server.delayed_message is not None
        server.pending[server.delayed_message["message_id"]] = "delayed prompt"

        server.fail_send = False
        server.delayed_enqueue = False
        await provider.send_message("clean prompt on rotated session", [])
        message_Calls = [
            (path, body) for _, path, body in server.calls if path.endswith("/messages")
        ]
        assert message_Calls[-1][0] == "/api/sessions/opaque-sid-2/messages"
        assert message_Calls[-1][1]["content"].endswith("clean prompt on rotated session")
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_reconcile_uncertain_delivery_fences_pending_consumed_between_status_and_pending_get() -> (
    None
):
    server = MasoServer()
    record = session()
    provider = transport(server, record)
    try:
        await provider.send_message("first prompt", [])
        first_message_id = provider._message_id
        assert first_message_id is not None
        server.streams[-1].queue.put_nowait(None)
        while True:
            event = await asyncio.wait_for(provider.read_line(), 2)
            assert event is not None
            if event["type"] == "hub_transport_error":
                break
        provider.acknowledge_turn_complete()
        assert provider._delivery_uncertain and provider._post_acknowledged

        # Simulate the race where the first GET /status sees idle while the
        # message is still queued in pending, and MASO dequeues + starts running
        # just before GET /messages/pending/{id} returns 404.
        server.running = False
        server.pending[first_message_id] = "first prompt"
        original_handle = server.handle

        async def racing_handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/messages/pending/" in request.url.path:
                server.pending.pop(first_message_id, None)
                server.running = True
            return await original_handle(request)

        provider._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:9796",
            transport=httpx.MockTransport(racing_handle),
        )
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await provider.send_message("must not bypass active execution", [])
        assert provider._delivery_uncertain and provider._delivery_path.exists()
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_cold_manager_targeted_stop_and_attention_status_for_persisted_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.models import AgentRuntimeStatus
    from claude_hub.services import managed_chat
    from claude_hub.services.agent_stream import tailer as tailer_module
    from claude_hub.services.agent_stream.tailer import get_native_runtime_snapshot

    server = MasoServer()
    server.fail_send = True
    record = session()
    current_record = record

    def fake_create_native_session(
        sess: ManagedSession,
        conversation_id_persist: Any = None,
        seed_history: Any = None,
        on_seed_consumed: Any = None,
    ) -> MasoHttpSession:
        inst = MasoHttpSession(
            sess,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        inst._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:9796",
            transport=httpx.MockTransport(server.handle),
        )
        return inst

    monkeypatch.setattr(tailer_module, "create_native_session", fake_create_native_session)

    def persist_cid(_sid: str, cid: str) -> None:
        nonlocal current_record
        current_record = current_record.model_copy(
            update={"agent_session_id": cid, "agent_session_id_verified": True}
        )

    manager1 = TailerManager(lambda _id: current_record, persist_session_id=persist_cid)
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await manager1.send_message(
                current_record, "quarantined prompt", [], "turn-quarantined"
            )
    finally:
        await manager1.stop_all()

    cold_record = current_record
    manager2 = TailerManager(lambda _id: current_record, persist_session_id=persist_cid)
    monkeypatch.setattr(managed_chat, "_manager", lambda: manager2)
    try:
        # Even before a tailer is hydrated, managed_chat.runtime_status reports ATTENTION.
        assert managed_chat.runtime_status(cold_record) == AgentRuntimeStatus.ATTENTION

        # Start the cold tailer via subscribe: it terminalizes the unfinished
        # turn-quarantined orphan in the store while preserving the persisted
        # MASO delivery sidecar.
        sub = await manager2.subscribe(cold_record)
        tailer2 = manager2.get_tailer(cold_record.id)
        assert tailer2 is not None
        tailer2.unsubscribe(sub)
        assert await tailer2._store.latest_unfinished_turn() is None
        assert getattr(tailer2.native_transport, "_delivery_uncertain", False) is True
        assert getattr(tailer2.native_transport, "_quarantined_turn_id", None) == "turn-quarantined"

        # Both direct-tab snapshot and managed_chat status report ATTENTION, not IDLE/Ready.
        snap = get_native_runtime_snapshot(cold_record.id)
        assert snap is not None
        assert snap.status == AgentRuntimeStatus.ATTENTION
        assert snap.detail != "Ready"
        assert managed_chat.runtime_status(cold_record) == AgentRuntimeStatus.ATTENTION

        # A subsequent local rejection under quarantine must not overwrite _quarantined_turn_id.
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await manager2.send_message(cold_record, "rejected follow-up", [], "turn-rejected")
        assert getattr(tailer2.native_transport, "_quarantined_turn_id", None) == "turn-quarantined"

        # Stale expected_turn_id is still rejected, preserving stale-target protection.
        assert await manager2.cancel_turn(cold_record, expected_turn_id="stale-turn-id") is False
        assert getattr(tailer2.native_transport, "_delivery_uncertain", False) is True

        # Targeted Stop with the quarantined turn_id succeeds and clears quarantine.
        assert await manager2.cancel_turn(cold_record, expected_turn_id="turn-quarantined") is True
        await tailer2._await_turn_teardown()
        assert getattr(tailer2.native_transport, "_delivery_uncertain", False) is False
        snap_after = get_native_runtime_snapshot(cold_record.id)
        assert snap_after is not None and snap_after.status == AgentRuntimeStatus.IDLE
        assert managed_chat.runtime_status(cold_record) == AgentRuntimeStatus.IDLE
    finally:
        await manager2.stop_all()


@pytest.mark.asyncio
async def test_local_rejection_under_existing_uncertain_quarantine_closes_user_and_scheduled_turns() -> (
    None
):
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
    completion = await tailer.subscribe_completion()
    try:
        await tailer.send_message("first", [], "turn-1")
        server.streams[-1].queue.put_nowait(None)
        while True:
            terminal = await asyncio.wait_for(completion.get(), 2)
            if terminal.type == AgentStreamEventType.TURN_COMPLETED:
                break
        assert terminal.turn_id == "turn-1" and terminal.payload["status"] == "failed"
        assert provider._delivery_uncertain and provider._delivery_path.exists()
        assert server.running

        # User retry rejected locally before reaching provider must close turn-2 immediately.
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await tailer.send_message("user retry", [], "turn-2")
        assert tailer._active_turn_id is None
        assert await tailer._store.latest_unfinished_turn() is None

        # Scheduled patrol turn rejected locally before reaching provider must also close immediately
        # without marking before_delivery or leaving an open ghost turn.
        delivery_markers: list[str] = []
        with pytest.raises(RuntimeError, match="Further sends are refused"):
            await tailer.send_message(
                "scheduled patrol",
                [],
                "scheduled-turn-3",
                turn_metadata={
                    "origin": "scheduled_task",
                    "scheduled_task_id": "sched-1",
                    "yields_to_user": True,
                },
                before_delivery=lambda: delivery_markers.append("submitted"),
                allow_question_answer=False,
            )
        assert delivery_markers == []
        assert tailer._active_turn_id is None
        assert await tailer._store.latest_unfinished_turn() is None

        page = await tailer._store.read_since(-1, 100)
        by_turn: dict[str, list] = {}
        for event in page.events:
            if event.turn_id is not None:
                by_turn.setdefault(event.turn_id, []).append(event)
        for rejected_id in ("turn-2", "scheduled-turn-3"):
            events = by_turn[rejected_id]
            assert [e.type for e in events] == [
                AgentStreamEventType.TURN_STARTED,
                AgentStreamEventType.ERROR,
                AgentStreamEventType.TURN_COMPLETED,
            ]
            assert events[-1].payload == {
                "status": "failed",
                "delivery_rejected": True,
                "delivery_uncertain": True,
                "quarantined_turn_id": "turn-1",
            }

        # Cold tailer startup must not find any orphan or emit runtime_interrupted.
        before_count = len(page.events)
        restarted_record = record.model_copy(
            update={"agent_session_id": "opaque-sid", "agent_session_id_verified": True}
        )
        restarted_provider = transport(server, restarted_record)
        restarted = SessionTailer(
            workspace_id=record.workspace_id,
            session_id=record.id,
            adapter=MasoEventAdapter(),
            session_getter=lambda: restarted_record,
            native_transport=restarted_provider,
        )
        try:
            assert restarted_provider._delivery_uncertain
            await restarted.start()
            after_page = await restarted._store.read_since(-1, 100)
            assert len(after_page.events) == before_count
            assert not any(e.payload.get("runtime_interrupted") is True for e in after_page.events)
        finally:
            await restarted.stop()

        # Stop via tailer when turn_in_flight=False clears the uncertain quarantine so the next turn succeeds.
        assert await tailer.cancel_turn()
        await tailer._await_turn_teardown()
        assert not provider._delivery_uncertain and not provider._delivery_path.exists()
        await tailer.send_message("recovered after stop", [], "turn-4")
        server.streams[-1].emit("post_llm_response", {"agent_id": "main", "content": "ok"})
        server.running = False
        server.streams[-1].emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        while True:
            done = await asyncio.wait_for(completion.get(), 2)
            if done.turn_id == "turn-4" and done.type == AgentStreamEventType.TURN_COMPLETED:
                assert done.payload["status"] == "completed"
                break
    finally:
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_stop_rotation_after_unacknowledged_followup_reinjects_guidance_and_seeds_prior_turns() -> (
    None
):
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
    completion = await tailer.subscribe_completion()
    try:
        await tailer.send_message("Remember token ALPHA-42", [], "turn-1")
        server.streams[-1].emit(
            "post_llm_response",
            {"agent_id": "main", "content": "Acknowledged ALPHA-42."},
        )
        server.running = False
        server.streams[-1].emit("agent_end", {"agent_id": "main", "stop_reason": "completed"})
        while True:
            done = await asyncio.wait_for(completion.get(), 2)
            if done.turn_id == "turn-1" and done.type == AgentStreamEventType.TURN_COMPLETED:
                break
        assert provider._hub_runtime_guidance_injected is True

        # Second turn fails during POST /messages (post_acknowledged=False).
        server.fail_send = True
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await tailer.send_message("ambiguous follow-up", [], "turn-2")
        assert provider._delivery_uncertain and not provider._post_acknowledged

        # Explicit Stop rotates to a fresh MASO session, resets guidance flags,
        # and seeds the completed turn-1 Q/A transcript into the new session.
        assert await tailer.cancel_turn("turn-2") is True
        assert provider._conversation_id == "opaque-sid-2"
        assert provider._hub_runtime_guidance_injected is False
        assert provider._seed_history is not None
        assert "Remember token ALPHA-42" in provider._seed_history
        assert "Acknowledged ALPHA-42." in provider._seed_history
        assert "ambiguous follow-up" not in provider._seed_history

        server.fail_send = False
        await tailer.send_message("continue", [], "turn-3")
        message_calls = [
            body
            for method, path, body in server.calls
            if method == "POST" and path == "/api/sessions/opaque-sid-2/messages"
        ]
        assert len(message_calls) == 1
        sent_content = message_calls[0]["content"]
        assert "CLAUDE_HUB_TAB_ID=" in sent_content
        assert "<claude_hub_instructions>" in sent_content
        assert "<<<FORK_SEED_HISTORY_V1>>>" in sent_content
        assert "Remember token ALPHA-42" in sent_content
        assert "Acknowledged ALPHA-42." in sent_content
        assert sent_content.endswith("continue")
        assert provider._hub_runtime_guidance_injected is True
        assert provider._seed_history is None
        from claude_hub.services.agent_stream.fork_seed import read_seed_sidecar

        assert read_seed_sidecar(record.workspace_id, record.id) is None
        # Consumption callbacks are one-shot, but this live transport can
        # recover again. The second durable seed must also be consumed.
        assert await tailer.cancel_turn("turn-3")
        server.fail_send = True
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await tailer.send_message("another ambiguous action", [], "turn-4")
        assert await tailer.cancel_turn("turn-4")
        server.fail_send = False
        await tailer.send_message("continue again", [], "turn-5")
        assert read_seed_sidecar(record.workspace_id, record.id) is None
    finally:
        tailer.unsubscribe_completion(completion)
        await tailer.stop()


@pytest.mark.asyncio
async def test_stop_rotation_adoption_is_crash_safe_when_sidecar_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = MasoServer()
    server.fail_send = True
    record = session()
    persisted_ids: list[str] = []

    def persist_cid(cid: str) -> None:
        persisted_ids.append(cid)

    provider = MasoHttpSession(record, conversation_id_persist=persist_cid)
    provider._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:9796",
        transport=httpx.MockTransport(server.handle),
    )
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await provider.send_message("uncertain post", [])
        assert provider._delivery_uncertain and not provider._post_acknowledged

        original_unlink = Path.unlink

        def failing_unlink(self_path: Path, *args: Any, **kwargs: Any) -> None:
            if self_path == provider._delivery_path:
                raise OSError("simulated unlink failure after adoption")
            original_unlink(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(OSError, match="simulated unlink failure"):
            await provider.cancel_active_turn()
        monkeypatch.setattr(Path, "unlink", original_unlink)

        assert persisted_ids[-1] == "opaque-sid-2"
        assert provider._delivery_path.exists()
        journal = json.loads(provider._delivery_path.read_text())
        assert journal.get("adopted_session_id") == "opaque-sid-2"

        # A cold restart after a crash before sidecar unlink must reconcile
        # cleanly onto opaque-sid-2 and remove the journal sidecar instead of
        # raising "MASO pending delivery identity could not be verified".
        cold_record = record.model_copy(
            update={"agent_session_id": "opaque-sid", "agent_session_id_verified": True}
        )
        cold_provider = MasoHttpSession(cold_record, conversation_id_persist=persist_cid)
        try:
            assert cold_provider._conversation_id == "opaque-sid-2"
            assert not cold_provider._delivery_uncertain
            assert not cold_provider._needs_abort
            assert not cold_provider._delivery_path.exists()
        finally:
            await cold_provider.stop()
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_legacy_sidecar_cold_targeted_stop_and_failed_stop_teardown_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.models import AgentRuntimeStatus
    from claude_hub.services import managed_chat
    from claude_hub.services.agent_stream import tailer as tailer_module
    from claude_hub.services.agent_stream.tailer import get_native_runtime_snapshot

    server = MasoServer()
    server.fail_send = True
    record = session()
    current_record = record

    def fake_create_native_session(
        sess: ManagedSession,
        conversation_id_persist: Any = None,
        seed_history: Any = None,
        on_seed_consumed: Any = None,
    ) -> MasoHttpSession:
        inst = MasoHttpSession(
            sess,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        inst._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:9796",
            transport=httpx.MockTransport(server.handle),
        )
        return inst

    monkeypatch.setattr(tailer_module, "create_native_session", fake_create_native_session)

    def persist_cid(_sid: str, cid: str) -> None:
        nonlocal current_record
        current_record = current_record.model_copy(
            update={"agent_session_id": cid, "agent_session_id_verified": True}
        )

    manager1 = TailerManager(lambda _id: current_record, persist_session_id=persist_cid)
    try:
        with pytest.raises(RuntimeError, match="acceptance is uncertain"):
            await manager1.send_message(
                current_record, "legacy uncertain prompt", [], "turn-legacy"
            )
    finally:
        await manager1.stop_all()

    # Rewrite sidecar to the legacy pre-fix schema (no turn_id, no post_acknowledged).
    sidecar_path = AgentStreamStore(
        current_record.workspace_id, current_record.id
    ).path.with_suffix(".maso-delivery.json")
    legacy_payload = json.loads(sidecar_path.read_text())
    legacy_payload.pop("turn_id", None)
    legacy_payload.pop("post_acknowledged", None)
    sidecar_path.write_text(json.dumps(legacy_payload))

    manager2 = TailerManager(lambda _id: current_record, persist_session_id=persist_cid)
    monkeypatch.setattr(managed_chat, "_manager", lambda: manager2)
    try:
        sub = await manager2.subscribe(current_record)
        tailer2 = manager2.get_tailer(current_record.id)
        assert tailer2 is not None
        tailer2.unsubscribe(sub)
        assert getattr(tailer2.native_transport, "_quarantined_turn_id", None) == "turn-legacy"
        assert tailer2.native_transport is not None
        caps = tailer2.native_transport.capabilities()
        assert caps.recovery_required is True
        assert caps.quarantined_turn_id == "turn-legacy"

        # If provider abort fails (HTTP 503), targeted Stop propagates RuntimeError
        # instead of returning True while quarantine remains active.
        server.fail_abort = True
        with pytest.raises(RuntimeError, match="native provider turn cancellation failed"):
            await manager2.cancel_turn(current_record, expected_turn_id="turn-legacy")
        assert getattr(tailer2.native_transport, "_delivery_uncertain", False) is True
        assert getattr(tailer2.native_transport, "_needs_abort", False) is True

        # Stale turn ID is still rejected.
        assert await manager2.cancel_turn(current_record, expected_turn_id="other-turn") is False

        # Once abort succeeds, targeted Stop on legacy sidecar completes and rotates.
        server.fail_abort = False
        assert await manager2.cancel_turn(current_record, expected_turn_id="turn-legacy") is True
        assert getattr(tailer2.native_transport, "_delivery_uncertain", False) is False
        assert getattr(tailer2.native_transport, "_needs_abort", False) is False
    finally:
        await manager2.stop_all()

    # Also verify C2-5: accepted POST -> Stop abort 503 -> cold restart with
    # uncertain=False, abort_required=True reports ATTENTION both before and
    # after tailer hydration.
    server3 = MasoServer()
    record3 = session()
    current_record3 = record3

    def persist_cid3(_sid: str, cid: str) -> None:
        nonlocal current_record3
        current_record3 = current_record3.model_copy(
            update={"agent_session_id": cid, "agent_session_id_verified": True}
        )

    def fake_create_native_session3(
        sess: ManagedSession,
        conversation_id_persist: Any = None,
        seed_history: Any = None,
        on_seed_consumed: Any = None,
    ) -> MasoHttpSession:
        inst = MasoHttpSession(
            sess,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        inst._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:9796",
            transport=httpx.MockTransport(server3.handle),
        )
        return inst

    monkeypatch.setattr(tailer_module, "create_native_session", fake_create_native_session3)
    manager3 = TailerManager(lambda _id: current_record3, persist_session_id=persist_cid3)
    try:
        await manager3.send_message(current_record3, "accepted prompt", [], "turn-abort-503")
        server3.fail_abort = True
        with pytest.raises(RuntimeError, match="native provider turn cancellation failed"):
            await manager3.cancel_turn(current_record3, expected_turn_id="turn-abort-503")
        tailer3 = manager3.get_tailer(current_record3.id)
        assert tailer3 is not None
        await tailer3._await_turn_teardown()
        assert getattr(tailer3.native_transport, "_delivery_uncertain", False) is False
        assert getattr(tailer3.native_transport, "_needs_abort", False) is True
    finally:
        await manager3.stop_all()

    manager4 = TailerManager(lambda _id: current_record3, persist_session_id=persist_cid3)
    monkeypatch.setattr(managed_chat, "_manager", lambda: manager4)
    try:
        assert managed_chat.runtime_status(current_record3) == AgentRuntimeStatus.ATTENTION
        sub4 = await manager4.subscribe(current_record3)
        tailer4 = manager4.get_tailer(current_record3.id)
        assert tailer4 is not None
        tailer4.unsubscribe(sub4)
        snap4 = get_native_runtime_snapshot(current_record3.id)
        assert snap4 is not None
        assert snap4.status == AgentRuntimeStatus.ATTENTION
        assert snap4.detail != "Ready"
        assert managed_chat.runtime_status(current_record3) == AgentRuntimeStatus.ATTENTION
        assert tailer4.native_transport is not None
        assert tailer4.native_transport.capabilities().recovery_required is True

        server3.fail_abort = False
        assert (
            await manager4.cancel_turn(current_record3, expected_turn_id="turn-abort-503") is True
        )
        snap4_after = get_native_runtime_snapshot(current_record3.id)
        assert snap4_after is not None and snap4_after.status == AgentRuntimeStatus.IDLE
        assert managed_chat.runtime_status(current_record3) == AgentRuntimeStatus.IDLE
    finally:
        await manager4.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["identity_before", "identity_after", "seed_before", "seed_after", "unlink"],
)
async def test_recovery_adoption_restarts_at_each_durable_boundary(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from claude_hub.services.agent_stream import maso_http
    from claude_hub.services.agent_stream import tailer as tailer_module
    from claude_hub.services.agent_stream.fork_seed import read_seed_sidecar

    server = MasoServer()
    server.fail_send = True
    record = session()
    persisted: dict[str, str] = {}
    fail = False

    def persist(cid: str) -> None:
        if fail and boundary == "identity_before":
            raise OSError("identity write interrupted")
        persisted["id"] = cid
        if fail and boundary == "identity_after":
            raise OSError("identity receipt interrupted")

    provider = MasoHttpSession(record, conversation_id_persist=persist)
    provider._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:9796", transport=httpx.MockTransport(server.handle)
    )
    store = AgentStreamStore(record.workspace_id, record.id)
    ctx = NormalizeContext(
        session_id=record.id,
        tab_id=record.tab_id,
        agent_type=record.agent_type,
        run_epoch=1,
        turn_id="prior-completed",
    )
    for kind, payload in [
        (AgentStreamEventType.TURN_STARTED, {"summary": "Remember the blue marker"}),
        (AgentStreamEventType.TEXT_DELTA, {"text": "The marker is blue."}),
        (AgentStreamEventType.TURN_COMPLETED, {"status": "completed"}),
    ]:
        await store.append(ctx.event(kind, payload))
    with pytest.raises(RuntimeError, match="acceptance is uncertain"):
        await provider.send_message("ambiguous action must not be replayed", [])

    original_seed = maso_http.write_seed_sidecar
    original_unlink = Path.unlink

    def write_seed(workspace_id: str, session_id: str, body: str) -> None:
        if boundary == "seed_before":
            raise OSError("seed write interrupted")
        original_seed(workspace_id, session_id, body)
        if boundary == "seed_after":
            raise OSError("seed receipt interrupted")

    def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if boundary == "unlink" and path == provider._delivery_path:
            raise OSError("journal cleanup interrupted")
        original_unlink(path, *args, **kwargs)

    fail = True
    with monkeypatch.context() as faults:
        faults.setattr(maso_http, "write_seed_sidecar", write_seed)
        faults.setattr(Path, "unlink", unlink)
        with pytest.raises(OSError, match="interrupted"):
            await provider.cancel_active_turn()
        assert provider.capabilities().recovery_required
        assert provider._delivery_path.exists()
        assert not any(path == "/api/sessions/opaque-sid-2/exit" for _, path, _ in server.calls)
    fail = False
    await provider.stop()
    assert not any(path == "/api/sessions/opaque-sid-2/exit" for _, path, _ in server.calls)

    # Restart with whichever identity actually reached storage. Both old and
    # adopted identities are covered; the old message UUID is never rebound.
    cold_record = record.model_copy(
        update={
            "agent_session_id": persisted["id"],
            "agent_session_id_verified": True,
        }
    )
    cold = MasoHttpSession(cold_record, conversation_id_persist=persist)
    assert cold.active_thread_id == "opaque-sid-2"
    assert not cold.capabilities().recovery_required
    assert not cold._delivery_path.exists()
    assert "marker is blue" in (read_seed_sidecar(record.workspace_id, record.id) or "")
    await cold.stop()

    # Another restart after journal cleanup must still preserve the one-shot
    # text seed through the real manager/factory lifecycle.
    current = cold_record.model_copy(update={"agent_session_id": persisted["id"]})

    def factory(sess: ManagedSession, **kwargs: Any) -> MasoHttpSession:
        instance = MasoHttpSession(sess, **kwargs)
        instance._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:9796",
            transport=httpx.MockTransport(server.handle),
        )
        return instance

    monkeypatch.setattr(tailer_module, "create_native_session", factory)
    manager = TailerManager(lambda _id: current)
    server.fail_send = False
    try:
        await manager.send_message(current, "continue", [], "next-turn")
        posts = [
            (path, body)
            for method, path, body in server.calls
            if method == "POST" and path.endswith("/messages")
        ]
        assert len(posts) == 2
        path, body = posts[-1]
        assert path == "/api/sessions/opaque-sid-2/messages"
        assert "marker is blue" in body["content"]
        assert "<claude_hub_instructions>" in body["content"]
        assert "ambiguous action must not be replayed" not in body["content"]
        assert read_seed_sidecar(record.workspace_id, record.id) is None
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_legacy_target_ignores_rejected_turns_and_never_accepts_arbitrary_terminal() -> None:
    server = MasoServer()
    server.fail_send = True
    record = session()
    provider = transport(server, record)
    with pytest.raises(RuntimeError, match="acceptance is uncertain"):
        await provider.send_message("ambiguous", [])
    store = AgentStreamStore(record.workspace_id, record.id)
    for turn_id, rejected in [("uncertain-turn", False), ("rejected-retry", True)]:
        ctx = NormalizeContext(
            session_id=record.id,
            tab_id=record.tab_id,
            agent_type=record.agent_type,
            run_epoch=1,
            turn_id=turn_id,
        )
        await store.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": turn_id}))
        await store.append(
            ctx.event(
                AgentStreamEventType.TURN_COMPLETED,
                {"status": "failed", "delivery_rejected": rejected},
            )
        )
    await provider.stop()
    cold = transport(
        server,
        record.model_copy(
            update={"agent_session_id": "opaque-sid", "agent_session_id_verified": True}
        ),
    )
    tailer = SessionTailer(
        workspace_id=record.workspace_id,
        session_id=record.id,
        adapter=MasoEventAdapter(),
        session_getter=lambda: record,
        native_transport=cold,
    )
    try:
        assert cold.capabilities().quarantined_turn_id == "uncertain-turn"
        before = len(server.calls)
        assert not await tailer.cancel_turn("rejected-retry")
        assert len(server.calls) == before
        # Original versions left locally rejected ghost starts without the
        # delivery_rejected marker, then cold recovery marked them cancelled.
        ghost = NormalizeContext(
            session_id=record.id,
            tab_id=record.tab_id,
            agent_type=record.agent_type,
            run_epoch=2,
            turn_id="old-ghost",
        )
        await store.append(
            ghost.event(AgentStreamEventType.TURN_STARTED, {"summary": "refused locally"})
        )
        await store.append(
            ghost.event(AgentStreamEventType.TURN_COMPLETED, {"status": "cancelled"})
        )
        assert cold._recover_legacy_quarantined_turn_id() is None
        cold._quarantined_turn_id = None  # Uncorrelated legacy recovery stays untargeted.
        assert not await tailer.cancel_turn("uncertain-turn")
        assert len(server.calls) == before
        assert await tailer.cancel_turn()
    finally:
        await tailer.stop()
