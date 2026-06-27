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


def patch_get_client(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response]
) -> List[httpx.Request]:
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


def test_build_card_new_display_kinds(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "ws1", "name": "Alpha"}])
        if path == "/api/workspaces/ws1/board":
            return httpx.Response(
                200,
                json={
                    "workspace": {"name": "Alpha"},
                    "tasks": [
                        {"id": "t1", "title": "Fix it", "status": "working", "session_id": "s1"}
                    ],
                    "sessions": [
                        {
                            "id": "s1",
                            "role": "orchestrator",
                            "agent_type": "claude",
                            "runtime_status": "working",
                            "tab_id": "tab9",
                        }
                    ],
                },
            )
        if path == "/api/workspaces/ws1/tasks/t1/reports":
            return httpx.Response(
                200,
                json=[
                    {"state": "working", "message": "old", "created_at": "2026-01-01T00:00:00"},
                    {"state": "completed", "message": "new", "created_at": "2026-01-02T00:00:00"},
                ],
            )
        if path == "/api/terminal/history/tab9":
            return httpx.Response(200, json={"history": "line1\nline2"})
        if path == "/api/workspaces/ws1/lessons":
            return httpx.Response(200, json=[{"id": "l1", "title": "Use locks"}])
        if path == "/api/tabs":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "tab9",
                        "name": "Worker",
                        "agent_type": "claude",
                        "target": "local",
                        "cwd": "/repo",
                        "workspace_name": "Alpha",
                    }
                ],
            )
        if path == "/api/tabs/status":
            return httpx.Response(
                200,
                json=[
                    {
                        "tab_id": "tab9",
                        "tab_name": "Worker",
                        "agent_type": "claude",
                        "status": "working",
                        "status_text": "Running",
                        "detail": "editing",
                    }
                ],
            )
        if path == "/api/system/network-access":
            return httpx.Response(
                200,
                json={
                    "hostname": "host1",
                    "addresses": [{"address": "192.168.1.20", "label": "en0"}],
                },
            )
        if path == "/api/filesystem/list":
            assert request.url.params.get("path") == "/repo"
            return httpx.Response(
                200,
                json={
                    "current_path": "/repo",
                    "parent_path": "/",
                    "items": [{"name": "backend", "path": "/repo/backend", "is_dir": True}],
                },
            )
        if path == "/api/remote/profiles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "devbox",
                        "name": "Dev Box",
                        "ssh_host": "dev.example",
                        "user": "me",
                        "port": 2222,
                        "default_cwd": "~/repo",
                    }
                ],
            )
        if path == "/api/remote/filesystem/list":
            assert request.url.params.get("profile_id") == "devbox"
            assert request.url.params.get("path") == "~/repo"
            return httpx.Response(
                200,
                json={
                    "current_path": "/home/me/repo",
                    "parent_path": "/home/me",
                    "items": [
                        {"name": "README.md", "path": "/home/me/repo/README.md", "is_dir": False}
                    ],
                },
            )
        raise AssertionError(f"unexpected path {path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    cases = [
        (["--kind", "workspaces"], "Alpha"),
        (["--kind", "overview", "--workspace-id", "ws1"], "Active tasks"),
        (["--kind", "agents", "--workspace-id", "ws1"], "Orchestrator"),
        (["--kind", "task_detail", "--workspace-id", "ws1", "--task-id", "t1"], "new"),
        (["--kind", "reports", "--workspace-id", "ws1", "--task-id", "t1"], "completed"),
        (["--kind", "terminal", "--tab-id", "tab9"], "line2"),
        (["--kind", "lessons", "--workspace-id", "ws1"], "Use locks"),
        (["--kind", "tabs"], "Worker"),
        (["--kind", "tab_status"], "editing"),
        (["--kind", "network"], "192.168.1.20"),
        (["--kind", "filesystem", "--path", "/repo"], "backend"),
        (["--kind", "remote_profiles"], "dev.example"),
        (
            ["--kind", "remote_filesystem", "--remote-profile-id", "devbox", "--path", "~/repo"],
            "README.md",
        ),
        (["--kind", "result", "--title", "API Result", "--body", "ok"], "ok"),
        (["--kind", "action_catalog"], "parse-action"),
    ]
    for args, expected in cases:
        result = runner.invoke(cli, ["feishu", "build-card", *args])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        if "--kind" in args and args[args.index("--kind") + 1] == "task_detail":
            assert payload["token"]
            assert payload["token"] in result.output
        else:
            assert payload["token"] is None
        assert expected in result.output


def test_build_card_new_display_kinds_validate_required_ids() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "overview"])
    assert result.exit_code != 0
    assert "--workspace-id is required for kind=overview" in result.output

    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "task_detail"])
    assert result.exit_code != 0
    assert "--task-id is required for kind=task_detail" in result.output

    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "terminal"])
    assert result.exit_code != 0
    assert "--tab-id is required for kind=terminal" in result.output

    result = runner.invoke(cli, ["feishu", "build-card", "--kind", "remote_filesystem"])
    assert result.exit_code != 0
    assert "--remote-profile-id is required for kind=remote_filesystem" in result.output


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
    assert payload["value"][TOKEN_KEY] == "tok1"
    assert payload["operator_id"] == "ou_42"
    assert payload["chat_id"] == "oc_9"


def test_parse_action_includes_cli_command_when_action_is_mappable() -> None:
    runner = CliRunner()
    callback = _callback("tok1", "terminal")
    callback["event"]["action"]["value"]["tab_id"] = "tab9"
    result = runner.invoke(cli, ["feishu", "parse-action", json.dumps(callback)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cli_command"] == "claude-hub feishu build-card --kind terminal --tab-id tab9"


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
