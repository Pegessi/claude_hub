"""Dot REST API and the ``claude-hub dot`` CLI, wired end to end."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
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
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.dot import DotService, DotStore, reset_dot_service
from claude_hub.services.ttyd_manager import DEFAULT_CLAUDE_LAUNCH_ENV
from claude_hub.services.workspace_manager._task_execution import TaskExecutionConflict


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


def test_setup_passes_provider_env_to_the_chat_tab(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    calls: list[tuple[str, Any]] = []
    tabs: dict[str, SimpleNamespace] = {}

    async def fake_create_tab(**kwargs: Any) -> SimpleNamespace:
        calls.append(("create", kwargs["env"]))
        tabs["t-1"] = SimpleNamespace(
            id="t-1",
            archived=False,
            env=kwargs["env"] or {},
            session_kind=SimpleNamespace(value="terminal"),
        )
        return tabs["t-1"]

    async def fake_switch_env(tab_id: str, env: dict[str, str], **_: Any) -> SimpleNamespace:
        calls.append(("switch", env))
        tabs[tab_id].env = env
        return tabs[tab_id]

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(ttyd_manager, "switch_env", fake_switch_env)
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: tabs.get(tab_id))
    monkeypatch.setattr(service, "ensure_schedules", lambda tab_id: None)

    first = client.post(
        "/api/dot/setup",
        json={"env_preset": "socks-proxy-1080", "env": {"ANTHROPIC_MODEL": "m"}},
    )
    assert first.status_code == 200, first.text
    assert first.json()["tab_id"] == "t-1"
    assert calls == [
        (
            "create",
            {
                "ALL_PROXY": "socks5://127.0.0.1:1080",
                "NO_PROXY": "localhost,127.0.0.1,::1",
                "ANTHROPIC_MODEL": "m",
            },
        )
    ]

    # A user-chosen env is kept; an empty or built-in fallback env gets seeded.
    assert client.post("/api/dot/setup", json={"env": {"X": "1"}}).status_code == 200
    assert len(calls) == 1
    tabs["t-1"].env = {}
    assert client.post("/api/dot/setup", json={"env": {"X": "1"}}).status_code == 200
    assert calls[-1] == ("switch", {"X": "1"})
    tabs["t-1"].env = dict(DEFAULT_CLAUDE_LAUNCH_ENV)
    assert client.post("/api/dot/setup", json={"env": {"Y": "2"}}).status_code == 200
    assert calls[-1] == ("switch", {"Y": "2"})
    assert client.post("/api/dot/setup").status_code == 200
    assert len(calls) == 3
    assert client.post("/api/dot/setup", json={"env_preset": "no-such"}).status_code == 400


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


def test_cli_setup_sends_the_env_preset(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    seen: list[dict[str, str]] = []

    async def fake_ensure_tab(env: dict[str, str]) -> str:
        seen.append(env)
        return "t-1"

    monkeypatch.setattr(service, "ensure_tab", fake_ensure_tab)
    result = CliRunner().invoke(cli, ["--json", "dot", "setup", "--env-preset", "socks-proxy-1080"])
    assert result.exit_code == 0, result.output
    assert seen == [{"ALL_PROXY": "socks5://127.0.0.1:1080", "NO_PROXY": "localhost,127.0.0.1,::1"}]


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


def test_files_are_confined_and_only_images_render_inline(
    api: tuple[TestClient, DotService],
) -> None:
    client, service = api
    html = base64.b64encode(b"<script>alert(1)</script>").decode()
    path = client.post("/api/dot/files", json={"filename": "x.html", "data_base64": html}).json()[
        "path"
    ]
    page = client.get(f"/api/dot/{path}")
    assert page.headers["content-disposition"].startswith("attachment")
    assert page.headers["content-type"] == "application/octet-stream"
    assert page.headers["x-content-type-options"] == "nosniff"
    service.store.update_state(tab_id="tab-dot")
    assert client.get("/api/dot/files/%2e%2e/state.json").status_code == 404


def test_updating_an_archived_card_keeps_it_archived(
    api: tuple[TestClient, DotService],
) -> None:
    client, _ = api
    card = client.post(
        "/api/dot/cards", json={"kind": "knowledge", "title": "Old", "summary": "s"}
    ).json()
    client.delete(f"/api/dot/cards/{card['id']}")
    update = {"id": card["id"], "kind": "knowledge", "title": "Old", "summary": "new"}
    assert client.post("/api/dot/cards", json=update).json()["archived"] is True
    assert all(item["id"] != card["id"] for item in client.get("/api/dot/cards").json())


def test_identity_and_avatar_routes(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, service = api
    monkeypatch.setattr(ttyd_manager, "rename_tab", lambda tab_id, name: True)
    assert client.get("/api/dot/identity").json()["name"] == "Dot"
    renamed = client.patch("/api/dot/identity", json={"name": "Jarvis", "avatar": "🤖"})
    assert renamed.status_code == 200, renamed.text
    assert client.get("/api/dot/status").json()["identity"]["name"] == "Jarvis"
    assert client.patch("/api/dot/identity", json={"name": " "}).status_code == 400

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    upload = client.post(
        "/api/dot/identity/avatar", json={"data_base64": base64.b64encode(png).decode()}
    )
    image = upload.json()["avatar_image"]
    assert client.get(f"/api/dot/{image}").content == png
    svg = base64.b64encode(b"<svg/>").decode()
    assert client.post("/api/dot/identity/avatar", json={"data_base64": svg}).status_code == 400
    assert client.delete("/api/dot/identity/avatar").json()["avatar_image"] is None

    _route_to_app(client, monkeypatch)
    picture = tmp_path / "me.png"
    picture.write_bytes(png)
    result = CliRunner().invoke(
        cli, ["--json", "dot", "identity", "--name", "Friday", "--avatar-image", str(picture)]
    )
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["name"] == "Friday" and shown["avatar_image"] == image


def test_watch_routes_and_cli(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services import workspace_manager

    client, service = api
    task = SimpleNamespace(
        id="task-1",
        workspace_id="ws",
        title="Old job",
        agent_type=SimpleNamespace(value="codex"),
        status=SimpleNamespace(value="working"),
    )
    monkeypatch.setattr(workspace_manager, "tasks", {"task-1": task})
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: [])

    assert client.post("/api/dot/work/nope/watch").status_code == 404
    watched = client.post("/api/dot/work/task-1/watch", json={"stall_hours": 4})
    assert watched.status_code == 200, watched.text
    assert watched.json()["stall_hours"] == 4
    assert client.post("/api/dot/work/task-1/watch", json={"stall_hours": 0}).status_code == 422
    assert client.post("/api/dot/work/task-1/unwatch").json()["closed"]
    assert client.post("/api/dot/work/nope/unwatch").status_code == 404

    _route_to_app(client, monkeypatch)
    result = CliRunner().invoke(cli, ["dot", "watch", "task-1", "--stall-hours", "1.5"])
    assert result.exit_code == 0, result.output
    assert "watching task-1" in result.output
    assert service.store.get_work("task-1")["stall_hours"] == 1.5  # type: ignore[index]
    assert CliRunner().invoke(cli, ["dot", "unwatch", "task-1"]).exit_code == 0
    assert service.store.list_work() == []


def test_delegate_route_and_cli(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    outcome: dict[str, Any] = {}

    async def fake_register(ws: str, payload: Any, actor_key: str) -> tuple[Any, bool]:
        if ws == "missing":
            raise KeyError(ws)
        task = SimpleNamespace(
            id="task-5", title=payload.title, status=SimpleNamespace(value="todo")
        )
        return task, False

    async def fake_start(task_id: str, request: Any) -> Any:
        if outcome.get("conflict"):
            raise TaskExecutionConflict("task_is_initiator_controlled")
        return SimpleNamespace(id=task_id, title="Paper", status=SimpleNamespace(value="queued"))

    monkeypatch.setattr(workspace_manager, "register_task", fake_register)
    monkeypatch.setattr(workspace_manager, "start_task", fake_start)
    result = CliRunner().invoke(
        cli,
        [
            "dot", "delegate", "--workspace", "ws", "--title", "Paper", "--goal", "Summarize",
            "--done-when", "5 bullets", "--stall-hours", "3",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "delegated task-5 (queued): Paper" in result.output
    assert service.store.get_work("task-5")["stall_hours"] == 3  # type: ignore[index]

    body = {"workspace_id": "missing", "title": "x", "goal": "y", "done_when": "z"}
    assert client.post("/api/dot/delegate", json=body).status_code == 404
    outcome["conflict"] = True
    body["workspace_id"] = "ws"
    assert client.post("/api/dot/delegate", json=body).status_code == 409
    assert client.post("/api/dot/delegate", json={**body, "goal": " "}).status_code == 400
    assert client.post("/api/dot/delegate", json={**body, "stall_hours": 0}).status_code == 422


def test_journal_route_and_cli_show(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    assert client.get("/api/dot/journal").json() == {"days": [], "day": None, "text": ""}
    service.store.append_journal("user", "hello Dot")
    journal = client.get("/api/dot/journal").json()
    assert journal["days"] == [journal["day"]] and "hello Dot" in journal["text"]
    assert client.get("/api/dot/journal", params={"day": "bad"}).status_code == 422

    card = client.post(
        "/api/dot/cards",
        json={
            "kind": "knowledge",
            "title": "FA3",
            "summary": "fast",
            "body": "details",
            "tags": ["gpu"],
        },
    ).json()
    result = CliRunner().invoke(cli, ["dot", "show", card["id"]])
    assert result.exit_code == 0, result.output
    assert "FA3" in result.output and "summary: fast" in result.output
    assert "tags: gpu" in result.output and result.output.rstrip().endswith("details")
