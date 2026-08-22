"""Bounded MockTransport + CliRunner tests for ``claude-hub agent-tree``."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs

import click
import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.commands import agent_tree as agent_tree_cmd
from claude_hub.cli.main import cli

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


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


SAMPLE_RUN = {
    "id": "run-root",
    "parent_id": None,
    "executor_kind": "resident_root",
    "status": "running",
    "title": "resident",
    "ack_sequence": 0,
    "workspace_id": "ws1",
}
SAMPLE_CHILD = {
    "id": "run-child",
    "parent_id": "run-root",
    "executor_kind": "managed_task",
    "status": "running",
    "title": "child",
    "ack_sequence": 0,
    "workspace_id": "ws1",
    "context_ref": "task-1",
}
SAMPLE_EVENT = {
    "sequence": 4,
    "type": "progress",
    "author": "run-child",
    "recipient": "run-root",
    "call_id": "report:r1",
}


def test_help_registers_agent_tree_group() -> None:
    runner = CliRunner()
    root = runner.invoke(cli, ["--help"])
    assert root.exit_code == 0, root.output
    assert "agent-tree" in root.output
    group = runner.invoke(cli, ["agent-tree", "--help"])
    assert group.exit_code == 0, group.output
    for name in (
        "roots",
        "runs",
        "events",
        "spawn",
        "send",
        "followup",
        "wait",
        "ack",
        "interrupt",
    ):
        assert name in group.output
    assert "native_subagent" not in group.output
    assert "external_job" not in group.output


def test_ack_and_wait_reject_call_id() -> None:
    runner = CliRunner()
    for args in (
        ["agent-tree", "ack", "ws1", "run-root", "1", "--call-id", "x"],
        ["agent-tree", "wait", "ws1", "run-root", "--call-id", "x"],
        ["agent-tree", "roots", "ws1", "--call-id", "x"],
        ["agent-tree", "runs", "ws1", "--call-id", "x"],
        ["agent-tree", "events", "run-root", "--call-id", "x"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code != 0
        assert "No such option: --call-id" in result.output


def test_roots_filters_resident_root(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/agent-tree/runs"
        assert _query(request) == {"workspace_id": ["ws1"]}
        return httpx.Response(200, json=[SAMPLE_RUN, SAMPLE_CHILD])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "roots", "ws1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [SAMPLE_RUN]
    assert len(captured) == 1


def test_runs_and_events_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-tree/runs":
            query = _query(request)
            assert query["workspace_id"] == ["ws1"]
            assert query["root_id"] == ["run-root"]
            assert query["status"] == ["running"]
            return httpx.Response(200, json=[SAMPLE_RUN, SAMPLE_CHILD])
        assert request.url.path == "/api/agent-tree/runs/run-root/events"
        query = _query(request)
        assert query["since_sequence"] == ["2"]
        assert query["subtree"] == ["false"]
        return httpx.Response(200, json=[SAMPLE_EVENT])

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    runs = runner.invoke(
        cli,
        ["--json", "agent-tree", "runs", "ws1", "--root-id", "run-root", "--status", "running"],
    )
    assert runs.exit_code == 0, runs.output
    assert json.loads(runs.output) == [SAMPLE_RUN, SAMPLE_CHILD]
    events = runner.invoke(
        cli,
        ["--json", "agent-tree", "events", "run-root", "--since-sequence", "2", "--no-subtree"],
    )
    assert events.exit_code == 0, events.output
    assert json.loads(events.output) == [SAMPLE_EVENT]


def test_spawn_config_driven_body_and_stderr_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/agent-tree/spawn"
        return httpx.Response(200, json=SAMPLE_CHILD)

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "spawn",
            "ws1",
            "run-root",
            "--message",
            "investigate",
            "--title",
            "child",
            "--agent-type",
            "claude",
            "--model",
            "sonnet",
            "--target",
            "local",
            "--cwd",
            "/repo",
            "--env",
            "FOO=bar",
            "--solo-mode",
            "--call-id",
            "spawn-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == SAMPLE_CHILD
    assert "call_id=spawn-1" in result.stderr
    assert _json(captured[0]) == {
        "workspace_id": "ws1",
        "parent_id": "run-root",
        "executor_kind": "managed_task",
        "initial_message": "investigate",
        "title": "child",
        "call_id": "spawn-1",
        "executor_config": {
            "agent_type": "claude",
            "model": "sonnet",
            "target": "local",
            "cwd": "/repo",
            "env": {"FOO": "bar"},
            "solo_mode": True,
        },
    }


def test_spawn_session_only_omits_executor_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_CHILD)

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "spawn",
            "ws1",
            "run-root",
            "--message",
            "pin",
            "--session-id",
            "sess-1",
            "--call-id",
            "spawn-pin-1",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _json(captured[0])
    assert body["session_id"] == "sess-1"
    assert "executor_config" not in body


def test_spawn_session_plus_config_sends_both(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_CHILD)

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "spawn",
            "ws1",
            "run-root",
            "--message",
            "pin+cfg",
            "--session-id",
            "sess-1",
            "--agent-type",
            "codex",
            "--call-id",
            "spawn-pin-cfg-1",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _json(captured[0])
    assert body["session_id"] == "sess-1"
    assert body["executor_config"] == {"agent_type": "codex"}


def test_spawn_generates_unique_call_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_CHILD)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    first = runner.invoke(
        cli, ["--json", "agent-tree", "spawn", "ws1", "run-root", "--message", "a"]
    )
    second = runner.invoke(
        cli, ["--json", "agent-tree", "spawn", "ws1", "run-root", "--message", "b"]
    )
    assert first.exit_code == 0 and second.exit_code == 0
    id_a = _json(captured[0])["call_id"]
    id_b = _json(captured[1])["call_id"]
    assert UUID_RE.match(id_a)
    assert UUID_RE.match(id_b)
    assert id_a != id_b
    assert f"call_id={id_a}" in first.stderr
    assert f"call_id={id_b}" in second.stderr


def test_spawn_cursor_model_rejected_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=SAMPLE_CHILD))
    result = CliRunner().invoke(
        cli,
        [
            "agent-tree",
            "spawn",
            "ws1",
            "run-root",
            "--message",
            "x",
            "--agent-type",
            "cursor",
            "--model",
            "gpt",
        ],
    )
    assert result.exit_code == 1
    assert f"Error: {agent_tree_cmd.CURSOR_MODEL_ERROR}" in result.output
    assert captured == []


def test_send_followup_interrupt_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/send") or request.url.path.endswith("/followup"):
            return httpx.Response(200, json=SAMPLE_EVENT)
        return httpx.Response(200, json=SAMPLE_CHILD)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    send = runner.invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "send",
            "ws1",
            "run-root",
            "run-child",
            "--message",
            "note",
            "--call-id",
            "send-1",
        ],
    )
    followup = runner.invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "followup",
            "ws1",
            "run-root",
            "run-child",
            "--message",
            "fix it",
            "--call-id",
            "followup-1",
        ],
    )
    interrupt = runner.invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "interrupt",
            "ws1",
            "run-child",
            "--reason",
            "superseded",
            "--call-id",
            "interrupt-1",
        ],
    )
    assert send.exit_code == 0 and followup.exit_code == 0 and interrupt.exit_code == 0
    assert captured[0].url.path == "/api/agent-tree/send"
    assert _json(captured[0]) == {
        "workspace_id": "ws1",
        "author_id": "run-root",
        "recipient_id": "run-child",
        "message": "note",
        "call_id": "send-1",
    }
    assert captured[1].url.path == "/api/agent-tree/followup"
    assert _json(captured[1])["call_id"] == "followup-1"
    assert captured[2].url.path == "/api/agent-tree/interrupt"
    assert _json(captured[2]) == {
        "workspace_id": "ws1",
        "run_id": "run-child",
        "call_id": "interrupt-1",
        "reason": "superseded",
    }
    assert "call_id=send-1" in send.stderr
    assert "call_id=followup-1" in followup.stderr
    assert "call_id=interrupt-1" in interrupt.stderr


def test_ack_is_query_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/agent-tree/ack"
        assert request.content in (b"", b"null")
        assert _query(request) == {
            "workspace_id": ["ws1"],
            "run_id": ["run-root"],
            "sequence": ["7"],
        }
        return httpx.Response(200, json={**SAMPLE_RUN, "ack_sequence": 7})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "ack", "ws1", "run-root", "7"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ack_sequence"] == 7
    assert len(captured) == 1


def test_wait_without_ack_does_not_post_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent-tree/wait"
        assert _json(request) == {
            "workspace_id": "ws1",
            "recipient_id": "run-root",
            "since_sequence": 3,
            "subtree": True,
            "timeout_seconds": 5.0,
        }
        return httpx.Response(200, json=[SAMPLE_EVENT])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "agent-tree",
            "wait",
            "ws1",
            "run-root",
            "--since-sequence",
            "3",
            "--timeout-seconds",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [SAMPLE_EVENT]
    assert [item.url.path for item in captured] == ["/api/agent-tree/wait"]


def test_empty_wait_ack_does_not_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent-tree/wait"
        return httpx.Response(200, json=[])

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "wait", "ws1", "run-root", "--ack"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "acked_sequence" not in result.stderr
    assert [item.url.path for item in captured] == ["/api/agent-tree/wait"]


def test_wait_ack_renders_then_acks_max_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {**SAMPLE_EVENT, "sequence": 3, "call_id": "a"},
        {**SAMPLE_EVENT, "sequence": 8, "call_id": "b"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-tree/wait":
            return httpx.Response(200, json=events)
        assert request.url.path == "/api/agent-tree/ack"
        assert _query(request)["sequence"] == ["8"]
        return httpx.Response(200, json={**SAMPLE_RUN, "ack_sequence": 8})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "wait", "ws1", "run-root", "--ack"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == events
    assert [item.url.path for item in captured] == [
        "/api/agent-tree/wait",
        "/api/agent-tree/ack",
    ]
    assert json.loads(result.stderr) == {"acked_sequence": 8}


def test_wait_ack_human_confirmation_after_table(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[SAMPLE_EVENT])
        return httpx.Response(200, json={**SAMPLE_RUN, "ack_sequence": 4})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["agent-tree", "wait", "ws1", "run-root", "--ack"])
    assert result.exit_code == 0, result.output
    assert "sequence" in result.output
    assert result.output.strip().endswith("acked sequence 4")


def test_wait_ack_failure_keeps_events_and_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[SAMPLE_EVENT])
        return httpx.Response(400, json={"detail": "ACK sequence 4 is behind current ack cursor 5"})

    captured = patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "wait", "ws1", "run-root", "--ack"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == [SAMPLE_EVENT]
    assert agent_tree_cmd.ACK_FAILED_NOTE in result.stderr
    assert "Error: HTTP 400: ACK sequence 4 is behind current ack cursor 5" in result.stderr
    assert "acked_sequence" not in result.stderr
    assert [item.url.path for item in captured] == [
        "/api/agent-tree/wait",
        "/api/agent-tree/ack",
    ]


def test_wait_render_failure_does_not_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[SAMPLE_EVENT])
        raise AssertionError("ACK must not run after render failure")

    captured = patch_get_client(monkeypatch, handler)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("render failed")

    monkeypatch.setattr(agent_tree_cmd, "_emit_events", boom)
    result = CliRunner().invoke(cli, ["--json", "agent-tree", "wait", "ws1", "run-root", "--ack"])
    assert result.exit_code != 0
    assert [item.url.path for item in captured] == ["/api/agent-tree/wait"]


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "call_id 'x' already used"),
        (403, "Session s1 does not own run run-root"),
        (404, "Run missing not found"),
        (409, "report fingerprint conflict"),
        (422, "native_subagent is not available"),
        (429, "spawn quota exceeded"),
    ],
)
def test_http_status_detail_click_output(
    monkeypatch: pytest.MonkeyPatch, status: int, detail: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": detail})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["agent-tree", "runs", "ws1"])
    assert result.exit_code == 1
    assert f"Error: HTTP {status}: {detail}" in result.output


def test_transport_error_keeps_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_client(ctx: click.Context) -> HubClient:
        class Broken(HubClient):
            def list_agent_tree_runs(self, *args: Any, **kwargs: Any) -> Any:
                raise HubError("connection refused")

        return Broken(base_url="http://testserver")

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    result = CliRunner().invoke(cli, ["agent-tree", "runs", "ws1"])
    assert result.exit_code == 1
    assert result.output == "Error: connection refused\n"


def test_call_id_survives_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "call_id 'spawn-1' already used"})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "agent-tree",
            "spawn",
            "ws1",
            "run-root",
            "--message",
            "x",
            "--call-id",
            "spawn-1",
        ],
    )
    assert result.exit_code == 1
    assert "call_id=spawn-1" in result.stderr
    assert "Error: HTTP 400: call_id 'spawn-1' already used" in result.stderr


def test_wait_timeout_uses_build_request_extension_only() -> None:
    """wait raises only that request's timeout; other calls keep 30s default."""
    assert "timeout" not in inspect.signature(httpx.Client.send).parameters

    seen: List[httpx.Request] = []
    send_kwargs: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[SAMPLE_RUN])

    with make_client(handler) as client:
        original_send = client._client.send

        def spy(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            send_kwargs.append(kwargs)
            return original_send(request, **kwargs)

        client._client.send = spy  # type: ignore[method-assign]
        assert float(client._client.timeout.read) == 30.0
        client.list_agent_tree_runs("ws1")
        client.wait_agent_events(
            {
                "workspace_id": "ws1",
                "recipient_id": "run-root",
                "since_sequence": 0,
                "subtree": True,
                "timeout_seconds": 7,
            }
        )
        client.list_agent_tree_runs("ws1")
        assert float(client._client.timeout.read) == 30.0

    assert [item.url.path for item in seen] == [
        "/api/agent-tree/runs",
        "/api/agent-tree/wait",
        "/api/agent-tree/runs",
    ]
    assert seen[0].extensions["timeout"]["read"] == 30.0
    assert seen[1].extensions["timeout"]["read"] == 12.0
    assert seen[2].extensions["timeout"]["read"] == 30.0
    assert all("timeout" not in kwargs for kwargs in send_kwargs)
