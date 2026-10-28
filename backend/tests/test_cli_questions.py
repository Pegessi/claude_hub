"""Question CLI preserves exact identity and never invokes task/schedule commands."""

import json

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli


@pytest.fixture
def captured(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )
    return requests


def test_question_cli_list_and_answer_stdin(captured):
    runner = CliRunner()
    result = runner.invoke(cli, ["tab", "question", "list", "target"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli,
        [
            "tab",
            "question",
            "answer",
            "target",
            "--question-ref",
            "exact-q",
            "--answers-file",
            "-",
            "--call-id",
            "retry-1",
        ],
        input='["Skip"]',
    )
    assert result.exit_code == 0, result.output
    assert [r.url.path for r in captured] == [
        "/api/workspaces/tabs/target/questions",
        "/api/workspaces/tabs/target/questions/exact-q/answers",
    ]
    assert json.loads(captured[-1].content) == {"call_id": "retry-1", "selected": ["Skip"]}


def test_dot_notify_existing_question_needs_no_text(captured):
    result = CliRunner().invoke(cli, ["dot", "notify", "--question-ref", "exact-q"])
    assert result.exit_code == 0, result.output
    assert captured[0].url.path == "/api/dot/notify"
    assert json.loads(captured[0].content)["question_ref"] == "exact-q"
