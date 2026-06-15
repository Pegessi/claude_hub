"""Tests for the Feishu long-connection bot command logic.

The live WebSocket connection is never exercised (it needs network); instead we
test :func:`run_hub_chat_command` against a :class:`HubClient` backed by an
``httpx.MockTransport`` (the same pattern as ``tests/test_cli.py``), plus a
smoke check that the ``feishu-bot`` subcommand is registered.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable, List, Tuple

import httpx

from claude_hub.cli.client import HubClient
from claude_hub.cli.feishu_bot import handle_message_event
from claude_hub.cli.hub_commands import run_hub_chat_command


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HubClient:
    transport = httpx.MockTransport(handler)
    return HubClient(base_url="http://testserver", transport=transport)


def test_help_returns_usage() -> None:
    client = make_client(lambda req: httpx.Response(200, json={}))
    reply = run_hub_chat_command(client, "/hub help")
    assert "/hub workspaces" in reply
    assert "/hub task create" in reply


def test_non_hub_message_is_empty() -> None:
    client = make_client(lambda req: httpx.Response(200, json={}))
    assert run_hub_chat_command(client, "hello there") == ""


def test_workspaces_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces"
        return httpx.Response(200, json=[{"id": "ws1", "name": "Demo"}])

    reply = run_hub_chat_command(make_client(handler), "/hub workspaces")
    assert "ws1" in reply
    assert "Demo" in reply


def test_workspaces_empty() -> None:
    client = make_client(lambda req: httpx.Response(200, json=[]))
    assert run_hub_chat_command(client, "/hub workspaces") == "No workspaces configured."


def test_status_summarizes_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/board"
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {"id": "t1", "title": "A", "status": "queued"},
                    {"id": "t2", "title": "B", "status": "done"},
                ]
            },
        )

    reply = run_hub_chat_command(make_client(handler), "/hub status ws1")
    assert "Workspace ws1: 2 task(s)" in reply
    assert "t1" in reply


def test_task_create_posts_title_and_prompt() -> None:
    bodies: List[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/tasks"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "tNEW", "title": "My Title"})

    reply = run_hub_chat_command(make_client(handler), '/hub task create ws1 "My Title" "Do it"')
    assert "tNEW" in reply
    assert bodies[0]["title"] == "My Title"
    assert bodies[0]["prompt"] == "Do it"


def test_task_start_replies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/tasks/t1/start"
        return httpx.Response(200, json={"id": "t1", "status": "queued"})

    reply = run_hub_chat_command(make_client(handler), "/hub task start t1")
    assert "t1" in reply
    assert "queued" in reply


def test_task_abort_replies() -> None:
    bodies: List[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/tasks/t1/abort"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1", "status": "aborted"})

    reply = run_hub_chat_command(make_client(handler), '/hub task abort t1 "no longer needed"')
    assert "aborted" in reply
    assert bodies[0]["reason"] == "no longer needed"


def test_lessons_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/lessons"
        assert request.url.params.get("query") == "auth"
        return httpx.Response(200, json=[{"id": "l1", "title": "Lesson one"}])

    reply = run_hub_chat_command(make_client(handler), "/hub lessons ws1 auth")
    assert "l1" in reply
    assert "Lesson one" in reply


def test_unknown_verb_returns_help() -> None:
    client = make_client(lambda req: httpx.Response(200, json={}))
    reply = run_hub_chat_command(client, "/hub frobnicate")
    assert "/hub workspaces" in reply


def test_mention_stripping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces"
        return httpx.Response(200, json=[{"id": "ws1", "name": "Demo"}])

    reply = run_hub_chat_command(make_client(handler), "@_user_1 /hub workspaces")
    assert "ws1" in reply


def test_error_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    reply = run_hub_chat_command(make_client(handler), "/hub status ws1")
    assert reply.startswith("error:")
    assert "nope" in reply


def test_subcommand_registered() -> None:
    from claude_hub.cli.main import cli

    assert "feishu-bot" in cli.commands


# -- Callback adapter (handle_message_event) ------------------------------------
#
# These exercise the pure adapter that lark's WS handler submits to a worker
# thread. lark is never imported: we inject a fake event object, a HubClient
# factory backed by httpx.MockTransport, and a stub reply sender.


def _fake_event(message_type: str, content: str, chat_id: str = "oc_1") -> SimpleNamespace:
    """Build a P2ImMessageReceiveV1-like object for the adapter."""
    message = SimpleNamespace(message_type=message_type, content=content, chat_id=chat_id)
    return SimpleNamespace(event=SimpleNamespace(message=message))


def test_adapter_runs_command_and_sends_reply() -> None:
    seen_paths: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json=[{"id": "ws1", "name": "Demo"}])

    sent: List[Tuple[str, str]] = []

    def reply_fn(chat_id: str, reply: str) -> None:
        sent.append((chat_id, reply))

    data = _fake_event("text", json.dumps({"text": "/hub workspaces"}))
    handle_message_event(data, lambda: make_client(handler), reply_fn)

    assert seen_paths == ["/api/workspaces"]
    assert len(sent) == 1
    chat_id, reply = sent[0]
    assert chat_id == "oc_1"
    assert "ws1" in reply and "Demo" in reply


def test_adapter_ignores_non_text_messages() -> None:
    factory_calls: List[int] = []

    def factory() -> HubClient:
        factory_calls.append(1)
        return make_client(lambda req: httpx.Response(200, json={}))

    sent: List[Tuple[str, str]] = []
    data = _fake_event("image", json.dumps({"image_key": "img_x"}))
    handle_message_event(data, factory, lambda c, r: sent.append((c, r)))

    assert sent == []
    assert factory_calls == []  # no command run for non-text


def test_adapter_skips_empty_reply() -> None:
    sent: List[Tuple[str, str]] = []
    # A non-/hub message yields an empty reply from run_hub_chat_command.
    data = _fake_event("text", json.dumps({"text": "just chatting"}))
    handle_message_event(
        data,
        lambda: make_client(lambda req: httpx.Response(200, json={})),
        lambda c, r: sent.append((c, r)),
    )
    assert sent == []


def test_adapter_swallows_reply_errors() -> None:
    def reply_fn(chat_id: str, reply: str) -> None:
        raise RuntimeError("network down")

    data = _fake_event("text", json.dumps({"text": "/hub workspaces"}))
    # Must not raise even though the reply sender blows up.
    handle_message_event(
        data,
        lambda: make_client(lambda req: httpx.Response(200, json=[])),
        reply_fn,
    )
