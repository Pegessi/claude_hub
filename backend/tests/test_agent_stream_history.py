"""Question/answer projection and bounded, lazy process-history contracts."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    StreamCapabilities,
    User,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import store as store_module
from claude_hub.services.agent_stream.history import ConversationHistoryIndex
from claude_hub.services.agent_stream.store import AgentStreamStore


@pytest.fixture(autouse=True)
def isolated_event_loop_policy() -> Iterator[None]:
    # asyncio.run() clears its policy's current loop. Keep that test-local so
    # synchronous stream tests do not inherit a closed/missing default loop.
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentStreamStore:
    monkeypatch.setattr(store_module, "_state_root", lambda: tmp_path)
    return AgentStreamStore("workspace", "session")


def event(kind: str, turn_id: Optional[str] = "turn", **payload: Any) -> AgentStreamEvent:
    return AgentStreamEvent(
        stream_sequence=0,
        session_id="session",
        tab_id="tab",
        agent_type=AgentType.MASO,
        type=AgentStreamEventType(kind),
        turn_id=turn_id,
        message_id=f"{turn_id}:{kind}",
        payload=payload,
        created_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )


def test_summary_preserves_questions_answers_controls_and_raw_log(store: AgentStreamStore) -> None:
    async def run() -> None:
        source = [
            event("turn_started", summary="Question", attachments=[{"id": "image"}]),
            event("thinking_delta", text="private process " * 500),
            event("tool_call_started", name="Read", args={"body": "large args " * 500}),
            event("tool_call_completed", result="large result " * 1000),
            event("text_delta", text="Answer ![image](https://example.test/image.png)"),
            event("approval_required", tool_call_id="confirm", title="Permission"),
            event("approval_resolved", tool_call_id="confirm"),
            event("error", message="visible error"),
            event("turn_completed", status="cancelled"),
        ]
        await store.replace_all(source)
        before = store.path.read_bytes()
        summary = await store.read_conversation(-1, 5000, compact=True)
        assert [item.stream_sequence for item in summary.events] == [0, 4, 5, 6, 7, 8]
        assert summary.events[0].payload["attachments"] == [{"id": "image"}]
        assert summary.events[1].payload["text"].startswith("Answer ![image]")
        assert summary.history_details[0].model_dump() == {
            "turn_id": "turn",
            "first_sequence": 0,
            "last_sequence": 8,
            "event_count": 9,
        }
        assert summary.next_sequence == 8 and not summary.has_more
        assert "large result" not in summary.model_dump_json()
        assert store.path.read_bytes() == before
        raw = await store.read_since(-1, 5000)
        assert len(raw.events) == 9 and raw.history_details == []
        details = await store.read_turn_since("turn", -1, 8, 5000)
        assert details.events == raw.events
        assert store._history.rows[3].visible is None

    asyncio.run(run())


def test_summary_preserves_legacy_and_child_failures(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("thinking_delta", None, text="legacy process"),
                event("turn_started", summary="Question"),
                event("text_delta", subagent_thread="child", text="child reply"),
                event("status", subagent_thread="child", subagent_status="running", text="running"),
                event("status", subagent_thread="child", subagent_status="failed", text="failed"),
                event(
                    "status", subagent_thread="child", subagent_status="cancelled", text="cancelled"
                ),
                event("error", subagent_thread="child", message="child error"),
                event("approval_required", subagent_thread="child", title="child approval"),
                event("text_delta", text="main answer"),
            ]
        )
        page = await store.read_conversation()
        assert [item.stream_sequence for item in page.events] == [0, 1, 4, 5, 6, 7, 8]
        assert page.history_details[0].event_count == 8

    asyncio.run(run())


def test_hidden_tail_advances_cursor_without_visible_events(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("turn_started", summary="Question"),
                event("thinking_delta", text="one"),
                event("tool_call_completed", result="two"),
            ]
        )
        first = await store.read_conversation(-1, 1)
        assert first.next_sequence == 0 and first.has_more
        assert first.history_details == []
        tail = await store.read_conversation(first.next_sequence, 1)
        assert tail.events == []
        assert tail.next_sequence == 2 and not tail.has_more
        assert tail.history_details[0].last_sequence == 2
        assert tail.history_details[0].event_count == 3
        empty = await store.read_conversation(tail.next_sequence, 1)
        assert empty.events == [] and empty.history_details == []
        assert empty.next_sequence == 2 and not empty.has_more

    asyncio.run(run())


def test_summary_bounds_metadata_when_all_turns_are_hidden(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [event("thinking_delta", str(index), text="hidden") for index in range(7)]
        )
        since = -1
        found: list[str] = []
        for _ in range(4):
            page = await store.read_conversation(since, 2)
            assert page.events == [] and len(page.history_details) <= 2
            found.extend(item.turn_id for item in page.history_details)
            assert page.next_sequence > since
            since = page.next_sequence
        assert found == [str(index) for index in range(7)]
        assert not page.has_more and since == 6

    asyncio.run(run())


def test_summary_compaction_keeps_omitted_boundaries(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("text_delta", text="a"),
                event("text_delta", text="b"),
                event("tool_call_started", name="Read"),
                event("text_delta", text="c"),
                event("text_delta", text="snapshot", snapshot=True),
            ]
        )
        page = await store.read_conversation(-1, 5000, compact=True)
        assert [item.payload["text"] for item in page.events] == ["ab", "c", "snapshot"]
        assert page.events[0].payload["_history_chunk_count"] == 2
        assert page.next_sequence == 4

    asyncio.run(run())


def test_index_only_parses_appended_lines(
    store: AgentStreamStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexed: list[int] = []
    original = ConversationHistoryIndex._index_line

    def track(self: ConversationHistoryIndex, line: bytes, offset: int) -> None:
        indexed.append(offset)
        original(self, line, offset)

    monkeypatch.setattr(ConversationHistoryIndex, "_index_line", track)

    async def run() -> None:
        await store.replace_all(
            [
                event("turn_started", summary="Question"),
                *[event("tool_call_completed", result="large" * 1000) for _ in range(25)],
                event("text_delta", text="Answer"),
            ]
        )
        await store.read_conversation(-1, 1)
        assert len(indexed) == 27
        old_size = store.path.stat().st_size
        indexed.clear()
        await store.read_conversation(0, 1)
        await store.read_conversation(-1, 5000)
        assert indexed == []
        await store.append(event("thinking_delta", text="new hidden tail"))
        page = await store.read_conversation(26, 5000)
        assert indexed == [old_size]
        assert page.events == [] and page.next_sequence == 27
        assert page.history_details[0].event_count == 28

    asyncio.run(run())


def test_partial_append_waits_for_complete_line(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all([event("turn_started", summary="Question")])
        await store.read_conversation()
        new = event("text_delta", text="Answer").model_copy(update={"stream_sequence": 1})
        line = new.model_dump_json().encode()
        with store.path.open("ab") as handle:
            handle.write(line[: len(line) // 2])
        page = await store.read_conversation(0)
        assert page.events == [] and page.next_sequence == 0
        with store.path.open("ab") as handle:
            handle.write(line[len(line) // 2 :] + b"\n")
        complete = await store.read_conversation(0)
        assert complete.events == [new] and complete.next_sequence == 1

    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["replace_all", "inode", "same_size", "shrink"])
def test_index_invalidates_after_replacement(store: AgentStreamStore, replacement: str) -> None:
    async def run() -> None:
        await store.replace_all([event("text_delta", text="old answer")])
        assert (await store.read_conversation()).events[0].payload["text"] == "old answer"
        updated = event("text_delta", text="new answer")
        if replacement == "replace_all":
            await store.replace_all([updated])
        elif replacement == "inode":
            other = store.path.with_suffix(".replacement")
            other.write_text(updated.model_dump_json() + "\n")
            other.replace(store.path)
        else:
            if replacement == "shrink":
                updated = event("text_delta", text="new")
            old_mtime = store.path.stat().st_mtime_ns
            store.path.write_text(updated.model_dump_json() + "\n")
            os.utime(store.path, ns=(old_mtime + 1, old_mtime + 1))
        assert (await store.read_conversation()).events == [updated]

    asyncio.run(run())


def test_edit_restore_and_clear_invalidate_history(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("turn_started", summary="Question"),
                event("thinking_delta", text="process"),
                event("text_delta", text="Answer"),
            ]
        )
        before = await store.read_conversation()
        backup = await store.snapshot()
        await store.truncate_before(1)
        truncated = await store.read_conversation()
        assert truncated.next_sequence == 0 and truncated.history_details == []
        await store.restore(backup)
        restored = await store.read_conversation()
        assert restored == before
        await store.clear()
        empty = await store.read_conversation()
        assert empty.events == [] and empty.history_details == [] and empty.next_sequence == -1
        assert (await store.read_turn_since("turn", -1, 2)).events == []
        await store.append(event("text_delta", text="new session content"))
        assert (await store.read_conversation()).events[0].payload["text"] == "new session content"

    asyncio.run(run())


def test_turn_details_filter_interleaved_turns_and_fixed_upper_bound(
    store: AgentStreamStore,
) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("turn_started", "a", summary="A"),
                event("thinking_delta", "a", text="thinking A"),
                event("turn_started", "b", summary="B"),
                event("text_delta", "a", text="reply A"),
                event("tool_call_completed", "b", result="B only"),
                event("tool_call_completed", "a", result="A only"),
                event("turn_completed", "a", status="completed"),
            ]
        )
        await store.read_conversation()
        first = await store.read_turn_since("a", -1, 5, 2, compact=True)
        assert [item.stream_sequence for item in first.events] == [0, 1]
        assert first.has_more and first.next_sequence == 1
        await store.append(event("text_delta", "a", text="late A"))
        second = await store.read_turn_since("a", first.next_sequence, 5, 2, compact=True)
        assert [item.stream_sequence for item in second.events] == [3, 5]
        assert not second.has_more and second.next_sequence == 5
        assert "B only" not in second.model_dump_json()
        assert "late A" not in second.model_dump_json()
        assert (await store.read_turn_since("missing", -1, 5)).events == []
        empty = await store.read_turn_since("a", 10, 5)
        assert empty.events == [] and not empty.has_more and empty.next_sequence == 10

    asyncio.run(run())


def test_store_history_isolation(store: AgentStreamStore) -> None:
    async def run() -> None:
        other = AgentStreamStore("other-workspace", "session")
        await store.replace_all([event("thinking_delta", text="first secret")])
        await other.replace_all([event("thinking_delta", text="second secret")])
        await store.read_conversation()
        await other.read_conversation()
        page = await other.read_turn_since("turn", -1, 0)
        assert page.events[0].payload["text"] == "second secret"
        assert "first secret" not in page.model_dump_json()

    asyncio.run(run())


def test_cancelled_history_read_joins_io_before_replacement(
    store: AgentStreamStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = ConversationHistoryIndex.conversation_page

    def blocked(self: ConversationHistoryIndex, *args: Any) -> AgentStreamEventPage:
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(ConversationHistoryIndex, "conversation_page", blocked)

    async def run() -> None:
        await store.replace_all([event("text_delta", text="old")])
        reading = asyncio.create_task(store.read_conversation())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            reading.cancel()
            replacing = asyncio.create_task(store.replace_all([event("text_delta", text="new")]))
            await asyncio.sleep(0)
            assert store._read_lock.locked() and not replacing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await reading
            await replacing
            assert (await store.read_conversation()).events[0].payload["text"] == "new"
        finally:
            release.set()

    asyncio.run(run())


def test_summary_replay_guard_preserves_earlier_legacy_events(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("tool_call_completed", None, tool_call_id="shared", result="legacy first"),
                event("turn_started", summary="Question"),
                event("tool_call_started", tool_call_id="shared", name="Read"),
                event("tool_call_completed", tool_call_id="shared", result="attributed"),
                event("tool_call_completed", None, tool_call_id="shared", result="late replay"),
                event(
                    "approval_required", None, tool_call_id="shared", title="late approval replay"
                ),
                event("tool_call_completed", None, tool_call_id="unrelated", result="other legacy"),
                event("approval_required", tool_call_id="permission", title="visible approval"),
            ]
        )
        page = await store.read_conversation(-1, 5000)
        assert [item.stream_sequence for item in page.events] == [0, 1, 6, 7]
        assert page.attributed_tool_ids == ["shared", "permission"]
        assert page.events[0].payload["result"] == "legacy first"
        raw = await store.read_since(-1, 5000)
        assert len(raw.events) == 8 and raw.attributed_tool_ids == []
        await store.append(
            event("tool_call_completed", None, tool_call_id="shared", result="new replay")
        )
        tail = await store.read_conversation(7, 5000)
        assert tail.events == [] and tail.next_sequence == 8
        assert tail.attributed_tool_ids == []
        await store.replace_all(
            [event("tool_call_completed", None, tool_call_id="shared", result="new legacy")]
        )
        assert (await store.read_conversation()).events[0].payload["result"] == "new legacy"

    asyncio.run(run())


def test_attributed_id_pagination_is_bounded_in_hidden_tail(store: AgentStreamStore) -> None:
    async def run() -> None:
        await store.replace_all(
            [
                event("turn_started", summary="Question"),
                event("tool_call_started", tool_call_id="a", name="Read"),
                event("tool_call_completed", tool_call_id="a", result="A"),
                event("tool_call_started", name="Read").model_copy(update={"call_id": "b"}),
                event("tool_call_completed", result="B").model_copy(update={"call_id": "b"}),
            ]
        )
        first = await store.read_conversation(-1, 1)
        assert first.attributed_tool_ids == [] and first.next_sequence == 0
        second = await store.read_conversation(0, 1)
        assert second.events == [] and second.attributed_tool_ids == ["a"]
        assert second.next_sequence == 2 and second.has_more
        assert second.history_details[0].event_count == 3
        third = await store.read_conversation(second.next_sequence, 1)
        assert third.events == [] and third.attributed_tool_ids == ["b"]
        assert third.next_sequence == 4 and not third.has_more
        assert third.history_details[0].event_count == 5

    asyncio.run(run())


@pytest.fixture
def history_client(
    store: AgentStreamStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, MagicMock, MagicMock]]:
    from claude_hub.api import agent_stream as api

    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="session",
        workspace_id="workspace",
        tab_id="tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.MASO,
        status=ManagedSessionStatus.WORKING,
        title="Chat",
        workspace_path=str(store.path.parent),
        tmux_session="unused",
        created_at=now,
        updated_at=now,
    )
    manager = MagicMock()
    manager.ensure_started = AsyncMock()
    manager.hard_failed.return_value = False
    manager.get_store.return_value = store
    current_check = MagicMock()

    @asynccontextmanager
    async def interaction(*args: Any, **kwargs: Any) -> AsyncIterator[ManagedSession]:
        yield session

    monkeypatch.setattr(api, "_terminal_tab_session_or_404", lambda _: session)
    monkeypatch.setattr(api, "_session_or_404", lambda _: session)
    monkeypatch.setattr(api, "_get_tailer_manager", lambda: manager)
    monkeypatch.setattr(api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(api, "_managed_stream_interaction", interaction)
    monkeypatch.setattr(api, "_require_current_managed_session", current_check)
    monkeypatch.setattr(
        api, "_capabilities_for", AsyncMock(return_value=StreamCapabilities(structured=True))
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="u", name="User", email="u@test"
    )
    with TestClient(app) as client:
        yield client, current_check, manager


@pytest.mark.parametrize("path", ["tabs/tab", "sessions/session"])
def test_history_http_contract(store: AgentStreamStore, history_client: Any, path: str) -> None:
    client, current_check, manager = history_client
    asyncio.run(
        store.replace_all(
            [
                event("turn_started", summary="Question"),
                event("tool_call_completed", result="large process result"),
                event("text_delta", text="Answer"),
            ]
        )
    )
    endpoint = f"/api/workspaces/{path}/stream/events"
    raw = client.get(endpoint, params={"limit": 5000})
    assert raw.status_code == 200 and len(raw.json()["events"]) == 3
    summary = client.get(
        endpoint, params={"view": "conversation", "compact": "true", "limit": 5000}
    )
    assert summary.status_code == 200
    assert [item["stream_sequence"] for item in summary.json()["events"]] == [0, 2]
    assert summary.json()["history_details"][0]["event_count"] == 3
    details = client.get(
        endpoint, params={"turn_id": "turn", "through_sequence": 2, "compact": "true"}
    )
    assert details.status_code == 200 and len(details.json()["events"]) == 3
    assert details.json()["next_sequence"] == 2
    manager.get_store.assert_called_with("workspace", "session")
    if path.startswith("sessions"):
        assert current_check.call_count == 3


@pytest.mark.parametrize(
    "params, status",
    [
        ({"view": "unknown"}, 422),
        ({"turn_id": "turn"}, 400),
        ({"through_sequence": 5}, 400),
        ({"view": "conversation", "turn_id": "turn", "through_sequence": 5}, 400),
        ({"turn_id": "", "through_sequence": 5}, 422),
        ({"turn_id": "turn", "through_sequence": -1}, 422),
        ({"view": "conversation", "limit": 5001}, 422),
    ],
)
def test_invalid_history_queries_are_rejected(
    history_client: Any, params: Any, status: int
) -> None:
    client, _, manager = history_client
    response = client.get("/api/workspaces/tabs/tab/stream/events", params=params)
    assert response.status_code == status
    manager.ensure_started.assert_not_awaited()


def test_managed_history_checks_current_session_after_read(
    history_client: Any,
) -> None:
    client, current_check, _ = history_client
    current_check.side_effect = HTTPException(status_code=409, detail="retired")
    response = client.get(
        "/api/workspaces/sessions/session/stream/events", params={"view": "conversation"}
    )
    assert response.status_code == 409
