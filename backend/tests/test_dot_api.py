"""Dot REST API and the ``claude-hub dot`` CLI, wired end to end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Generator

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models.schemas import User
from claude_hub.services.dot import DotService, DotStore, reset_dot_service


@pytest.fixture()
def api(tmp_path: Path) -> Generator[tuple[TestClient, DotService], None, None]:
    service = DotService(DotStore(tmp_path / "dot"))
    service.store.ensure()
    reset_dot_service(service)

    async def fake_current_user() -> User:
        return User(open_id="local", name="Local User", email="local@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    yield TestClient(app), service
    app.dependency_overrides.pop(get_current_user, None)
    reset_dot_service(None)


def test_card_lifecycle(api: tuple[TestClient, DotService]) -> None:
    client, service = api
    created = client.post(
        "/api/dot/cards",
        json={"kind": "user", "title": "Prefers short answers", "summary": "Simple words"},
    )
    assert created.status_code == 200, created.text
    card_id = created.json()["id"]
    assert client.get(f"/api/dot/cards/{card_id}").json()["summary"] == "Simple words"
    assert client.get("/api/dot/recall", params={"q": "short answers"}).json()[0]["id"] == card_id

    assert client.post("/api/dot/cards", json={"id": "nope", "title": "x"}).status_code == 404
    bad_link = client.post("/api/dot/cards", json={"title": "x", "links": ["missing"]})
    assert bad_link.status_code == 400

    assert client.delete(f"/api/dot/cards/{card_id}").json()["archived"]
    assert client.get("/api/dot/cards").json() == []
    assert len(client.get("/api/dot/cards", params={"include_archived": True}).json()) == 1


def test_files_focus_and_inbox(api: tuple[TestClient, DotService]) -> None:
    client, service = api
    upload = client.post("/api/dot/files", json={"filename": "a.txt", "data_base64": "aGk="})
    path = upload.json()["path"]
    assert client.get(f"/api/dot/{path}").content == b"hi"
    assert client.get("/api/dot/files/../state.json").status_code == 404
    assert (
        client.post("/api/dot/files", json={"filename": "a", "data_base64": "%%"}).status_code
        == 400
    )

    for i in range(7):
        assert client.post("/api/dot/focus", json={"title": f"Goal {i}"}).status_code == 200
    assert client.post("/api/dot/focus", json={"title": "Too many"}).status_code == 409
    first = client.get("/api/dot/focus").json()[0]
    done = client.patch(f"/api/dot/focus/{first['id']}", json={"status": "done"})
    assert done.json()["status"] == "done"
    assert len(client.get("/api/dot/focus").json()) == 6
    assert len(client.get("/api/dot/focus", params={"all": True}).json()) == 7

    service.store.add_inbox("task_ready", "Paper done")
    assert len(client.get("/api/dot/inbox").json()) == 1
    assert client.post("/api/dot/inbox/ack", json={}).json()["acked"] == 1
    assert client.get("/api/dot/inbox").json() == []


def test_status_rotate_and_health(api: tuple[TestClient, DotService]) -> None:
    client, service = api
    status = client.get("/api/dot/status").json()
    assert status["set_up"] is False and status["home"] == str(service.store.home)
    assert client.post("/api/dot/rotate").json() == {"rotate_requested": True}
    assert service.store.read_state()["rotate_requested"] is True
    health = client.get("/api/dot/health").json()
    assert {check["name"] for check in health["checks"]} >= {"focus_size", "index", "git"}


def _route_to_app(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's HTTP client at the in-process FastAPI app."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            str(request.url.copy_with(scheme="http", host="testserver")),
            content=request.content,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    def fake_get_client(ctx: Any) -> HubClient:
        return HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)


def test_cli_remember_recall_and_merge(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    runner = CliRunner()
    note = tmp_path / "diagram.png"
    note.write_bytes(b"\x89PNG fake")

    saved = runner.invoke(
        cli,
        [
            "--json",
            "dot",
            "remember",
            "--kind",
            "knowledge",
            "--title",
            "FlashAttention 3",
            "--summary",
            "Fast attention on H100",
            "--tag",
            "paper",
            "--file",
            str(note),
        ],
    )
    assert saved.exit_code == 0, saved.output
    card = json.loads(saved.output)
    assert card["files"] and card["files"][0].endswith(".png")

    # --id keeps every field that is not given.
    updated = runner.invoke(cli, ["--json", "dot", "remember", "--id", card["id"], "--tag", "gpu"])
    assert updated.exit_code == 0, updated.output
    merged = json.loads(updated.output)
    assert merged["summary"] == "Fast attention on H100"
    assert merged["tags"] == ["gpu", "paper"] and merged["files"] == card["files"]

    recall = runner.invoke(cli, ["--json", "dot", "recall", "H100"])
    assert json.loads(recall.output)[0]["id"] == card["id"]

    focus = runner.invoke(cli, ["dot", "focus", "add", "Ship Dot", "--priority", "1"])
    assert focus.exit_code == 0, focus.output
    assert service.store.list_focus()[0]["title"] == "Ship Dot"

    missing = runner.invoke(cli, ["dot", "remember", "--summary", "no title"])
    assert missing.exit_code != 0 and "--title is required" in missing.output
    gone = runner.invoke(cli, ["dot", "show", "no-such-card"])
    assert gone.exit_code != 0
