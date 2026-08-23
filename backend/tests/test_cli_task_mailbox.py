"""CLI contract for ``claude-hub task tree|events|wait|ack|followup|send``."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs

import click
import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)

SAMPLE_TASK = {
    "id": "task-1",
    "title": "parent",
    "status": "todo",
    "parent_task_id": None,
    "agent_type": "claude",
    "consumer_ack_sequence": 0,
}
SAMPLE_CHILD = {
    "id": "task-2",
    "title": "child",
    "status": "todo",
    "parent_task_id": "task-1",
    "agent_type": "claude",
    "consumer_ack_sequence": 0,
}
SAMPLE_EVENT = {
    "sequence": 4,
    "type": "followup",
    "call_id": "fu-1",
    "task_id": "task-2",
    "consumer_key": "task:task-2",
}


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> HubClient:
    return HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler), **kwargs)


def patch_get_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> List[httpx.Request]:
    captured: List[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def fake_get_client(ctx: click.Context) -> HubClient:
        return make_client(recording_handler, **kwargs)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    return captured


def _query(request: httpx.Request) -> Dict[str, List[str]]:
    return parse_qs(
        request.url.query.decode()
        if isinstance(request.url.query, bytes)
        else str(request.url.query)
    )


def _json(request: httpx.Request) -> Dict[str, Any]:
    return json.loads(request.content)


def test_help_registers_task_mailbox_commands() -> None:
    runner = CliRunner()
    group = runner.invoke(cli, ["task", "--help"])
    assert group.exit_code == 0, group.output
    for name in ("tree", "events", "wait", "ack", "followup", "send"):
        assert name in group.output
    assert "interrupt" not in group.output


def test_tree_events_hit_workspace_task_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tree"):
            return httpx.Response(200, json=[SAMPLE_TASK, SAMPLE_CHILD])
        return httpx.Response(200, json=[SAMPLE_EVENT])

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    tree = runner.invoke(cli, ["--json", "task", "tree", "ws1", "task-1"])
    events = runner.invoke(
        cli,
        ["--json", "task", "events", "ws1", "task-1", "--since-sequence", "2", "--subtree"],
    )
    resident = runner.invoke(
        cli, ["--json", "task", "events", "ws1", "task-1", "--since-sequence", "0"]
    )
    assert tree.exit_code == 0, tree.output
    assert events.exit_code == 0, events.output
    assert resident.exit_code == 0, resident.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/tree"
    assert captured[1].url.path == "/api/workspaces/ws1/tasks/task-1/events"
    assert _query(captured[1])["since_sequence"] == ["2"]
    assert _query(captured[1])["subtree"] == ["true"]
    assert captured[2].url.path == "/api/workspaces/ws1/tasks/task-1/events"
    for request in captured:
        assert "/api/agent-tree" not in request.url.path


def test_followup_and_send_alias_use_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_EVENT)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    followup = runner.invoke(
        cli,
        [
            "--json",
            "task",
            "followup",
            "ws1",
            "task-1",
            "--message",
            "fix it",
            "--call-id",
            "followup-1",
        ],
    )
    send = runner.invoke(
        cli,
        ["--json", "task", "send", "ws1", "task-1", "--message", "note", "--call-id", "send-1"],
    )
    minted = runner.invoke(cli, ["--json", "task", "followup", "ws1", "task-1", "--message", "x"])
    assert followup.exit_code == 0, followup.output
    assert send.exit_code == 0, send.output
    assert minted.exit_code == 0, minted.output
    assert [request.url.path for request in captured] == [
        "/api/workspaces/ws1/tasks/task-1/followup",
        "/api/workspaces/ws1/tasks/task-1/followup",
        "/api/workspaces/ws1/tasks/task-1/followup",
    ]
    assert _json(captured[0]) == {"message": "fix it", "call_id": "followup-1"}
    assert _json(captured[1]) == {"message": "note", "call_id": "send-1"}
    minted_id = _json(captured[2])["call_id"]
    assert UUID_RE.match(minted_id)
    assert "call_id=followup-1" in followup.stderr
    assert "call_id=send-1" in send.stderr
    assert f"call_id={minted_id}" in minted.stderr
    for request in captured:
        assert "/sessions/" not in request.url.path
        assert "/api/agent-tree" not in request.url.path


def test_wait_ack_flushes_events_then_acks_max_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {**SAMPLE_EVENT, "sequence": 3, "call_id": "a"},
        {**SAMPLE_EVENT, "sequence": 8, "call_id": "b"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=events)
        assert request.url.path.endswith("/ack")
        assert _json(request) == {"sequence": 8}
        return httpx.Response(200, json={**SAMPLE_TASK, "consumer_ack_sequence": 8})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        ["--json", "task", "wait", "ws1", "task-1", "--subtree", "--ack"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == events
    assert [item.url.path for item in captured] == [
        "/api/workspaces/ws1/tasks/task-1/wait",
        "/api/workspaces/ws1/tasks/task-1/ack",
    ]
    assert _query(captured[0])["subtree"] == ["true"]
    assert json.loads(result.stderr) == {"acked_sequence": 8}
    assert result.stdout.strip().startswith("[")
    assert "acked_sequence" not in result.stdout


def test_empty_wait_ack_does_not_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/wait")
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "task", "wait", "ws1", "task-1", "--ack"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "acked_sequence" not in result.stderr
    assert [item.url.path for item in captured] == ["/api/workspaces/ws1/tasks/task-1/wait"]


def test_ack_task_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**SAMPLE_TASK, "consumer_ack_sequence": 7})

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    task_ack = runner.invoke(cli, ["--json", "task", "ack", "ws1", "task-1", "7"])
    assert task_ack.exit_code == 0, task_ack.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/ack"
    assert _json(captured[0]) == {"sequence": 7}
    for request in captured:
        assert "/api/agent-tree" not in request.url.path


def test_wait_sends_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "task",
            "wait",
            "ws1",
            "task-1",
            "--timeout-seconds",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/wait"
    assert _query(captured[0])["timeout_seconds"] == ["7.0"]
    assert json.loads(result.stdout) == []
    assert "/api/agent-tree" not in captured[0].url.path


def test_task_wait_sends_timeout_seconds_alt(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        ["--json", "task", "wait", "ws1", "task-1", "--timeout-seconds", "0.2"],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/wait"
    assert _query(captured[0])["timeout_seconds"] == ["0.2"]


def test_task_wait_ack_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[SAMPLE_EVENT])
        return httpx.Response(200, json={**SAMPLE_TASK, "consumer_ack_sequence": 4})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "task", "wait", "ws1", "task-1", "--ack"])
    assert result.exit_code == 0, result.output
    assert [item.url.path for item in captured] == [
        "/api/workspaces/ws1/tasks/task-1/wait",
        "/api/workspaces/ws1/tasks/task-1/ack",
    ]
    assert json.loads(result.stdout) == [SAMPLE_EVENT]
    assert json.loads(result.stderr) == {"acked_sequence": 4}
    assert "acked_sequence" not in result.stdout
