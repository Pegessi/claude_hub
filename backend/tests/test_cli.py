"""Tests for the Claude Hub CLI."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.main import cli


def make_client(handler, **kwargs) -> HubClient:
    transport = httpx.MockTransport(handler)
    return HubClient(base_url="http://testserver", transport=transport, **kwargs)


def patch_get_client(monkeypatch, handler, **kwargs) -> List[httpx.Request]:
    """Make get_client return a MockTransport-backed client; record requests."""
    captured: List[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def fake_get_client(ctx) -> HubClient:
        return make_client(recording_handler)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    return captured


def test_help_lists_groups():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for group in ("workspace", "task", "agent", "session", "lessons"):
        assert group in result.output


def test_workspace_list_json(monkeypatch):
    sample = [
        {
            "id": "ws1",
            "name": "Demo",
            "path": "/repo",
            "default_branch": "main",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces"
        return httpx.Response(200, json=sample)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "workspace", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == sample
    assert captured[0].url.path == "/api/workspaces"


def test_task_create_body(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "ws1",
            "--title",
            "T",
            "--prompt",
            "do it",
            "--review-profile",
            "code",
            "--review-profile",
            "ui",
        ],
    )
    assert result.exit_code == 0, result.output
    body = bodies[0]
    assert set(body.keys()) == {
        "title",
        "prompt",
        "agent_type",
        "task_mode",
        "execution_complexity",
        "review_profiles",
    }
    assert body["review_profiles"] == ["code", "ui"]
    assert body["agent_type"] == "codex"
    assert body["task_mode"] == "reviewed"
    assert body["execution_complexity"] == "auto"


def test_task_start_omits_none(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "start", "t1", "--agent-type", "claude"])
    assert result.exit_code == 0, result.output
    assert bodies[0] == {"agent_type": "claude"}


def test_session_send_204(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/sessions/s1/send"
        return httpx.Response(204)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["session", "send", "s1", "--message", "hi"])
    assert result.exit_code == 0, result.output
    assert "sent" in result.output


def test_error_nonzero_exit(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Task not found"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "abort", "missing", "--reason", "x"])
    assert result.exit_code != 0
    assert "Task not found" in result.output


def test_task_send_resolves_session(monkeypatch):
    board = {
        "tasks": [
            {"id": "t1", "status": "working", "session_id": "s9"},
            {"id": "t2", "status": "queued", "session_id": None},
        ]
    }
    paths: List[str] = []
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/board"):
            return httpx.Response(200, json=board)
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "send", "ws1", "t1", "--message", "hello"])
    assert result.exit_code == 0, result.output
    assert "/api/workspaces/ws1/board" in paths
    assert "/api/workspaces/sessions/s9/send" in paths
    assert bodies[0] == {"message": "hello", "attachments": []}
    assert "s9" in result.output


def test_task_send_no_session_errors(monkeypatch):
    board = {"tasks": [{"id": "t2", "status": "queued", "session_id": None}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/board")  # must never reach send
        return httpx.Response(200, json=board)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "send", "ws1", "t2", "--message", "hi"])
    assert result.exit_code != 0
    assert "no active session" in result.output


def test_task_send_missing_task_errors(monkeypatch):
    board = {"tasks": [{"id": "other", "status": "working", "session_id": "s1"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=board)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "send", "ws1", "nope", "--message", "hi"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_invalid_choice_exits(monkeypatch):
    called = {"hit": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "ws1",
            "--title",
            "T",
            "--prompt",
            "p",
            "--agent-type",
            "bogus",
        ],
    )
    assert result.exit_code == 2
    assert called["hit"] is False


def test_config_precedence(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[default]\nbase_url = "http://from-file:9999"\n', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HUB_URL", "http://from-env:8888")
    monkeypatch.delenv("CLAUDE_HUB_TOKEN", raising=False)

    from claude_hub.cli.config import resolve_settings

    # env beats config file
    s = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    assert s.base_url == "http://from-env:8888"

    # flag beats env
    s2 = resolve_settings(
        base_url="http://from-flag:7777",
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    assert s2.base_url == "http://from-flag:7777"

    # config file beats default when env absent
    monkeypatch.delenv("CLAUDE_HUB_URL", raising=False)
    s3 = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    assert s3.base_url == "http://from-file:9999"


def test_client_token_cookie():
    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, json=[])

    client = make_client(handler, token="secret")
    client.list_workspaces()
    assert "claude_hub_session=secret" in seen["cookie"]


def test_client_raises_huberror_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = make_client(handler)
    with pytest.raises(HubError) as exc:
        client.get_board("ws1")
    assert exc.value.status == 404
    assert "nope" in str(exc.value)


def test_table_handles_non_dict_rows(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "a"}, "junk", 5])

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "list"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "AttributeError" not in result.output
    assert "a" in result.output
    assert "junk" in result.output


def test_board_mixed_rows_no_partial_crash(monkeypatch):
    board = {
        "workspace": {"id": "ws1", "name": "Demo"},
        "tasks": ["x", {"id": "t1", "title": "T", "status": "working"}],
        "sessions": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=board)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "board", "ws1"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "AttributeError" not in result.output
    assert "t1" in result.output


def test_verbose_logs_real_url(monkeypatch):
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    def fake_get_client(ctx):
        settings = ctx.obj
        return make_client(handler, verbose=settings.verbose)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["-v", "--base-url", "http://testserver/api/v2", "workspace", "list"]
    )
    assert result.exit_code == 0, result.output
    real_url = str(captured[0].url)
    logged = result.output + getattr(result, "stderr", "")
    assert "//api" not in real_url  # no double slash in the real request
    assert real_url in logged  # verbose line matches the actual URL


def test_truncate_collapses_tabs():
    from claude_hub.cli.output import render_table, truncate

    assert "\t" not in truncate("a\tb")
    assert truncate("a\tb") == "a b"
    assert truncate("a\n\tb  c") == "a b c"

    table = render_table([{"x": "a\tb", "y": "z"}], ["x", "y"])
    assert "\t" not in table
    # Columns stay aligned: every line has the same width.
    lines = table.splitlines()
    assert len({len(line) for line in lines}) == 1


def test_truncate_strips_ansi():
    from claude_hub.cli.output import truncate

    assert truncate("\x1b[31mred\x1b[0m") == "red"


def test_cookie_empty_key_skipped():
    from claude_hub.cli.client import _parse_cookie_string

    assert _parse_cookie_string("=v") == {}
    assert _parse_cookie_string("k=v; =x; a=b") == {"k": "v", "a": "b"}
    assert _parse_cookie_string("garbage") == {}
    assert _parse_cookie_string("a;b;c") == {}


def test_config_ignores_non_string_base_url(tmp_path, monkeypatch):
    from claude_hub.cli.config import DEFAULT_BASE_URL, resolve_settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("[default]\nbase_url = 8173\ntoken = 42\n", encoding="utf-8")
    monkeypatch.delenv("CLAUDE_HUB_URL", raising=False)
    monkeypatch.delenv("CLAUDE_HUB_TOKEN", raising=False)

    s = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    # Non-string values are ignored; fall through to default / None.
    assert s.base_url == DEFAULT_BASE_URL
    assert s.token is None
