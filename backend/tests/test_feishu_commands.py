"""Tests for the ``feishu`` CLI command group (bind/send-card/result)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.commands import feishu as feishu_cmd
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


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDE_HUB_CONFIG_DIR", str(tmp_path))
    return tmp_path


# -- bindings ---------------------------------------------------------------


def test_bind_and_list(config_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "bind", "ops", "--chat-id", "oc_123"])
    assert result.exit_code == 0, result.output
    assert "oc_123" in result.output

    listed = runner.invoke(cli, ["feishu", "bindings"])
    assert listed.exit_code == 0
    assert "ops" in listed.output
    assert "oc_123" in listed.output


def test_bindings_empty(config_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "bindings"])
    assert result.exit_code == 0
    assert "(none)" in result.output


def test_unbind(config_dir: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["feishu", "bind", "ops", "--chat-id", "oc_123"])
    result = runner.invoke(cli, ["feishu", "unbind", "ops"])
    assert result.exit_code == 0
    assert "unbound ops" in result.output


def test_unbind_missing_errors(config_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "unbind", "ghost"])
    assert result.exit_code != 0
    assert "no binding" in result.output


# -- send-card --dry-run ----------------------------------------------------


def test_send_card_dry_run_approval() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feishu", "send-card", "--kind", "approval", "--title", "T", "--body", "B", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "approval"
    assert payload["token"]  # interactive kinds get a token
    assert "header" in payload["card"]


def test_send_card_dry_run_status_fetches_board(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/board"
        return httpx.Response(200, json={"tasks": [{"id": "t1", "title": "A", "status": "done"}]})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feishu", "send-card", "--kind", "status", "--workspace-id", "ws1", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "status"
    assert payload["token"] is None  # display kinds carry no token


def test_send_card_wait_invalid_for_display() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feishu", "send-card", "--kind", "status", "--workspace-id", "ws1", "--wait"],
    )
    assert result.exit_code != 0
    assert "--wait is only valid" in result.output


def test_send_card_status_requires_workspace_id() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["feishu", "send-card", "--kind", "status", "--dry-run"])
    assert result.exit_code != 0
    assert "--workspace-id is required" in result.output


# -- send-card (real send path, mocked sender) ------------------------------


def test_send_card_registers_then_sends(monkeypatch, config_dir: Path) -> None:
    """Interactive send registers the token before pushing the card."""
    feishu_store_calls: List[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/feishu/cards/register":
            feishu_store_calls.append(json.loads(request.content))
            return httpx.Response(200, json={"token": "x", "status": "pending"})
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)

    sent: List[tuple] = []

    def fake_send(app_id, app_secret, chat_id, card) -> str:
        sent.append((app_id, app_secret, chat_id, card))
        return "om_999"

    monkeypatch.setattr(feishu_cmd, "send_card", fake_send)
    monkeypatch.setattr(feishu_cmd, "_resolve_credentials", lambda a, b: ("app", "secret"))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "feishu",
            "send-card",
            "--kind",
            "approval",
            "--to",
            "oc_direct",
            "--title",
            "T",
            "--body",
            "B",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(feishu_store_calls) == 1
    assert feishu_store_calls[0]["chat_id"] == "oc_direct"
    assert feishu_store_calls[0]["kind"] == "approval"
    assert len(sent) == 1
    assert sent[0][2] == "oc_direct"
    assert "om_999" in result.output


def test_send_card_requires_target_when_not_dry_run(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["feishu", "send-card", "--kind", "approval", "--title", "T", "--body", "B"]
    )
    assert result.exit_code != 0
    assert "--to is required" in result.output


def test_send_card_wait_polls_until_resolved(monkeypatch, config_dir: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/feishu/cards/register":
            return httpx.Response(200, json={"status": "pending"})
        if request.url.path.startswith("/api/feishu/cards/result/"):
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(200, json={"token": "t", "status": "pending"})
            return httpx.Response(
                200, json={"token": "t", "status": "resolved", "action": "approve"}
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)
    monkeypatch.setattr(feishu_cmd, "send_card", lambda *a: "om_1")
    monkeypatch.setattr(feishu_cmd, "_resolve_credentials", lambda a, b: ("app", "secret"))
    monkeypatch.setattr(feishu_cmd.time, "sleep", lambda s: None)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "feishu",
            "send-card",
            "--kind",
            "approval",
            "--to",
            "oc_1",
            "--title",
            "T",
            "--body",
            "B",
            "--wait",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "resolved"
    assert payload["action"] == "approve"


def test_send_card_wait_timeout(monkeypatch, config_dir: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/feishu/cards/register":
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"token": "t", "status": "pending"})

    patch_get_client(monkeypatch, handler)
    monkeypatch.setattr(feishu_cmd, "send_card", lambda *a: "om_1")
    monkeypatch.setattr(feishu_cmd, "_resolve_credentials", lambda a, b: ("app", "secret"))
    monkeypatch.setattr(feishu_cmd.time, "sleep", lambda s: None)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "feishu",
            "send-card",
            "--kind",
            "approval",
            "--to",
            "oc_1",
            "--title",
            "T",
            "--body",
            "B",
            "--wait",
            "--timeout",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "timeout"


# -- result -----------------------------------------------------------------


def test_result_reads_token(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/feishu/cards/result/tok1"
        return httpx.Response(
            200, json={"token": "tok1", "status": "resolved", "action": "approve"}
        )

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "feishu", "result", "tok1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["action"] == "approve"
