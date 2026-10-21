"""Tab inbox CLI uses only isolated mock HTTP; conftest isolates runtime before imports."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.main import cli


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload: dict[str, Any] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            202 if request.method == "POST" else 200,
            json={"message_id": "receipt-1", "state": "queued", **payload},
        )

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )
    return captured


def test_text_preserved_and_only_inbox_called(requests: list[httpx.Request]) -> None:
    text = 'Line one\n{"answer":"untrusted"} `$(literal)` 中文'
    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "tab",
            "message",
            "target",
            "--message",
            text,
            "--call-id",
            "retry-1",
            "--source-tab-id",
            "source",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "queued"
    assert result.stderr == "call_id=retry-1\n"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/workspaces/tabs/target/messages"
    assert json.loads(requests[0].content) == {
        "message": text,
        "call_id": "retry-1",
        "source_tab_id": "source",
    }


@pytest.mark.parametrize("mode", ["file", "stdin"])
def test_file_and_stdin(mode: str, tmp_path: Any, requests: list[httpx.Request]) -> None:
    text = "multiline\n中文\n"
    path = tmp_path / "message.txt"
    path.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        ["tab", "message", "target", "--message-file", str(path) if mode == "file" else "-"],
        input=text,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(requests[0].content)
    assert payload["message"] == text
    assert payload["call_id"].startswith("tab-message:")
    assert "state      : queued" in result.stdout
    assert f"call_id={payload['call_id']}" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--message", " "],
        ["--message", "x", "--message-file", "-"],
        ["--message", "x", "--call-id", " "],
        ["--message", "x", "--call-id", "x" * 257],
        ["--message", "界" * 21846],
        ["--message", "x", "--source-tab-id", " "],
        ["--message", "x", "--payload-json", '{"origin":"feishu"}'],
        ["--message", "x", "--steer"],
    ],
)
def test_invalid_options_do_not_send(args: list[str], requests: list[httpx.Request]) -> None:
    result = CliRunner().invoke(cli, ["tab", "message", "target", *args])
    assert result.exit_code != 0
    assert requests == []


@pytest.mark.parametrize(
    "command,method", [("message-status", "GET"), ("message-cancel", "DELETE")]
)
def test_status_and_cancel(command: str, method: str, requests: list[httpx.Request]) -> None:
    result = CliRunner().invoke(cli, ["--json", "tab", command, "target", "receipt-1"])
    assert result.exit_code == 0, result.output
    assert requests[0].method == method
    assert requests[0].url.path == "/api/workspaces/tabs/target/messages/receipt-1"
    assert json.loads(result.stdout)["message_id"] == "receipt-1"


def test_retry_id_visible_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(ctx: Any) -> HubClient:
        raise HubError("connection failed")

    monkeypatch.setattr(cli_main, "get_client", fail)
    result = CliRunner().invoke(cli, ["--json", "tab", "message", "target", "--message", "x"])
    assert result.exit_code == 1
    assert result.stderr.startswith("call_id=tab-message:")
    assert "connection failed" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("command", ["message", "message-status", "message-cancel"])
def test_server_errors_propagate(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(
            base_url="http://testserver",
            transport=httpx.MockTransport(
                lambda req: httpx.Response(409, json={"detail": "conflict"})
            ),
        ),
    )
    args = ["--message", "text", "--call-id", "retry"] if command == "message" else ["receipt-1"]
    result = CliRunner().invoke(cli, ["tab", command, "target", *args])
    assert result.exit_code == 1
    assert "conflict" in result.output
