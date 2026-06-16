"""Tests for the reusable ``/hub`` chat command dispatcher.

:func:`run_hub_chat_command` is a lark-independent helper an external bot can
call to turn a ``/hub …`` chat message into a backend action and a reply
string. We test it against a :class:`HubClient` backed by an
``httpx.MockTransport`` (the same pattern as ``tests/test_cli.py``).
"""

from __future__ import annotations

import json
from typing import Callable, List

import httpx

from claude_hub.cli.client import HubClient
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
