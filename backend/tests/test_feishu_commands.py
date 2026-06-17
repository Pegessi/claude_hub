"""Tests for the ``feishu`` CLI command group (build-card / parse-action)."""

from __future__ import annotations

import json
from typing import Callable, List

import httpx
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.feishu_cards import ACTION_KEY, TOKEN_KEY
from claude_hub.cli.main import cli


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HubClient:
    transport = httpx.MockTransport(handler)
    return HubClient(base_url="http://testserver", transport=transport)


def patch_get_client(monkeypatch, handler) -> List[httpx.Request]:
    captured: List[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    monkeypatch.setattr(cli_main, "get_client", lambda ctx: make_client(recording_handler))
    return captured


# -- build-card -------------------------------------------------------------


def test_build_card_approval_has_token() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["feishu", "build-card", "--kind", "approval", "--title", "T", "--body", "B"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "approval"
    assert payload["token"]  # interactive kinds get a token
    assert "header" in payload["card"]
    # The token is embedded in a control's value per the card contract.
    assert payload["token"] in result.output


def test_build_card_accepts_explicit_token() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feishu", "build-card", "--kind", "approval", "--title", "T", "--token", "fixedtok"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token"] == "fixedtok"


def test_build_card_needs_input_field_name() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feishu", "build-card", "--kind", "needs_input", "--title", "T", "--field-name", "note"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "needs_input"
    assert "note" in result.output  # the named input field


def test_build_card_status_fetches_board(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/board"
        return httpx.Response(200, json={"tasks": [{"id": "t1", "title": "A", "status": "done"}]})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["feishu", "build-card", "--kind", "status", "--workspace-id", "ws1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "status"
    assert payload["token"] is None  # display kinds carry no token


def test_build_card_status_requires_workspace_id() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "status"])
    assert result.exit_code != 0
    assert "--workspace-id is required" in result.output


def test_build_card_task_requires_ids() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "task", "--workspace-id", "ws1"])
    assert result.exit_code != 0
    assert "--task-id" in result.output


# -- parse-action -----------------------------------------------------------


def _callback(token: str = "tok1", action: str = "approve") -> dict:
    return {
        "event": {
            "action": {"value": {TOKEN_KEY: token, ACTION_KEY: action}},
            "operator": {"open_id": "ou_42"},
            "context": {"open_chat_id": "oc_9"},
        }
    }


def test_parse_action_from_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "parse-action", json.dumps(_callback())])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token"] == "tok1"
    assert payload["action"] == "approve"
    assert payload["operator_id"] == "ou_42"
    assert payload["chat_id"] == "oc_9"


def test_parse_action_from_stdin() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "parse-action"], input=json.dumps(_callback("t2")))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["token"] == "t2"


def test_parse_action_foreign_card_exits_1() -> None:
    runner = CliRunner()
    foreign = {"event": {"action": {"value": {"something": "else"}}}}
    result = runner.invoke(cli, ["feishu", "parse-action", json.dumps(foreign)])
    assert result.exit_code == 1
    assert result.output.strip() == "null"


def test_parse_action_invalid_json_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "parse-action", "{not json"])
    assert result.exit_code != 0
    assert "invalid JSON" in result.output


def test_parse_action_empty_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "parse-action"], input="")
    assert result.exit_code != 0
    assert "no callback payload" in result.output


# -- group wiring -----------------------------------------------------------


def test_feishu_group_has_only_two_commands() -> None:
    from claude_hub.cli.commands.feishu import feishu

    assert set(feishu.commands) == {"build-card", "parse-action"}
