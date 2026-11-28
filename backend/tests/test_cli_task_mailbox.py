"""CLI contract for ``claude-hub task list --tree | events [--wait] | ack | followup``."""

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
    "workspace_id": "ws1",
    "title": "parent",
    "status": "todo",
    "parent_task_id": None,
    "agent_type": "claude",
    "consumer_ack_sequence": 0,
}
SAMPLE_CHILD = {
    "id": "task-2",
    "workspace_id": "ws1",
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
        if request.method == "GET" and request.url.path == "/api/workspaces/tasks/task-1":
            return httpx.Response(200, json=SAMPLE_TASK)
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
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


def test_help_registers_streamlined_task_mailbox_commands() -> None:
    runner = CliRunner()
    group = runner.invoke(cli, ["task", "--help"])
    assert group.exit_code == 0, group.output
    for name in ("list", "events", "ack", "followup"):
        assert name in group.output
    for removed in ("tree", "wait", "send", "interrupt"):
        assert f"\n  {removed} " not in group.output


def test_events_ack_help_requires_task_id_first_without_resident_cursor() -> None:
    runner = CliRunner()

    events_help = runner.invoke(cli, ["task", "events", "--help"])
    ack_help = runner.invoke(cli, ["task", "ack", "--help"])
    assert events_help.exit_code == 0, events_help.output
    assert ack_help.exit_code == 0, ack_help.output
    assert "TASK_ID" in events_help.output
    assert "--wait" in events_help.output
    assert "TASK_ID SEQUENCE" in ack_help.output
    assert "resident" not in events_help.output.lower()
    assert "resident" not in ack_help.output.lower()

    missing_events_task = runner.invoke(cli, ["task", "events"])
    missing_ack_sequence = runner.invoke(cli, ["task", "ack", "task-1"])
    assert missing_events_task.exit_code == 2
    assert "Missing argument 'TASK_ID'" in missing_events_task.output
    assert missing_ack_sequence.exit_code == 2
    assert "Missing argument 'SEQUENCE'" in missing_ack_sequence.output


def test_list_tree_and_events_hit_workspace_task_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tree"):
            return httpx.Response(200, json=[SAMPLE_TASK, SAMPLE_CHILD])
        return httpx.Response(200, json=[SAMPLE_EVENT])

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    tree = runner.invoke(cli, ["--json", "task", "list", "ws1", "--tree", "--root", "task-1"])
    events = runner.invoke(
        cli,
        ["--json", "task", "events", "task-1", "--since-sequence", "2", "--subtree"],
    )
    explicit_ws = runner.invoke(
        cli,
        [
            "--json",
            "task",
            "events",
            "task-1",
            "--workspace-id",
            "ws1",
            "--since-sequence",
            "0",
        ],
    )
    assert tree.exit_code == 0, tree.output
    assert events.exit_code == 0, events.output
    assert explicit_ws.exit_code == 0, explicit_ws.output
    paths = [req.url.path for req in captured]
    assert paths == [
        "/api/workspaces/ws1/tasks/task-1/tree",
        "/api/workspaces/tasks/task-1",
        "/api/workspaces/ws1/tasks/task-1/events",
        "/api/workspaces/ws1/tasks/task-1/events",
    ]
    assert _query(captured[2])["since_sequence"] == ["2"]
    assert _query(captured[2])["subtree"] == ["true"]
    for request in captured:
        assert "/api/agent-tree" not in request.url.path


def test_followup_uses_call_id_and_resolves_workspace_o1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            "task-1",
            "--message",
            "fix it",
            "--call-id",
            "followup-1",
        ],
    )
    minted = runner.invoke(
        cli,
        ["--json", "task", "followup", "task-1", "--workspace-id", "ws1", "--message", "x"],
    )
    assert followup.exit_code == 0, followup.output
    assert minted.exit_code == 0, minted.output
    assert [request.url.path for request in captured] == [
        "/api/workspaces/tasks/task-1",
        "/api/workspaces/ws1/tasks/task-1/followup",
        "/api/workspaces/ws1/tasks/task-1/followup",
    ]
    assert _json(captured[1]) == {"message": "fix it", "call_id": "followup-1"}
    minted_id = _json(captured[2])["call_id"]
    assert UUID_RE.match(minted_id)
    assert "call_id=followup-1" in followup.stderr
    assert f"call_id={minted_id}" in minted.stderr
    for request in captured:
        assert "/sessions/" not in request.url.path
        assert "/api/agent-tree" not in request.url.path


def test_events_wait_ack_flushes_events_then_acks_max_sequence(
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
        ["--json", "task", "events", "task-1", "--wait", "--subtree", "--ack"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == events
    assert [item.url.path for item in captured] == [
        "/api/workspaces/tasks/task-1",
        "/api/workspaces/ws1/tasks/task-1/wait",
        "/api/workspaces/ws1/tasks/task-1/ack",
    ]
    assert _query(captured[1])["subtree"] == ["true"]
    assert json.loads(result.stderr) == {"acked_sequence": 8}
    assert result.stdout.strip().startswith("[")
    assert "acked_sequence" not in result.stdout


def test_empty_events_wait_ack_does_not_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/wait")
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli, ["--json", "task", "events", "task-1", "--workspace-id", "ws1", "--wait", "--ack"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "acked_sequence" not in result.stderr
    assert [item.url.path for item in captured] == ["/api/workspaces/ws1/tasks/task-1/wait"]


def test_ack_task_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**SAMPLE_TASK, "consumer_ack_sequence": 7})

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    task_ack = runner.invoke(cli, ["--json", "task", "ack", "task-1", "7"])
    assert task_ack.exit_code == 0, task_ack.output
    assert [item.url.path for item in captured] == [
        "/api/workspaces/tasks/task-1",
        "/api/workspaces/ws1/tasks/task-1/ack",
    ]
    assert _json(captured[1]) == {"sequence": 7}
    for request in captured:
        assert "/api/agent-tree" not in request.url.path


def test_events_wait_sends_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "task",
            "events",
            "task-1",
            "--workspace-id",
            "ws1",
            "--wait",
            "--timeout-seconds",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/wait"
    assert _query(captured[0])["timeout_seconds"] == ["7.0"]
    assert json.loads(result.stdout) == []
    assert "/api/agent-tree" not in captured[0].url.path


def test_events_wait_sends_fractional_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "task",
            "events",
            "task-1",
            "--workspace-id",
            "ws1",
            "--wait",
            "--timeout-seconds",
            "0.2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0].url.path == "/api/workspaces/ws1/tasks/task-1/wait"
    assert _query(captured[0])["timeout_seconds"] == ["0.2"]


def test_events_wait_ack_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[SAMPLE_EVENT])
        return httpx.Response(200, json={**SAMPLE_TASK, "consumer_ack_sequence": 4})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli, ["--json", "task", "events", "task-1", "--workspace-id", "ws1", "--wait", "--ack"]
    )
    assert result.exit_code == 0, result.output
    assert [item.url.path for item in captured] == [
        "/api/workspaces/ws1/tasks/task-1/wait",
        "/api/workspaces/ws1/tasks/task-1/ack",
    ]
    assert json.loads(result.stdout) == [SAMPLE_EVENT]
    assert json.loads(result.stderr) == {"acked_sequence": 4}
    assert "acked_sequence" not in result.stdout
