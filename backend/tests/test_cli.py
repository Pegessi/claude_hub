"""Tests for the Claude Hub CLI."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

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
    for group in (
        "auth",
        "system",
        "tab",
        "terminal",
        "filesystem",
        "fs",
        "remote",
        "clipboard",
        "api",
        "workspace",
        "task",
        "agent",
        "session",
        "lessons",
    ):
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


def test_task_send_is_followup_alias(monkeypatch):
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
        ["--json", "task", "send", "ws1", "t1", "--message", "hello", "--call-id", "send-1"],
    )
    assert result.exit_code == 0, result.output
    assert [request.url.path for request in captured] == ["/api/workspaces/ws1/tasks/t1/followup"]
    assert json.loads(captured[0].content) == {"message": "hello", "call_id": "send-1"}
    assert "call_id=send-1" in result.stderr
    assert "/board" not in captured[0].url.path
    assert "/sessions/" not in captured[0].url.path


def test_task_send_missing_task_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Task not found"})

    captured = patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "send", "ws1", "nope", "--message", "hi"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
    assert captured[0].url.path.endswith("/followup")


def test_task_get_scans_workspaces_and_includes_reports(monkeypatch):
    reports = [{"created_at": "t1", "state": "working", "message": "progress"}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "wsA"}, {"id": "wsB"}])
        if path == "/api/workspaces/wsA/board":
            return httpx.Response(200, json={"tasks": []})
        if path == "/api/workspaces/wsB/board":
            return httpx.Response(
                200,
                json={"tasks": [{"id": "t9", "title": "T", "status": "working"}]},
            )
        if path == "/api/workspaces/wsB/tasks/t9/reports":
            return httpx.Response(200, json=reports)
        raise AssertionError(f"unexpected path {path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "task", "get", "t9"])
    assert result.exit_code == 0, result.output
    detail = json.loads(result.output)
    assert detail["workspace_id"] == "wsB"
    assert detail["reports"] == reports


def test_task_report_newest_first_and_review_filter(monkeypatch):
    reports = [
        {"created_at": "t0", "state": "working", "message": "progress"},
        {
            "created_at": "t1",
            "state": "review_failed",
            "session_id": "cb-reviewer-1",
            "review_cycle": 1,
            "review_reason": "needs work",
        },
        {"created_at": "t2", "state": "review_passed", "session_id": "cb-reviewer-1"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces/wsA/board":
            return httpx.Response(200, json={"tasks": [{"id": "t9", "status": "review"}]})
        assert request.url.path == "/api/workspaces/wsA/tasks/t9/reports"
        return httpx.Response(200, json=reports)

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "task", "report", "t9", "--workspace-id", "wsA", "--limit", "2"]
    )
    assert result.exit_code == 0, result.output
    assert [r["created_at"] for r in json.loads(result.output)] == ["t2", "t1"]

    result = runner.invoke(cli, ["--json", "task", "review", "t9", "--workspace-id", "wsA"])
    assert result.exit_code == 0, result.output
    rounds = json.loads(result.output)
    assert [r["verdict"] for r in rounds] == ["review_failed", "review_passed"]
    assert rounds[0]["notes"] == "needs work"


def test_task_report_with_workspace_id_requires_task(monkeypatch):
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/workspaces/wsA/board":
            return httpx.Response(200, json={"tasks": []})
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "report", "missing", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not found in workspace wsA" in result.output
    assert calls == ["/api/workspaces/wsA/board"]


def test_task_accept_marks_review_task_done(monkeypatch):
    patches: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/workspaces/wsA/board":
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {
                            "id": "t9",
                            "status": "review",
                            "human_acceptance_requested_at": "2026-06-28T01:00:00",
                        }
                    ]
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
        return httpx.Response(200, json={"tasks": [{"id": "t9", "status": "working"}]})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "accept", "t9", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not 'review'" in result.output


def test_task_accept_rejects_review_without_human_acceptance(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"tasks": [{"id": "t9", "status": "review"}]})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "accept", "t9", "--workspace-id", "wsA"])
    assert result.exit_code != 0
    assert "not awaiting human acceptance" in result.output


def test_session_list_and_logs(monkeypatch):
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
        if path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "wsA"}])
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


def test_workspace_summary_and_docs_surface_board_state(monkeypatch):
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
    result = runner.invoke(cli, ["--json", "workspace", "summary", "ws1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task_counts"] == {"review": 1, "working": 1}
    assert payload["session_counts"] == {"working": 1}
    assert payload["active_tasks"][0]["latest_report_message"] == "editing"
    assert payload["snapshot_path"] == "/state/snapshot.md"

    result = runner.invoke(cli, ["workspace", "docs", "ws1"])
    assert result.exit_code == 0, result.output
    assert "snapshot: /state/snapshot.md" in result.output
    assert "Snapshot" in result.output


def test_agent_and_session_status_show_runtime(monkeypatch):
    board = {
        "sessions": [
            {
                "id": "s1",
                "role": "orchestrator",
                "agent_type": "codex",
                "status": "active",
                "runtime_status": "working",
                "current_task_id": "t1",
                "tab_id": "tab1",
                "target": "local",
                "last_activity_at": "2026-06-29T01:00:00",
            },
            {"id": "s2", "role": "reviewer", "agent_type": "claude", "runtime_status": "idle"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces/ws1/board":
            return httpx.Response(200, json=board)
        if request.url.path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "ws1"}])
        raise AssertionError(f"unexpected path {request.url.path}")

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "agent", "status", "ws1", "--role", "orchestrator"])
    assert result.exit_code == 0, result.output
    agents = json.loads(result.output)
    assert agents == [
        {
            "id": "s1",
            "role": "orchestrator",
            "agent_type": "codex",
            "status": "active",
            "runtime_status": "working",
            "current_task_id": "t1",
            "tab_id": "tab1",
            "last_activity_at": "2026-06-29T01:00:00",
        }
    ]

    result = runner.invoke(cli, ["session", "status", "s1"])
    assert result.exit_code == 0, result.output
    assert "runtime_status" in result.output
    assert "working" in result.output
    assert "tab1" in result.output


def test_task_status_surfaces_goal_review_and_acceptance(monkeypatch):
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
    assert bodies[0]["remote_profile_id"] == "prod"
    assert bodies[0]["remote_cwd"] == "/srv/app"
    assert bodies[0]["env"] == {"A": "B"}


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
    assert bodies[0] == payload


def test_lessons_summarize(monkeypatch):
    bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/workspaces/ws1/lessons/summarize"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "run1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["lessons", "summarize", "ws1", "--mode", "full", "--limit", "10", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0] == {"mode": "full", "limit": 10, "force": True}


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
    result = runner.invoke(cli, ["lessons", "delete", "ws1", "l1"])
    assert result.exit_code == 0, result.output
    assert paths[0] == "/api/workspaces/ws1/lessons/l1"
    assert methods[0] == "DELETE"
    if status == 204:
        assert "deleted l1" in result.output
