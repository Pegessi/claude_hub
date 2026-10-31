"""`claude-hub agent run` and companions against scripted mock HTTP only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.commands import subagents as subagent_commands
from claude_hub.cli.main import cli

Handler = Callable[[httpx.Request], httpx.Response]


def _turn(status: str, **extra: Any) -> Dict[str, Any]:
    return {
        "subagent_id": "sa-1",
        "tab_id": "tab-sub",
        "turn_id": "turn-1",
        "status": status,
        "final_text": None,
        "error": None,
        "started_at": "2026-10-24T00:00:00Z",
        "completed_at": None,
        "keep": False,
        "closed": False,
        **extra,
    }


class Hub:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.create_failures = 0
        self.create_interrupts = 0
        self.poll_results: List[Any] = []
        self.cancel_result: Dict[str, Any] = {"turn": _turn("cancelled", error="Cancelled")}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path in {"/api/subagents", "/api/subagents/sa-1/turns"}:
            if self.create_failures:
                self.create_failures -= 1
                raise httpx.ConnectError("connection refused", request=request)
            if self.create_interrupts:
                self.create_interrupts -= 1
                raise KeyboardInterrupt()
            return httpx.Response(201, json=_turn("running"))
        if request.method == "GET" and path.startswith("/api/subagents/sa-1/turns/"):
            item = self.poll_results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return httpx.Response(200, json=item)
        if path == "/api/subagents/sa-1/cancel":
            return httpx.Response(200, json=self.cancel_result)
        if path == "/api/subagents/sa-1/close":
            return httpx.Response(200, json={"id": "sa-1", "closed": True})
        if path == "/api/subagents":
            return httpx.Response(200, json=[{"id": "sa-1", "agent_type": "codex"}])
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})

    def bodies(self, method: str = "POST") -> List[Dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == method and r.content]


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Hub:
    state = Hub()
    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(
            base_url="http://testserver", transport=httpx.MockTransport(state.handler)
        ),
    )
    monkeypatch.setattr(subagent_commands.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("CLAUDE_HUB_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "parent-tab")
    monkeypatch.delenv("CLAUDE_HUB_SUBAGENT_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    return state


def _run(args: List[str], **kwargs: Any) -> Any:
    return CliRunner().invoke(cli, args, **kwargs)


def test_run_blocks_and_prints_only_final_text(hub: Hub, tmp_path: Path) -> None:
    hub.poll_results = [_turn("running"), _turn("completed", final_text="The answer.")]
    result = _run(["agent", "run", "--provider", "codex", "--prompt", "Explain X"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "The answer.\n"
    assert "sa-1" in result.stderr
    body = hub.bodies()[0]
    assert body["agent_type"] == "codex"
    assert body["prompt"] == "Explain X"
    assert body["parent_tab_id"] == "parent-tab"
    assert body["cwd"] == str(tmp_path.resolve())
    assert body["keep"] is False
    assert body["timeout_seconds"] == 1800
    assert body["call_id"].startswith("agent-run-")
    polls = [r for r in hub.requests if r.method == "GET"]
    assert polls[0].url.path == "/api/subagents/sa-1/turns/turn-1"
    assert polls[0].url.params["wait"] == "20.0"


@pytest.mark.parametrize(
    ("flag", "expected_solo"), [(None, False), ("--solo", True), ("--no-solo", False)]
)
def test_antigravity_uses_full_model_id_and_explicit_solo(
    hub: Hub, flag: str | None, expected_solo: bool
) -> None:
    hub.poll_results = [_turn("completed", final_text="Ready.")]
    args = [
        "agent",
        "run",
        "--provider",
        "antigravity",
        "--model",
        "gemini-4-argon-high",
        "--prompt",
        "Reply briefly",
    ]
    if flag:
        args.append(flag)
    result = _run(args)
    assert result.exit_code == 0, result.output
    assert result.stdout == "Ready.\n"
    body = hub.bodies()[0]
    assert body["agent_type"] == "antigravity"
    assert body["model"] == "gemini-4-argon-high"
    assert body["solo_mode"] is expected_solo
    assert "effort" not in body


@pytest.mark.parametrize(("status", "code"), [("failed", 1), ("timeout", 124), ("cancelled", 130)])
def test_run_exit_codes(hub: Hub, status: str, code: int) -> None:
    hub.poll_results = [_turn(status, error="went wrong")]
    result = _run(["agent", "run", "--provider", "claude", "--prompt", "x", "--no-parent"])
    assert result.exit_code == code
    assert result.stdout == ""
    assert "went wrong" in result.stderr
    assert "parent_tab_id" not in hub.bodies()[0]
    # The caller still identifies itself so the server can count nesting depth.
    assert hub.bodies()[0]["caller_tab_id"] == "parent-tab"


@pytest.mark.parametrize("parent_args", [[], ["--no-parent"], ["--parent-tab-id", "elsewhere"]])
def test_run_always_sends_caller_identity(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, parent_args: List[str]
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_SUBAGENT_ID", "sa-caller")
    hub.poll_results = [_turn("completed", final_text="ok")]
    result = _run(["agent", "run", "--provider", "codex", "--prompt", "x", *parent_args])
    assert result.exit_code == 0, result.output
    body = hub.bodies()[0]
    assert body["caller_tab_id"] == "parent-tab"
    assert body["caller_subagent_id"] == "sa-caller"
    expected_parent = {0: "parent-tab", 1: None}.get(len(parent_args), "elsewhere")
    assert body.get("parent_tab_id") == expected_parent


def test_run_json_background_and_options(hub: Hub) -> None:
    result = _run(
        [
            "agent",
            "run",
            "--provider",
            "codex",
            "--model",
            "gpt-x",
            "--effort",
            "high",
            "--keep",
            "--name",
            "Reviewer",
            "--timeout",
            "90s",
            "--call-id",
            "call-7",
            "--env-preset",
            "p1",
            "--background",
            "--json",
            "--prompt",
            "x",
        ]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "running"
    assert [r.method for r in hub.requests] == ["POST"]
    body = hub.bodies()[0]
    assert body["call_id"] == "call-7"
    assert body["timeout_seconds"] == 90
    assert body["model"] == "gpt-x" and body["effort"] == "high"
    assert body["keep"] is True and body["name"] == "Reviewer" and body["env_preset"] == "p1"


def test_run_json_prints_structured_result(hub: Hub) -> None:
    hub.poll_results = [_turn("completed", final_text="ok")]
    result = _run(["agent", "run", "--provider", "codex", "--json", "--prompt", "x"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["final_text"] == "ok"


def test_run_retries_transport_failure_with_same_call_id(hub: Hub) -> None:
    hub.create_failures = 2
    hub.poll_results = [_turn("completed", final_text="ok")]
    result = _run(["agent", "run", "--provider", "codex", "--prompt", "x"])
    assert result.exit_code == 0, result.output
    ids = {r.content for r in hub.requests if r.method == "POST"}
    assert len([r for r in hub.requests if r.method == "POST"]) == 3
    assert len(ids) == 1  # identical body, including the call_id


def test_ctrl_c_cancels_the_turn(hub: Hub) -> None:
    hub.poll_results = [KeyboardInterrupt()]
    result = _run(["agent", "run", "--provider", "codex", "--prompt", "x"])
    assert result.exit_code == 130
    assert hub.requests[-1].url.path == "/api/subagents/sa-1/cancel"
    assert hub.requests[-1].url.params["turn_id"] == "turn-1"


@pytest.mark.parametrize("command", ["run", "send"])
def test_ctrl_c_during_start_cancels_by_call_id(hub: Hub, command: str) -> None:
    hub.create_interrupts = 1
    args = (
        ["agent", "run", "--provider", "codex"] if command == "run" else ["agent", "send", "sa-1"]
    )
    result = _run([*args, "--prompt", "x", "--call-id", "call-9"])
    assert result.exit_code == 130
    posts = [r for r in hub.requests if r.method == "POST"]
    # The interrupted POST is repeated with the same body (same call_id) to
    # learn the turn it started, which is then cancelled by id.
    assert len(posts) == 3
    assert posts[0].content == posts[1].content
    assert json.loads(posts[1].content)["call_id"] == "call-9"
    assert posts[2].url.path == "/api/subagents/sa-1/cancel"
    assert posts[2].url.params["turn_id"] == "turn-1"
    assert "call-9" in result.stderr


def test_second_ctrl_c_during_start_explains_how_to_cancel(hub: Hub) -> None:
    hub.create_interrupts = 2
    result = _run(["agent", "run", "--provider", "codex", "--prompt", "x", "--call-id", "c-2"])
    assert result.exit_code == 130
    assert "agent cancel" in result.stderr and "c-2" in result.stderr
    assert all(r.url.path == "/api/subagents" for r in hub.requests)


def test_result_wait_ctrl_c_only_stops_watching(hub: Hub) -> None:
    hub.poll_results = [_turn("running"), KeyboardInterrupt()]
    result = _run(["agent", "result", "sa-1", "--wait"])
    assert result.exit_code == 130
    assert all("/cancel" not in r.url.path for r in hub.requests)
    assert "keeps running" in result.stderr


@pytest.mark.parametrize(
    ("flag", "expected"), [("--solo", True), ("--no-solo", False), (None, None)]
)
def test_run_solo_flag(hub: Hub, flag: Any, expected: Any) -> None:
    hub.poll_results = [_turn("completed", final_text="ok")]
    args = ["agent", "run", "--provider", "codex", "--prompt", "x"]
    result = _run(args + ([flag] if flag else []))
    assert result.exit_code == 0, result.output
    assert hub.bodies()[0].get("solo_mode") == expected


@pytest.mark.parametrize("mode", ["file", "stdin"])
def test_prompt_from_file_or_stdin(hub: Hub, tmp_path: Path, mode: str) -> None:
    hub.poll_results = [_turn("completed", final_text="ok")]
    text = "multi\nline 中文\n"
    path = tmp_path / "p.md"
    path.write_text(text, encoding="utf-8")
    args = [
        "agent",
        "run",
        "--provider",
        "codex",
        "--prompt-file",
        str(path) if mode == "file" else "-",
    ]
    result = _run(args, input=text)
    assert result.exit_code == 0, result.output
    assert hub.bodies()[0]["prompt"] == text


def test_usage_errors(hub: Hub) -> None:
    assert _run(["agent", "run", "--provider", "codex", "--prompt", "  "]).exit_code == 2
    assert _run(["agent", "run", "--provider", "bogus", "--prompt", "x"]).exit_code == 2
    assert (
        _run(
            ["agent", "run", "--provider", "codex", "--prompt", "x", "--timeout", "soon"]
        ).exit_code
        == 2
    )
    assert hub.requests == []


def test_send_result_list_and_close(hub: Hub) -> None:
    hub.poll_results = [_turn("completed", final_text="second")]
    sent = _run(["agent", "send", "sa-1", "--prompt", "more"])
    assert sent.exit_code == 0 and sent.stdout == "second\n"
    assert hub.bodies()[0]["prompt"] == "more"
    hub.poll_results = [_turn("running"), _turn("completed", final_text="later")]
    waited = _run(["agent", "result", "sa-1", "--wait"])
    assert waited.exit_code == 0 and waited.stdout == "later\n"
    assert hub.requests[-2].url.path == "/api/subagents/sa-1/turns/latest"
    hub.poll_results = [_turn("running")]
    pending = _run(["agent", "result", "sa-1", "turn-1"])
    assert pending.exit_code == 124
    listed = _run(["--json", "agent", "subagents"])
    assert json.loads(listed.stdout)[0]["id"] == "sa-1"
    assert hub.requests[-1].url.params["parent_tab_id"] == "parent-tab"
    closed = _run(["--json", "agent", "close", "sa-1"])
    assert json.loads(closed.stdout)["closed"] is True
