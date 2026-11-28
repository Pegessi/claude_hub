"""Tests for the Claude Hub CLI."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import click
import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.main import cli


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> HubClient:
    transport = httpx.MockTransport(handler)
    return HubClient(base_url="http://testserver", transport=transport, **kwargs)


def patch_get_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> List[httpx.Request]:
    """Make get_client return a MockTransport-backed client; record requests."""
    captured: List[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def fake_get_client(ctx: click.Context) -> HubClient:
        return make_client(recording_handler, **kwargs)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    return captured


def test_help_lists_groups():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    expected_groups = (
        "workspace",
        "task",
        "agent",
        "session",
        "lessons",
        "dot",
        "schedule",
        "tab",
        "feishu",
        "quota",
        "remote",
        "auth",
        "api",
    )
    for group in expected_groups:
        assert group in result.output
    listed = cli.list_commands(click.Context(cli))
    assert set(listed) == set(expected_groups)
    for retired in (
        "filesystem",
        "fs",
        "clipboard",
        "system",
        "terminal",
        "feedback",
        "patrol",
    ):
        assert retired not in listed


QUOTA_SAMPLE = {
    "providers": [
        {
            "provider": "codex",
            "available": True,
            "state": "near",
            "message": "",
            "as_of": "2026-10-15T04:07:36.155Z",
            "stale": False,
            "limits": [
                {
                    "scope": "weekly",
                    "label": "Weekly limit",
                    "state": "near",
                    "used_percent": 82,
                    "remaining_percent": 18,
                    "resets_at": "2026-10-20T18:38:35Z",
                }
            ],
            "plan": "pro",
            "credit_balance": "62500.0000000000",
            "enforcing": None,
        },
        {
            "provider": "maso",
            "available": False,
            "state": "unavailable",
            "message": "MASO quota is unavailable. Check the MASO service, then refresh.",
            "as_of": None,
            "stale": False,
            "limits": [],
            "plan": None,
            "credit_balance": None,
            "enforcing": None,
        },
    ]
}


def test_quota_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=QUOTA_SAMPLE)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "quota", "--refresh"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == QUOTA_SAMPLE
    assert captured[0].url.path == "/api/quota"
    assert captured[0].url.params["refresh"] == "true"
    assert captured[0].extensions["timeout"]["read"] == 35.0


def test_quota_table_shows_limits_and_unavailable_reason(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=QUOTA_SAMPLE)

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["quota"])
    assert result.exit_code == 0, result.output
    assert "codex: near" in result.output
    assert "plan=pro" in result.output and "credit_balance=62500.0000000000" in result.output
    assert "weekly" in result.output and "82" in result.output
    assert "maso: unavailable" in result.output
    assert "Check the MASO service" in result.output
    assert "refresh" not in captured[0].url.params


def test_quota_table_renders_booleans_json_style(monkeypatch):
    payload = {
        "providers": [
            {
                "provider": "maso",
                "available": True,
                "state": "within",
                "message": "",
                "as_of": None,
                "stale": True,
                "limits": [],
                "plan": None,
                "credit_balance": None,
                "enforcing": True,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["quota"])
    assert result.exit_code == 0, result.output
    assert "enforcing=true" in result.output
    assert "stale=true" in result.output


def test_quota_error_exits_nonzero(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["quota"])
    assert result.exit_code != 0


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
        "execution_control",
        "review_profiles",
    }
    assert body["execution_control"] == "workspace"
    assert body["review_profiles"] == ["code", "ui"]
    assert body["agent_type"] == "codex"
    assert body["task_mode"] == "reviewed"
    assert body["execution_complexity"] == "auto"


def test_task_create_explicit_session_kind(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "task",
            "create",
            "ws1",
            "--title",
            "Chat",
            "--prompt",
            "do it",
            "--session-id",
            "chat-session",
            "--session-kind",
            "chat",
        ],
    )

    assert result.exit_code == 0, result.output
    assert bodies[0]["session_id"] == "chat-session"
    assert bodies[0]["session_kind"] == "chat"


def test_task_create_agent_tag_success(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "t1", "agent_tag": "worker-a"})

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
            "--agent-tag",
            "worker-a",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["agent_tag"] == "worker-a"


def test_task_create_agent_tag_invalid_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"msg": "agent_tag must not be empty"}]})

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
            "--agent-tag",
            "   ",
        ],
    )
    assert result.exit_code != 0


def test_task_create_agent_tag_invalid_control_character(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(422, json={"detail": [{"msg": "control characters"}]})

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
            "--agent-tag",
            "bad\nline",
        ],
    )
    assert result.exit_code != 0
    assert bodies[0]["agent_tag"] == "bad\nline"


def test_task_create_without_agent_tag_unchanged(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["task", "create", "ws1", "--title", "T", "--prompt", "do it"],
    )
    assert result.exit_code == 0, result.output
    assert "agent_tag" not in bodies[0]


def test_task_update_agent_tag_clear(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1", "agent_tag": None})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["task", "update", "t1", "--agent-tag", ""],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["agent_tag"] == ""


def test_task_create_parent_task_id_and_payload_merge(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "child-1", "parent_task_id": "parent-cli"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "ws1",
            "--title",
            "Child",
            "--prompt",
            "sub-step",
            "--parent-task-id",
            "parent-cli",
            "--payload-json",
            '{"parent_task_id":"parent-json","prompt":"from json"}',
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["parent_task_id"] == "parent-cli"
    assert bodies[0]["prompt"] == "sub-step"
    assert bodies[0]["title"] == "Child"


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


def test_task_followup_posts_to_task_mailbox(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sequence": 1,
                "call_id": "send-1",
                "type": "followup",
                "task_id": "t1",
                "consumer_key": "task:t1",
            },
        )

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "task",
            "followup",
            "t1",
            "--workspace-id",
            "ws1",
            "--message",
            "hello",
            "--call-id",
            "send-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [request.url.path for request in captured] == ["/api/workspaces/ws1/tasks/t1/followup"]
    assert json.loads(captured[0].content) == {"message": "hello", "call_id": "send-1"}
    assert "call_id=send-1" in result.stderr
    assert "/board" not in captured[0].url.path
    assert "/sessions/" not in captured[0].url.path


def test_task_followup_missing_task_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Task not found"})

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["task", "followup", "nope", "--workspace-id", "ws1", "--message", "hi"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
    assert captured[0].url.path.endswith("/followup")


def test_task_status_o1_endpoint_includes_reports_and_brief(monkeypatch):
    status_payload = {
        "workspace_id": "wsB",
        "task": {"id": "t9", "title": "T", "status": "review"},
        "lifecycle": {"phase": "reviewing", "reason": "awaiting_reviewer"},
        "goal_packet": None,
        "latest_report": {"created_at": "t2", "state": "review_passed"},
        "latest_report_message": "ok",
        "latest_acceptance_report": None,
        "latest_acceptance_check": [],
        "latest_review": {"created_at": "t2", "state": "review_passed"},
        "review_reports": [
            {
                "created_at": "t1",
                "state": "review_failed",
                "session_id": "cb-reviewer-1",
                "review_cycle": 1,
                "review_reason": "needs work",
            },
            {
                "created_at": "t2",
                "state": "review_passed",
                "session_id": "cb-reviewer-1",
            },
        ],
        "reports": [
            {"created_at": "t2", "state": "review_passed"},
            {"created_at": "t1", "state": "review_failed"},
        ],
        "reports_meta": {"total": 3, "returned": 2, "truncated": True},
        "brief_markdown": "# Task Brief\nReady.",
        "human_acceptance_requested_at": None,
        "human_accepted_at": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/tasks/t9/status"
        assert request.url.params.get("reports_limit") == "2"
        assert request.url.params.get("include_brief") == "true"
        return httpx.Response(200, json=status_payload)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "task", "status", "t9", "--reports", "2", "--brief"])
    assert result.exit_code == 0, result.output
    detail = json.loads(result.output)
    assert detail["workspace_id"] == "wsB"
    assert [r["created_at"] for r in detail["reports"]] == ["t2", "t1"]
    assert detail["reports_meta"]["truncated"] is True
    assert detail["brief_markdown"] == "# Task Brief\nReady."
    assert [r["state"] for r in detail["review_reports"]] == [
        "review_failed",
        "review_passed",
    ]


def test_task_status_workspace_mismatch_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/tasks/t9/status"
        return httpx.Response(
            200,
            json={
                "workspace_id": "wsB",
                "task": {"id": "t9", "status": "working"},
                "reports": [],
                "reports_meta": {"total": 0, "returned": 0, "truncated": False},
            },
        )

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "status", "t9", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not found in workspace wsA" in result.output


def test_task_accept_marks_review_task_done(monkeypatch):
    patches: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces/tasks/t9" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "t9",
                    "workspace_id": "wsA",
                    "status": "review",
                    "human_acceptance_requested_at": "2026-06-28T01:00:00",
                },
            )
        if path == "/api/workspaces/tasks/t9" and request.method == "PATCH":
            patches.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "t9", "status": "done"})
        raise AssertionError(f"unexpected {request.method} {path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "task", "accept", "t9", "--workspace-id", "wsA"])
    assert result.exit_code == 0, result.output
    assert patches == [{"status": "done"}]
    assert json.loads(result.output)["status"] == "done"


def test_task_accept_rejects_non_review(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "t9", "workspace_id": "wsA", "status": "working"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "accept", "t9", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not 'review'" in result.output


def test_task_accept_rejects_review_without_human_acceptance(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "t9", "workspace_id": "wsA", "status": "review"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "accept", "t9", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not awaiting human acceptance" in result.output


def test_session_list_and_logs_support_terminal_and_chat(monkeypatch):
    history = "\n".join(f"line{i}" for i in range(1, 121))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces/wsA/board":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {"id": "s1", "role": "orchestrator", "tab_id": "tab1"},
                        {"id": "s2", "role": "reviewer", "tab_id": "tab2"},
                    ]
                },
            )
        if path == "/api/workspaces/sessions/s2":
            return httpx.Response(
                200,
                json={
                    "id": "s2",
                    "workspace_id": "wsA",
                    "session_kind": "terminal",
                    "tab_id": "tab2",
                },
            )
        if path == "/api/workspaces/sessions/s-chat":
            return httpx.Response(
                200,
                json={
                    "id": "s-chat",
                    "workspace_id": "wsA",
                    "session_kind": "chat",
                    "archived": True,
                },
            )
        if path == "/api/workspaces/sessions/s-chat/stream/events":
            assert request.url.params.get("persisted_only") == "true"
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "type": "turn_started",
                            "turn_id": "t1",
                            "message_id": "t1:user",
                            "payload": {"summary": "check status"},
                        },
                        {
                            "type": "text_delta",
                            "turn_id": "t1",
                            "message_id": "t1:assistant",
                            "payload": {"text": "all green"},
                        },
                    ],
                    "has_more": False,
                },
            )
        if path == "/api/terminal/history/tab2":
            assert request.url.params.get("lines") == "100"
            return httpx.Response(200, json={"history": history})
        raise AssertionError(f"unexpected path {path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "session", "list", "wsA", "--role", "reviewer"])
    assert result.exit_code == 0, result.output
    assert [row["id"] for row in json.loads(result.output)] == ["s2"]

    result = runner.invoke(cli, ["session", "logs", "s2", "--lines", "2"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["line119", "line120"]

    chat_logs = runner.invoke(cli, ["session", "logs", "s-chat", "--lines", "5"])
    assert chat_logs.exit_code == 0, chat_logs.output
    assert "[user] check status" in chat_logs.output
    assert "[assistant] all green" in chat_logs.output


def test_session_report_payload_json_and_bilingual_fields(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/sessions/s1/reports"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "r1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    payload = {
        "goal_packet": {"objective": "ship"},
        "acceptance_check": [{"criterion": "tests", "status": "passed", "evidence": "ok"}],
        "review_decision": "skip",
        "changed_files": ["ignored.py"],
    }
    result = runner.invoke(
        cli,
        [
            "--json",
            "session",
            "report",
            "s1",
            "--state",
            "completed",
            "--message",
            "Done",
            "--message-en",
            "Done",
            "--message-zh",
            "完成",
            "--task-id",
            "t1",
            "--changed-file",
            "kept.py",
            "--review-decision",
            "request",
            "--review-reason",
            "nontrivial",
            "--risk-level",
            "medium",
            "--payload-json",
            json.dumps(payload),
        ],
    )
    assert result.exit_code == 0, result.output
    body = bodies[0]
    assert body["goal_packet"] == {"objective": "ship"}
    assert body["acceptance_check"][0]["criterion"] == "tests"
    assert body["message_en"] == "Done"
    assert body["message_zh"] == "完成"
    assert body["task_id"] == "t1"
    assert body["changed_files"] == ["kept.py"]  # explicit flags override payload
    assert body["review_decision"] == "request"
    assert body["review_reason"] == "nontrivial"
    assert body["risk_level"] == "medium"


def test_session_report_payload_json_requires_object():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "session",
            "report",
            "s1",
            "--state",
            "working",
            "--message",
            "m",
            "--payload-json",
            "[]",
        ],
    )
    assert result.exit_code != 0
    assert "JSON object" in result.output


def test_session_report_payload_json_can_supply_changed_files(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "r1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "session",
            "report",
            "s1",
            "--state",
            "working",
            "--message",
            "m",
            "--payload-json",
            json.dumps({"changed_files": ["from-json.py"]}),
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["changed_files"] == ["from-json.py"]


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


def _resolve_with_env(
    tmp_path, monkeypatch, *, env: Optional[Dict[str, str]] = None, **overrides: Any
) -> Any:
    """Call resolve_settings against a missing config file with clean env."""
    from claude_hub.cli.config import resolve_settings

    for key in ("CLAUDE_HUB_URL", "CLAUDE_HUB_HOST", "CLAUDE_HUB_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    kwargs: Dict[str, Any] = dict(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(tmp_path / "missing.toml"),
    )
    kwargs.update(overrides)
    return resolve_settings(**kwargs)


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("::1", 8173, "http://[::1]:8173"),
        ("[::1]", 8173, "http://[::1]:8173"),  # already bracketed: no doubling
        ("::", 8173, "http://[::1]:8173"),  # wildcard -> loopback
        ("0.0.0.0", 8173, "http://127.0.0.1:8173"),
        ("127.0.0.1", 9000, "http://127.0.0.1:9000"),
        ("localhost", 8173, "http://localhost:8173"),
    ],
)
def test_base_url_derived_from_host_env(tmp_path, monkeypatch, host, port, expected):
    def fail_probe(url: str) -> bool:
        raise AssertionError(f"explicit CLAUDE_HUB_HOST must skip probing: {url}")

    s = _resolve_with_env(
        tmp_path,
        monkeypatch,
        env={"CLAUDE_HUB_HOST": host, "CLAUDE_HUB_PORT": str(port)},
        health_probe=fail_probe,
    )
    assert s.base_url == expected


def test_explicit_url_sources_skip_probe_and_beat_host_env(tmp_path, monkeypatch):
    from claude_hub.cli.config import resolve_settings

    config_file = tmp_path / "config.toml"
    config_file.write_text('[default]\nbase_url = "http://from-file:9999"\n', encoding="utf-8")
    monkeypatch.delenv("CLAUDE_HUB_URL", raising=False)
    monkeypatch.setenv("CLAUDE_HUB_HOST", "::1")

    def fail_probe(url: str) -> bool:
        raise AssertionError(f"explicit URL sources must skip probing: {url}")

    def resolve(**overrides: Any) -> Any:
        kwargs: Dict[str, Any] = dict(
            base_url=None,
            token=None,
            cookie=None,
            json_output=False,
            verbose=False,
            config_path=str(config_file),
            health_probe=fail_probe,
        )
        kwargs.update(overrides)
        return resolve_settings(**kwargs)

    # config file beats CLAUDE_HUB_HOST
    assert resolve().base_url == "http://from-file:9999"

    # CLAUDE_HUB_URL beats config file and host
    monkeypatch.setenv("CLAUDE_HUB_URL", "http://from-env:8888")
    assert resolve().base_url == "http://from-env:8888"

    # flag beats everything
    assert resolve(base_url="http://from-flag:7777").base_url == "http://from-flag:7777"


def test_auto_probe_prefers_ipv4_when_healthy(tmp_path, monkeypatch):
    seen: List[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return url == "http://127.0.0.1:8173"

    s = _resolve_with_env(tmp_path, monkeypatch, health_probe=probe)
    assert s.base_url == "http://127.0.0.1:8173"
    assert seen == ["http://127.0.0.1:8173"]  # short-circuits, no IPv6 probe


def test_auto_probe_falls_back_to_ipv6(tmp_path, monkeypatch):
    seen: List[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return url == "http://[::1]:8173"

    s = _resolve_with_env(tmp_path, monkeypatch, health_probe=probe)
    assert s.base_url == "http://[::1]:8173"
    assert seen == ["http://127.0.0.1:8173", "http://[::1]:8173"]


def test_auto_probe_uses_env_port(tmp_path, monkeypatch):
    seen: List[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return url == "http://[::1]:9000"

    s = _resolve_with_env(
        tmp_path, monkeypatch, env={"CLAUDE_HUB_PORT": "9000"}, health_probe=probe
    )
    assert s.base_url == "http://[::1]:9000"
    assert seen == ["http://127.0.0.1:9000", "http://[::1]:9000"]


def test_auto_probe_all_unhealthy_falls_back_to_default(tmp_path, monkeypatch):
    from claude_hub.cli.config import DEFAULT_BASE_URL

    s = _resolve_with_env(tmp_path, monkeypatch, health_probe=lambda url: False)
    assert s.base_url == DEFAULT_BASE_URL


def test_blank_host_env_triggers_probe(tmp_path, monkeypatch):
    seen: List[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return True

    s = _resolve_with_env(tmp_path, monkeypatch, env={"CLAUDE_HUB_HOST": "   "}, health_probe=probe)
    assert s.base_url == "http://127.0.0.1:8173"
    assert seen == ["http://127.0.0.1:8173"]


def test_invalid_port_env_falls_back_to_default_port(tmp_path, monkeypatch):
    seen: List[str] = []

    def probe(url: str) -> bool:
        seen.append(url)
        return False

    _resolve_with_env(
        tmp_path,
        monkeypatch,
        env={"CLAUDE_HUB_PORT": "not-a-port"},
        health_probe=probe,
    )
    assert seen == ["http://127.0.0.1:8173", "http://[::1]:8173"]


def test_probe_health_requires_hub_identity():
    from claude_hub.cli.config import probe_health

    cases = [
        (httpx.Response(200, json={"status": "healthy", "instance_id": "hub"}), True),
        (httpx.Response(200, text="not json"), False),  # 2xx non-JSON body
        (httpx.Response(200, json={"status": "degraded"}), False),
        (httpx.Response(500, json={"status": "healthy"}), False),
    ]
    for response, expected in cases:
        seen: List[httpx.Request] = []

        def handler(request: httpx.Request, _response: httpx.Response = response) -> httpx.Response:
            seen.append(request)
            return _response

        result = probe_health("http://127.0.0.1:8173", transport=httpx.MockTransport(handler))
        assert result is expected
        assert seen[0].url.path == "/health"

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert (
        probe_health("http://127.0.0.1:8173", transport=httpx.MockTransport(raise_connect_error))
        is False
    )


def test_whitespace_only_explicit_urls_fall_through(tmp_path, monkeypatch):
    from claude_hub.cli.config import DEFAULT_BASE_URL, resolve_settings

    config_file = tmp_path / "config.toml"
    for key in ("CLAUDE_HUB_URL", "CLAUDE_HUB_HOST", "CLAUDE_HUB_PORT"):
        monkeypatch.delenv(key, raising=False)

    def resolve(**overrides: Any) -> Any:
        kwargs: Dict[str, Any] = dict(
            base_url=None,
            token=None,
            cookie=None,
            json_output=False,
            verbose=False,
            config_path=str(config_file),
            health_probe=lambda url: False,
        )
        kwargs.update(overrides)
        return resolve_settings(**kwargs)

    # A padded-but-valid config URL is stripped and used verbatim otherwise.
    config_file.write_text('[default]\nbase_url = "  http://from-file:9999  "\n', encoding="utf-8")
    assert resolve().base_url == "http://from-file:9999"

    # Whitespace-only / empty config value falls through to the probe chain.
    for raw in ('[default]\nbase_url = "   "\n', '[default]\nbase_url = ""\n'):
        config_file.write_text(raw, encoding="utf-8")
        assert resolve().base_url == DEFAULT_BASE_URL

    # Whitespace-only / empty env URL falls through to the config file.
    config_file.write_text('[default]\nbase_url = "http://from-file:9999"\n', encoding="utf-8")
    for raw_env in ("   ", ""):
        monkeypatch.setenv("CLAUDE_HUB_URL", raw_env)
        assert resolve().base_url == "http://from-file:9999"

    # A padded env URL is stripped and used.
    monkeypatch.setenv("CLAUDE_HUB_URL", "  http://from-env:8888  ")
    assert resolve().base_url == "http://from-env:8888"

    # Whitespace-only / empty flag falls through to the env value.
    for raw_flag in ("   ", ""):
        assert resolve(base_url=raw_flag).base_url == "http://from-env:8888"

    # A padded flag URL is stripped and wins over env.
    assert resolve(base_url="  http://from-flag:7777  ").base_url == "http://from-flag:7777"


def test_resolve_workspace_id_zero_one_and_ambiguous_matches(tmp_path, monkeypatch):
    from claude_hub.cli.commands.common import resolve_workspace_id

    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()

    # 1. Zero matches -> fail closed without creating any workspace
    zero_client = make_client(
        lambda req: httpx.Response(
            200,
            json=[
                {
                    "id": "ws-other",
                    "name": "Other",
                    "path": str(other),
                    "target": "local",
                }
            ],
        )
    )
    with pytest.raises(click.ClickException, match="No Hub Workspace matches"):
        resolve_workspace_id(zero_client, None, cwd=repo)

    # 2. Exactly 1 match -> resolves cleanly
    one_client = make_client(
        lambda req: httpx.Response(
            200,
            json=[{"id": "ws-1", "name": "Repo", "path": str(repo), "target": "local"}],
        )
    )
    assert resolve_workspace_id(one_client, None, cwd=repo) == "ws-1"

    # 3. Multiple matches (e.g. two workspaces pointing to same tree) -> fail closed
    ambig_client = make_client(
        lambda req: httpx.Response(
            200,
            json=[
                {"id": "ws-a", "name": "LocalA", "path": str(repo), "target": "local"},
                {"id": "ws-b", "name": "LocalB", "path": str(repo), "target": "local"},
            ],
        )
    )
    with pytest.raises(click.ClickException, match="Multiple Hub Workspaces match"):
        resolve_workspace_id(ambig_client, None, cwd=repo)


def test_transport_error_names_url_and_suggests_ipv6_loopback_same_port():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    client = HubClient(
        base_url="http://127.0.0.1:9911",
        transport=httpx.MockTransport(handler),
        token="secret-session-token",
    )
    with pytest.raises(HubError) as exc:
        client.list_workspaces()
    message = str(exc.value)
    assert "http://127.0.0.1:9911/api/workspaces" in message
    # The IPv6 suggestion keeps the REQUEST port instead of hardcoding 8173.
    assert "http://[::1]:9911" in message
    assert "8173" not in message
    assert "CLAUDE_HUB_URL" in message
    # Credentials live in the cookie header; they must never surface in the error.
    assert "secret-session-token" not in message
    assert "cookie" not in message.lower()


def test_transport_error_non_loopback_host_has_no_bracketed_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    client = HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler))
    with pytest.raises(HubError) as exc:
        client.list_workspaces()
    message = str(exc.value)
    assert "http://testserver/api/workspaces" in message
    assert "[::1]" not in message
    assert "8173" not in message  # no hardcoded default port
    assert "same port" in message


def test_transport_error_redacts_oauth_callback_query_secrets():
    # The auth callback puts the OAuth code and CSRF state in the query
    # string; a connection failure must never echo them back to the terminal.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    client = HubClient(
        base_url="http://127.0.0.1:9911",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HubError) as exc:
        client.auth_callback_response(
            code="oauth-authorization-code-SECRET",
            state="oauth-csrf-state-SECRET",
        )
    message = str(exc.value)
    # Scheme/host/port/path remain for diagnosis, and the IPv6 hint survives.
    assert "http://127.0.0.1:9911/api/auth/callback" in message
    assert "http://[::1]:9911" in message
    # The secret query values and the query string itself are stripped.
    assert "oauth-authorization-code-SECRET" not in message
    assert "oauth-csrf-state-SECRET" not in message
    assert "code=" not in message
    assert "state=" not in message
    assert "?" not in message


def test_transport_error_redacts_url_userinfo_password():
    # A base URL with embedded userinfo (user:password@host) must render the
    # host endpoint only, never the credentials.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    client = HubClient(
        base_url="http://connector:userinfo-password-SECRET@127.0.0.1:9911",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HubError) as exc:
        client.list_workspaces()
    message = str(exc.value)
    assert "http://127.0.0.1:9911/api/workspaces" in message
    assert "http://[::1]:9911" in message  # same-port IPv6 hint retained
    assert "userinfo-password-SECRET" not in message
    assert "connector" not in message
    assert "@" not in message


def test_env_preset_for_agent_type_defaults_claude_to_day1(tmp_path, monkeypatch):
    from claude_hub.cli.config import resolve_settings

    config_file = tmp_path / "config.toml"
    config_file.write_text('[default]\nbase_url = "http://x"\n', encoding="utf-8")
    monkeypatch.delenv("CLAUDE_HUB_URL", raising=False)
    settings = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    assert settings.env_preset_for_agent_type("claude") == "day1"
    assert settings.env_preset_for_agent_type("codex") is None

    config_file.write_text(
        '[default]\nbase_url = "http://x"\n\n[default_env_presets]\nclaude = "none"\n',
        encoding="utf-8",
    )
    settings_none = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
    )
    assert settings_none.env_preset_for_agent_type("claude") is None


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
    result = runner.invoke(cli, ["workspace", "status", "ws1", "--full"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "AttributeError" not in result.output
    assert "t1" in result.output


def test_workspace_status_surfaces_summary_and_full_board(monkeypatch):
    board = {
        "workspace": {
            "id": "ws1",
            "name": "Demo",
            "path": "/repo",
            "default_branch": "main",
        },
        "tasks": [
            {"id": "t1", "title": "Build", "status": "working", "session_id": "s1"},
            {"id": "t2", "title": "Review", "status": "review", "session_id": "s2"},
        ],
        "sessions": [
            {
                "id": "s1",
                "role": "orchestrator",
                "agent_type": "codex",
                "status": "active",
                "runtime_status": "working",
                "current_task_id": "t1",
                "tab_id": "tab1",
            }
        ],
        "reports": [
            {
                "task_id": "t1",
                "state": "working",
                "message": "editing",
                "created_at": "2026-06-29T01:00:00",
            }
        ],
        "markdown_documents": [
            {
                "id": "doc1",
                "source": "snapshot",
                "label": "Snapshot",
                "path": "/state/snapshot.md",
            }
        ],
        "snapshot_path": "/state/snapshot.md",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/ws1/board"
        return httpx.Response(200, json=board)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "workspace", "status", "ws1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task_counts"] == {"review": 1, "working": 1}
    assert payload["session_counts"] == {"working": 1}
    assert payload["active_tasks"][0]["latest_report_message"] == "editing"
    assert payload["snapshot_path"] == "/state/snapshot.md"

    text_summary = runner.invoke(cli, ["workspace", "status", "ws1"])
    assert text_summary.exit_code == 0, text_summary.output
    assert "snapshot: /state/snapshot.md" in text_summary.output


def test_session_list_and_status_show_runtime(monkeypatch):
    session_s1 = {
        "id": "s1",
        "workspace_id": "ws1",
        "role": "orchestrator",
        "agent_type": "codex",
        "session_kind": "chat",
        "status": "active",
        "runtime_status": "working",
        "current_task_id": "t1",
        "tab_id": "tab1",
        "target": "local",
        "last_activity_at": "2026-06-29T01:00:00",
    }
    board = {
        "sessions": [
            session_s1,
            {
                "id": "s2",
                "role": "reviewer",
                "agent_type": "claude",
                "runtime_status": "idle",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces/ws1/board":
            return httpx.Response(200, json=board)
        if request.url.path == "/api/workspaces/sessions/s1":
            return httpx.Response(200, json=session_s1)
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "session", "list", "ws1", "--role", "orchestrator"])
    assert result.exit_code == 0, result.output
    sessions = json.loads(result.output)
    assert [s["id"] for s in sessions] == ["s1"]

    result = runner.invoke(cli, ["session", "status", "s1"])
    assert result.exit_code == 0, result.output
    assert "runtime_status" in result.output
    assert "working" in result.output
    assert "tab1" in result.output


def test_task_status_surfaces_goal_review_and_acceptance_with_legacy_fallback(
    monkeypatch,
):
    goal_packet = {
        "objective": "Ship CLI display",
        "acceptance_criteria": ["status command"],
        "validation_plan": ["pytest"],
        "status": "approved",
        "updated_at": "2026-06-29T01:00:00",
    }
    reports = [
        {
            "created_at": "2026-06-29T01:00:00",
            "state": "completed",
            "session_id": "worker",
            "review_decision": "request",
            "message": "done",
            "acceptance_check": [
                {
                    "criterion": "status command",
                    "status": "passed",
                    "evidence": "test covered",
                }
            ],
        },
        {
            "created_at": "2026-06-29T02:00:00",
            "state": "review_passed",
            "session_id": "reviewer",
            "review_cycle": 1,
            "review_decision": "request",
            "review_reason": "checked",
            "message": "passed",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces/tasks/t1/status":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path == "/api/workspaces/tasks/t1":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path == "/api/workspaces/ws1/board":
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "CLI status",
                            "status": "review",
                            "agent_type": "codex",
                            "task_mode": "reviewed",
                            "execution_complexity": "simple",
                            "session_id": "worker",
                            "review_cycle": 1,
                            "reviewed_cycle": 1,
                            "review_attempts": 1,
                            "human_acceptance_requested_at": "2026-06-29T02:30:00",
                            "goal_packet": goal_packet,
                        }
                    ]
                },
            )
        if request.url.path == "/api/workspaces/ws1/tasks/t1/reports":
            return httpx.Response(200, json=reports)
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "task", "status", "t1", "--workspace-id", "ws1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["goal_packet"]["status"] == "approved"
    assert payload["latest_report_message"] == "passed"
    assert payload["latest_acceptance_report"]["state"] == "completed"
    assert payload["latest_acceptance_check"][0]["status"] == "passed"
    assert payload["review_reports"][0]["state"] == "review_passed"

    result = runner.invoke(cli, ["task", "status", "t1", "--workspace-id", "ws1"])
    assert result.exit_code == 0, result.output
    assert "Goal Packet" in result.output
    assert "approved" in result.output
    assert "Acceptance check" in result.output
    assert "source: completed 2026-06-29T01:00:00" in result.output
    assert "test covered" in result.output
    assert "review_passed" in result.output


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
    monkeypatch.delenv("CLAUDE_HUB_HOST", raising=False)
    monkeypatch.delenv("CLAUDE_HUB_PORT", raising=False)
    monkeypatch.delenv("CLAUDE_HUB_TOKEN", raising=False)

    s = resolve_settings(
        base_url=None,
        token=None,
        cookie=None,
        json_output=False,
        verbose=False,
        config_path=str(config_file),
        health_probe=lambda url: False,
    )
    # Non-string values are ignored; both probes fail so the default is used.
    assert s.base_url == DEFAULT_BASE_URL
    assert s.token is None


def test_limit_validation_error_formatted():
    detail = [
        {
            "type": "greater_than_equal",
            "loc": ["query", "limit"],
            "msg": "Input should be greater than or equal to 1",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": detail})

    client = make_client(handler)
    with pytest.raises(HubError) as exc:
        client.list_lessons("ws1", {"limit": 0})
    msg = str(exc.value)
    assert "Input should be greater than or equal to 1" in msg
    assert "{'type'" not in msg
    assert "limit:" in msg


def test_limit_out_of_range_exits(monkeypatch):
    captured = patch_get_client(monkeypatch, lambda r: httpx.Response(200, json=[]))
    runner = CliRunner()
    result = runner.invoke(cli, ["lessons", "list", "ws1", "--limit", "0"])
    assert result.exit_code == 2
    assert captured == []  # no HTTP call was made


def test_request_review_body(monkeypatch):
    paths: List[str] = []
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "request-review", "t1", "--message", "pls review"])
    assert result.exit_code == 0, result.output
    assert paths[0] == "/api/workspaces/tasks/t1/request-review"
    assert bodies[0] == {"message": "pls review"}


def test_request_review_omits_message(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "request-review", "t1"])
    assert result.exit_code == 0, result.output
    assert bodies[0] == {}


def test_api_raw_patch_with_query_and_payload(monkeypatch):
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "api",
            "raw",
            "PATCH",
            "api/example",
            "--query",
            "expand=1",
            "--payload-json",
            json.dumps({"name": "demo"}),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "method": "PATCH",
        "path": "/api/example",
        "query": {"expand": "1"},
        "body": {"name": "demo"},
    }
    assert json.loads(result.output) == {"ok": True}


def test_tab_create_antigravity_chat_body(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tabs"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "antigravity-chat"})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "tab",
            "create",
            "--name",
            "Antigravity",
            "--agent-type",
            "antigravity",
            "--session-kind",
            "chat",
            "--env",
            "ANTIGRAVITY_MODEL=gemini-example-model-1dd583607df6-high",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["agent_type"] == "antigravity"
    assert bodies[0]["session_kind"] == "chat"
    assert bodies[0]["target"] == "local"
    assert bodies[0]["solo_mode"] is False
    assert bodies[0]["env"] == {"ANTIGRAVITY_MODEL": "gemini-example-model-1dd583607df6-high"}


def test_tab_update_accepts_antigravity_type_and_preserves_model_id(monkeypatch):
    captured = patch_get_client(
        monkeypatch, lambda request: httpx.Response(200, json={"id": "tab1"})
    )
    result = CliRunner().invoke(
        cli,
        [
            "tab",
            "update",
            "tab1",
            "--agent-type",
            "antigravity",
            "--env",
            "ANTIGRAVITY_MODEL=gemini-3.8-flash-medium",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(captured[0].content) == {
        "agent_type": "antigravity",
        "env": {"ANTIGRAVITY_MODEL": "gemini-3.8-flash-medium"},
    }


def test_task_create_offers_antigravity_managed_chat(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "task1"})

    patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "task",
            "create",
            "workspace1",
            "--title",
            "Antigravity managed Chat",
            "--prompt",
            "Do nothing",
            "--agent-type",
            "antigravity",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["agent_type"] == "antigravity"
    # Chat-only providers omit the surface and let the backend resolve Chat.
    assert "session_kind" not in bodies[0]


def test_tab_create_remote_body(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tabs"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "tab1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "tab",
            "create",
            "--name",
            "remote",
            "--session-kind",
            "chat",
            "--target",
            "remote",
            "--remote-profile-id",
            "prod",
            "--remote-cwd",
            "/srv/app",
            "--env",
            "A=B",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["target"] == "remote"
    assert bodies[0]["session_kind"] == "chat"
    assert bodies[0]["remote_profile_id"] == "prod"
    assert bodies[0]["remote_cwd"] == "/srv/app"
    assert bodies[0]["env"] == {"A": "B"}


@pytest.mark.parametrize(
    "args",
    [
        ["tab", "create", "--name", "retired", "--session-kind", "agent"],
        [
            "tab",
            "create",
            "--name",
            "retired-json",
            "--payload-json",
            json.dumps({"session_kind": "agent"}),
        ],
        [
            "tab",
            "update",
            "tab-1",
            "--payload-json",
            json.dumps({"session_kind": "agent"}),
        ],
    ],
)
def test_tab_commands_reject_retired_agent_session_kind(args):
    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "agent" in result.output


def test_task_update_payload_and_flags(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/workspaces/tasks/t1"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "t1", "status": "queued"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "task",
            "update",
            "t1",
            "--status",
            "queued",
            "--review-profile",
            "code",
            "--payload-json",
            json.dumps({"title": "from-json", "status": "todo"}),
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0] == {
        "title": "from-json",
        "status": "queued",
        "review_profiles": ["code"],
    }


def test_session_send_payload_json_attachments(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/sessions/s1/send"
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    payload = {"message": "from-json", "attachments": [{"filename": "a.txt"}]}
    result = runner.invoke(cli, ["session", "send", "s1", "--payload-json", json.dumps(payload)])
    assert result.exit_code == 0, result.output
    assert bodies[0]["message"] == payload["message"]
    assert bodies[0]["attachments"] == payload["attachments"]
    assert isinstance(bodies[0]["call_id"], str)


def test_lessons_reap_workspace_and_single_task(monkeypatch):
    bodies: List[Dict[str, Any]] = []
    paths: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        paths.append(request.url.path)
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "run1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["lessons", "reap", "ws1", "--mode", "full", "--limit", "10", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert paths[0] == "/api/workspaces/ws1/lessons/summarize"
    assert bodies[0] == {"mode": "full", "limit": 10, "force": True}

    task_reap = runner.invoke(
        cli,
        ["lessons", "reap", "--task-id", "t1", "--summarize"],
    )
    assert task_reap.exit_code == 0, task_reap.output
    assert paths[1] == "/api/workspaces/tasks/t1/feedback/reap"
    assert bodies[1] == {"summarize": True}


@pytest.mark.parametrize("status", [200, 204])
def test_lessons_delete(monkeypatch, status):
    paths: List[str] = []
    methods: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        methods.append(request.method)
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "l1", "status": "archived"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["lessons", "delete", "l1", "--workspace-id", "ws1"])
    assert result.exit_code == 0, result.output
    assert paths[0] == "/api/workspaces/ws1/lessons/l1"
    assert methods[0] == "DELETE"
    if status == 204:
        assert "deleted l1" in result.output


@pytest.mark.parametrize("include_archived", [False, True])
def test_session_list_archived_filter(monkeypatch, include_archived):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/workspaces/wsA/board"
        assert request.url.params.get("include_archived") == ("true" if include_archived else None)
        return httpx.Response(
            200,
            json={"sessions": [{"id": "active"}, {"id": "old", "archived": True}]},
        )

    patch_get_client(monkeypatch, handler)
    args = ["--json", "session", "list", "wsA"]
    if include_archived:
        args.append("--include-archived")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert [row["id"] for row in json.loads(result.output)] == (
        ["active", "old"] if include_archived else ["active"]
    )


@pytest.mark.parametrize("action", ["archive", "unarchive"])
@pytest.mark.parametrize("explicit_workspace", [False, True])
def test_session_archive_commands(monkeypatch, action, explicit_workspace):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/workspaces/sessions/old":
            assert not explicit_workspace
            return httpx.Response(200, json={"id": "old", "workspace_id": "wsA", "archived": True})
        assert request.method == "POST"
        assert request.url.path == f"/api/workspaces/wsA/sessions/old/{action}"
        return httpx.Response(200, json={"id": "old", "archived": action == "archive"})

    captured = patch_get_client(monkeypatch, handler)
    args = ["--json", "session", action, "old"]
    if explicit_workspace:
        args.extend(["--workspace-id", "wsA"])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"id": "old", "archived": action == "archive"}
    assert len(captured) == (1 if explicit_workspace else 2)


@pytest.mark.parametrize("action", ["archive", "unarchive"])
def test_session_archive_command_reports_backend_error(monkeypatch, action):
    patch_get_client(
        monkeypatch,
        lambda request: httpx.Response(400, json={"detail": "Session has non-terminal tasks"}),
    )
    result = CliRunner().invoke(cli, ["session", action, "old", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "Session has non-terminal tasks" in result.output


def test_session_unarchive_unknown_session(monkeypatch):
    patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=[]))
    result = CliRunner().invoke(cli, ["session", "unarchive", "missing"])
    assert result.exit_code != 0
    assert "Session missing not found" in result.output


def _lifecycle_board() -> Dict[str, Any]:
    def lifecycle(task_id: str, phase: str, reason: str, attention: bool, **extra: Any) -> dict:
        return {
            "task_id": task_id,
            "phase": phase,
            "reason": reason,
            "attention": attention,
            **extra,
        }

    return {
        "tasks": [
            {"id": "t-run", "title": "Running", "status": "working"},
            {"id": "t-ask", "title": "Question", "status": "review"},
            {"id": "t-wait", "title": "Behind", "status": "queued"},
        ],
        "task_lifecycles": {
            "t-run": lifecycle("t-run", "running", "agent_working", False),
            "t-ask": lifecycle("t-ask", "needs_user", "reviewer_needs_input", True),
            "t-wait": lifecycle(
                "t-wait",
                "queued",
                "agent_held_by_other_task",
                False,
                blocked_by_task_id="t-ask",
            ),
        },
    }


def test_task_list_shows_lifecycle_phase_reason_and_blocker(monkeypatch):
    patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=_lifecycle_board()))
    result = CliRunner().invoke(cli, ["--json", "task", "list", "ws1"])
    assert result.exit_code == 0, result.output
    rows = {row["id"]: row for row in json.loads(result.output)}
    assert rows["t-ask"]["phase"] == "needs_user"
    assert rows["t-ask"]["phase_reason"] == "reviewer_needs_input"
    assert rows["t-wait"]["blocked_by"] == "t-ask"
    assert rows["t-run"]["status"] == "working"


def test_task_list_attention_and_phase_filters(monkeypatch):
    patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=_lifecycle_board()))
    runner = CliRunner()
    attention = runner.invoke(cli, ["--json", "task", "list", "ws1", "--attention"])
    assert [row["id"] for row in json.loads(attention.output)] == ["t-ask"]
    queued = runner.invoke(cli, ["--json", "task", "list", "ws1", "--phase", "queued"])
    assert [row["id"] for row in json.loads(queued.output)] == ["t-wait"]


def test_task_list_tolerates_board_without_lifecycles(monkeypatch):
    board = {"tasks": [{"id": "t1", "title": "Old", "status": "todo"}]}
    patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=board))
    result = CliRunner().invoke(cli, ["--json", "task", "list", "ws1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == board["tasks"]


def test_task_list_lifecycle_filters_warn_on_old_hub(monkeypatch):
    board = {"tasks": [{"id": "t1", "title": "Old", "status": "todo"}]}
    patch_get_client(monkeypatch, lambda request: httpx.Response(200, json=board))
    result = CliRunner().invoke(cli, ["task", "list", "ws1", "--attention"])
    assert result.exit_code == 0, result.output
    assert "does not report task lifecycles" in result.stderr


def test_older_server_fallback_without_workspace_id_scans_workspace_boards(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in (
            "/api/workspaces/tasks/t-done/status",
            "/api/workspaces/tasks/t-done",
            "/api/workspaces/sessions/s-arch",
        ):
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "ws1", "name": "Repo1"}])
        if path == "/api/workspaces/ws1/board":
            include_archived = request.url.params.get("include_archived") == "true"
            sessions = (
                [
                    {
                        "id": "s-arch",
                        "session_kind": "chat",
                        "status": "idle",
                        "archived": True,
                    }
                ]
                if include_archived
                else []
            )
            return httpx.Response(
                200,
                json={
                    "workspace": {"id": "ws1"},
                    "tasks": [{"id": "t-done", "title": "Old done", "status": "done"}],
                    "sessions": sessions,
                },
            )
        if path == "/api/workspaces/ws1/tasks/t-done/reports":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected path {path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    task_res = runner.invoke(cli, ["--json", "task", "status", "t-done"])
    assert task_res.exit_code == 0, task_res.output
    assert json.loads(task_res.output)["workspace_id"] == "ws1"

    sess_res = runner.invoke(cli, ["--json", "session", "status", "s-arch"])
    assert sess_res.exit_code == 0, sess_res.output
    assert json.loads(sess_res.output)["archived"] is True


def test_chat_logs_paginates_to_latest_page_and_tab_logs_uses_managed_session(
    monkeypatch,
):
    seen_session_calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tabs/tab-managed":
            return httpx.Response(
                200,
                json={
                    "id": "tab-managed",
                    "session_kind": "chat",
                    "managed_session_id": "s-managed",
                    "archived": False,
                },
            )
        if path == "/api/workspaces/sessions/s-managed/stream/events":
            since = int(request.url.params.get("since_sequence", "-1"))
            seen_session_calls.append(f"since={since}")
            if since < 0:
                return httpx.Response(
                    200,
                    json={
                        "events": [
                            {
                                "type": "turn_started",
                                "turn_id": "t1",
                                "payload": {"summary": "early prompt"},
                            },
                            {
                                "type": "text_delta",
                                "turn_id": "t1",
                                "message_id": "t1:assistant",
                                "payload": {"text": "early answer"},
                            },
                        ],
                        "next_sequence": 10,
                        "has_more": True,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "type": "turn_started",
                            "turn_id": "t2",
                            "payload": {"summary": "latest prompt"},
                        },
                        {
                            "type": "text_delta",
                            "turn_id": "t2",
                            "message_id": "t2:assistant",
                            "payload": {"text": "latest answer"},
                        },
                        {
                            "type": "tool_call_started",
                            "turn_id": "t2",
                            "payload": {"tool_name": "Read"},
                        },
                    ],
                    "next_sequence": 20,
                    "has_more": True,
                },
            )
        raise AssertionError(f"unexpected path {path}")

    def paged_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path == "/api/workspaces/sessions/s-managed/stream/events"
            and int(request.url.params.get("since_sequence", "-1")) == 20
        ):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "type": "turn_completed",
                            "turn_id": "t2",
                            "payload": {"assistant_text": "latest answer"},
                        }
                    ],
                    "next_sequence": 21,
                    "has_more": False,
                },
            )
        return handler(request)

    patch_get_client(monkeypatch, paged_handler)
    res = CliRunner().invoke(cli, ["tab", "logs", "tab-managed", "-n", "3"])
    assert res.exit_code == 0, res.output
    assert seen_session_calls == ["since=-1", "since=10"]
    assert res.output.splitlines() == [
        "[user] latest prompt",
        "[assistant] latest answer",
        "[tool] Read",
    ]


def test_task_status_reports_limit_and_older_server_brief_markdown_fallback(
    monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces/tasks/task-1/status":
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/api/workspaces/tasks/task-1":
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/api/workspaces/ws-1/board":
            return httpx.Response(
                200,
                json={"tasks": [{"id": "task-1", "workspace_id": "ws-1", "status": "working"}]},
            )
        if path == "/api/workspaces/ws-1/tasks/task-1/reports":
            return httpx.Response(200, json=[])
        if path == "/api/workspaces/ws-1/tasks/task-1/brief":
            return httpx.Response(200, json={"markdown": "# Handoff Brief\nAll clear"})
        raise AssertionError(f"unexpected path {path}")

    patch_get_client(monkeypatch, handler)
    too_high = CliRunner().invoke(cli, ["task", "status", "task-1", "--reports", "201"])
    assert too_high.exit_code == 2

    ok = CliRunner().invoke(
        cli, ["--json", "task", "status", "task-1", "--workspace-id", "ws-1", "--brief"]
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["brief_markdown"] == "# Handoff Brief\nAll clear"


def test_older_server_fallbacks_for_session_archive_and_task_progress(monkeypatch, tmp_path):
    key_file = tmp_path / "task-1.key"
    key_file.write_text("secret-key-" + ("x" * 32), encoding="utf-8")
    key_file.chmod(0o600)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path in {
            "/api/workspaces/sessions/s-old",
            "/api/workspaces/tasks/task-1",
        }:
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "ws-1"}])
        if request.url.path == "/api/workspaces/ws-1/board":
            return httpx.Response(
                200,
                json={
                    "sessions": [{"id": "s-old", "workspace_id": "ws-1", "archived": False}],
                    "tasks": [
                        {
                            "id": "task-1",
                            "workspace_id": "ws-1",
                            "execution_epoch": 3,
                            "progress_revision": 7,
                        }
                    ],
                },
            )
        if request.url.path == "/api/workspaces/ws-1/sessions/s-old/archive":
            return httpx.Response(200, json={"id": "s-old", "archived": True})
        if request.url.path == "/api/workspaces/ws-1/tasks/task-1/progress":
            payload = json.loads(request.content.decode())
            assert payload["expected_execution_epoch"] == 3
            assert payload["expected_progress_revision"] == 7
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "execution_epoch": 3,
                    "progress_revision": 8,
                    "status": "working",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    patch_get_client(monkeypatch, handler)
    arch = CliRunner().invoke(cli, ["session", "archive", "s-old"])
    assert arch.exit_code == 0, arch.output
    assert "archived s-old" in arch.output

    prog = CliRunner().invoke(
        cli,
        [
            "task",
            "progress",
            "task-1",
            "--state",
            "working",
            "--summary",
            "still going",
            "--reporter-key-file",
            str(key_file),
        ],
    )
    assert prog.exit_code == 0, prog.output


def test_remote_workspace_cwd_preserved_and_session_report_call_id(monkeypatch):
    seen_create_body: dict = {}
    seen_report_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "ws-remote", "target": "remote"}])
        if request.url.path == "/api/workspaces/ws-remote/tasks" and request.method == "POST":
            seen_create_body.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={"id": "t-rem"})
        if request.url.path == "/api/workspaces/tasks/t-rem/start" and request.method == "POST":
            return httpx.Response(503, json={"detail": "SSH tunnel busy"})
        if request.url.path == "/api/workspaces/sessions/s-1/reports" and request.method == "POST":
            seen_report_body.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={"id": "r-1"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    patch_get_client(monkeypatch, handler)
    run_res = CliRunner().invoke(
        cli,
        [
            "task",
            "run",
            "ws-remote",
            "--title",
            "Remote job",
            "--prompt",
            "Check GPU",
            "--cwd",
            "~/remote_repo",
            "--payload-json",
            '{"request_key": "custom-rk-1"}',
        ],
    )
    assert run_res.exit_code != 0
    assert seen_create_body["cwd"] == "~/remote_repo"
    assert seen_create_body["request_key"] == "custom-rk-1"
    assert "Task t-rem created (request_key=custom-rk-1) but start failed" in run_res.output

    rep_res = CliRunner().invoke(
        cli,
        [
            "session",
            "report",
            "s-1",
            "--state",
            "working",
            "--message",
            "step 1",
            "--call-id",
            "rep-cid-1",
        ],
    )
    assert rep_res.exit_code == 0, rep_res.output
    assert seen_report_body["call_id"] == "rep-cid-1"


@pytest.mark.asyncio
async def test_backend_task_update_agent_type_and_remote_start_cwd(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from claude_hub.models.schemas import (
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        SessionKind,
        StartTaskRequest,
        Workspace,
        WorkspaceSessionRole,
        WorkspaceTaskCreate,
        WorkspaceTaskRecipe,
        WorkspaceTaskUpdate,
    )
    from claude_hub.services.workspace_manager import WorkspaceManager

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    wm = WorkspaceManager()
    monkeypatch.setattr(wm, "_save_state", lambda: None)
    monkeypatch.setattr(wm, "_save_workspace_state", lambda _ws_id: None)
    now = datetime.now(timezone.utc)
    local_dir = tmp_path / "repo"
    local_dir.mkdir()
    ws_local = Workspace(
        id="ws-local",
        name="local",
        target=ExecutionTarget.LOCAL,
        path=str(local_dir),
        default_branch="main",
        session_prefix="loc",
        created_at=now,
        updated_at=now,
    )
    ws_remote = Workspace(
        id="ws-remote",
        name="remote",
        target=ExecutionTarget.REMOTE,
        path="/opt/tiger/repo",
        remote_profile_id="prof-1",
        remote_cwd="/opt/tiger/repo",
        default_branch="main",
        session_prefix="rem",
        created_at=now,
        updated_at=now,
    )
    wm.workspaces[ws_local.id] = ws_local
    wm.workspaces[ws_remote.id] = ws_remote

    t_local = wm.create_task(
        ws_local.id,
        WorkspaceTaskCreate(
            title="Local task",
            prompt="Run",
            agent_type=AgentType.CODEX,
            cwd=str(local_dir),
        ),
    )
    edited = await wm.update_task(
        t_local.id,
        WorkspaceTaskUpdate(
            agent_type=AgentType.CLAUDE,
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    assert edited.agent_type == AgentType.CLAUDE
    assert edited.auto_worktree is False

    # Reject traex on a terminal task, allow joint (MASO, chat session) update,
    # and reject binding a terminal session onto a MASO task.
    t_term = wm.create_task(
        ws_local.id,
        WorkspaceTaskCreate(
            title="Term task",
            prompt="Run",
            agent_type=AgentType.CODEX,
            session_kind=SessionKind.TERMINAL,
        ),
    )
    with pytest.raises(ValueError, match="TraeX is not supported"):
        await wm.update_task(t_term.id, WorkspaceTaskUpdate(agent_type=AgentType.TRAEX))
    maso_sess = ManagedSession(
        id="s-maso-1",
        workspace_id=ws_local.id,
        workspace_path=str(local_dir),
        role=WorkspaceSessionRole.ORCHESTRATOR,
        title="MASO worker",
        tab_id="tab-maso-1",
        tmux_session="",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        created_at=now,
        updated_at=now,
    )
    term_sess = maso_sess.model_copy(
        update={
            "id": "s-term-1",
            "tab_id": "tab-term-1",
            "agent_type": AgentType.CODEX,
            "session_kind": SessionKind.TERMINAL,
        }
    )
    wm.sessions[maso_sess.id] = maso_sess
    wm.sessions[term_sess.id] = term_sess
    upgraded = await wm.update_task(
        t_term.id,
        WorkspaceTaskUpdate(agent_type=AgentType.MASO, session_id="s-maso-1"),
    )
    assert upgraded.agent_type == AgentType.MASO
    assert upgraded.session_kind == SessionKind.CHAT
    with pytest.raises(ValueError, match="MASO is an HTTP Chat provider"):
        await wm.update_task(
            upgraded.id,
            WorkspaceTaskUpdate(session_id="s-term-1"),
        )

    t_remote = wm.create_task(
        ws_remote.id,
        WorkspaceTaskCreate(
            title="Remote task",
            prompt="Run",
            agent_type=AgentType.CODEX,
            session_kind=SessionKind.TERMINAL,
            cwd="~/remote_sub",
        ),
    )
    captured_ensure = []

    async def fake_ensure(workspace_id, payload, *, bootstrap=True):
        captured_ensure.append((workspace_id, payload))
        sess = ManagedSession(
            id="s-rem-1",
            workspace_id=workspace_id,
            workspace_path=payload.remote_cwd or "/opt/tiger/repo",
            remote_cwd=payload.remote_cwd,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            title="Remote worker",
            tab_id="tab-rem-1",
            tmux_session="tmux-rem-1",
            agent_type=payload.agent_type,
            session_kind=payload.session_kind,
            target=ExecutionTarget.REMOTE,
            ephemeral=True,
            caller_owned_ephemeral=True,
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            created_at=now,
            updated_at=now,
        )
        wm.sessions[sess.id] = sess
        return sess

    monkeypatch.setattr(wm, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(wm, "dispatch_workspace", AsyncMock())
    started = await wm.start_task(t_remote.id, StartTaskRequest())
    assert started.session_id == "s-rem-1"
    assert len(captured_ensure) == 1
    assert captured_ensure[0][1].remote_cwd == "~/remote_sub"
    assert captured_ensure[0][1].cwd is None
