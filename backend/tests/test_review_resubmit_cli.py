"""The supported raw API escape hatch preserves reviewer reports and errors."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli


@pytest.mark.parametrize("status", [201, 400, 404, 409])
def test_plan_review_resubmit_uses_canonical_authenticated_endpoint(monkeypatch, status):
    payload = {
        "task_id": "task-plan-gate",
        "state": "review_passed",
        "call_id": "task-plan-gate-review-passed-cycle-1",
        "message": "Plan approved; verdict recovered from reviewer log.",
        "message_en": "Plan approved; verdict recovered from reviewer log.",
        "message_zh": "计划已批准；从评审日志恢复结论。",
        "validation": "Original reviewer verdict and current review round checked.",
        "risks": "No implementation acceptance implied.",
    }
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.method == "POST"
        assert str(request.url) == ("http://[::1]:8173/api/workspaces/sessions/reviewer-4/reports")
        assert "claude_hub_session=test-session-token" in request.headers["cookie"]
        assert json.loads(request.content) == payload
        return httpx.Response(
            status,
            json={"id": "report-1", **payload} if status == 201 else {"detail": "rejected"},
        )

    def make_client(**kwargs: Any) -> HubClient:
        return HubClient(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli_main, "HubClient", make_client)
    # An explicit server must win over stale dispatch/environment configuration.
    monkeypatch.setenv("CLAUDE_HUB_URL", "http://localhost:11004")
    result = CliRunner().invoke(
        cli,
        [
            "--base-url",
            "http://[::1]:8173",
            "--token",
            "test-session-token",
            "--json",
            "api",
            "raw",
            "POST",
            "/api/workspaces/sessions/reviewer-4/reports",
            "--payload-json",
            json.dumps(payload),
        ],
    )
    assert len(captured) == 1  # No silent alternate-path or retry mutation.
    if status == 201:
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["call_id"] == payload["call_id"]
    else:
        assert result.exit_code != 0
        assert "rejected" in result.output
