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
    # A link to a card deleted since does not lock the card against edits.
    other = client.post("/api/dot/cards", json={"title": "Other"}).json()["id"]
    linker = client.post("/api/dot/cards", json={"title": "Linker", "links": [other]}).json()
    assert client.delete(f"/api/dot/cards/{other}", params={"hard": True}).status_code == 200
    edited = client.post(
        "/api/dot/cards",
        json={"id": f" {linker['id']} ", "title": "Linker", "links": [other], "summary": "new"},
    )
    assert edited.status_code == 200, edited.text
    # Spaces around the id still update the same card.
    assert client.get(f"/api/dot/cards/{linker['id']}").json()["summary"] == "new"
    assert client.delete(f"/api/dot/cards/{linker['id']}", params={"hard": True}).status_code == 200

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

    # A full tab list is the user's to fix, not a server error.
    from claude_hub.services.ttyd_manager import TabLimitExceededError

    async def full(**kwargs: Any) -> None:
        raise TabLimitExceededError("Too many tabs")

    tabs.clear()
    monkeypatch.setattr(ttyd_manager, "create_tab", full)
    limited = client.post("/api/dot/setup", json={"env": {"X": "1"}})
    assert limited.status_code == 429 and limited.json()["detail"] == "Too many tabs"


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


def test_notify_reports_whether_feishu_got_it(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return text != "unbound"

    monkeypatch.setattr(service, "notify", fake_notify)
    result = CliRunner().invoke(cli, ["--json", "dot", "notify", "Paper task is done"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"sent": True}
    missed = client.post("/api/dot/notify", json={"text": "unbound"}).json()
    assert missed["sent"] is False and "Feishu" in missed["reason"]
    assert client.post("/api/dot/notify", json={"text": "  "}).status_code == 422
    assert client.post("/api/dot/notify", json={"text": "x" * 2001}).status_code == 422
    assert sent == ["Paper task is done", "unbound"]


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
    # An empty --id (an unset shell variable) must not quietly make a new card.
    cards = len(service.store.list_cards())
    blank = runner.invoke(cli, ["dot", "remember", "--id", "", "--title", "t", "--summary", "s"])
    assert blank.exit_code != 0 and "--id is empty" in blank.output
    assert len(service.store.list_cards()) == cards
    locked = tmp_path / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    unreadable = runner.invoke(cli, ["dot", "remember", "--title", "t", "--file", str(locked)])
    assert unreadable.exit_code == 1 and "Cannot read" in unreadable.output


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

    # A bad picture path fails before anything changes.
    result = CliRunner().invoke(
        cli, ["dot", "identity", "--name", "Jarvis", "--avatar-image", str(tmp_path / "no.png")]
    )
    assert result.exit_code == 1 and "Cannot read" in result.output
    text = tmp_path / "notes.txt"
    text.write_text("not a picture")
    result = CliRunner().invoke(
        cli, ["dot", "identity", "--name", "Jarvis", "--avatar-image", str(text)]
    )
    assert result.exit_code == 1
    assert client.get("/api/dot/identity").json()["name"] == "Friday"


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
        if outcome.get("broken"):
            raise RuntimeError("tmux server is gone")
        return SimpleNamespace(id=task_id, title="Paper", status=SimpleNamespace(value="queued"))

    monkeypatch.setattr(workspace_manager, "register_task", fake_register)
    monkeypatch.setattr(workspace_manager, "start_task", fake_start)
    result = CliRunner().invoke(
        cli,
        [
            "dot", "delegate", "--workspace", "ws", "--title", "Paper", "--goal", "Summarize",
            "--done-when", "done", "--stall-hours", "3",
        ],
    )  # fmt: skip
    assert result.exit_code == 1, "a weak brief is held"
    assert "Not sent" in result.output and "No output" in result.output
    assert service.store.get_work("task-5") is None

    result = CliRunner().invoke(
        cli,
        [
            "dot", "delegate", "--workspace", "ws", "--title", "Paper", "--goal", "Summarize",
            "--done-when", "notes/fa3.md has 5 bullets", "--done-when", "each cites a section",
            "--output", "notes/fa3.md", "--context", "arxiv 2407.08608", "--stall-hours", "3",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "delegated task-5 (queued): Paper" in result.output
    work = service.store.get_work("task-5")
    assert work is not None and work["stall_hours"] == 3 and len(work["done_when"]) == 2

    body: dict[str, Any] = {
        "workspace_id": "missing",
        "title": "x",
        "goal": "y",
        "done_when": ["z"],
        "confirm": True,
    }
    assert client.post("/api/dot/delegate", json={**body, "done_when": []}).status_code == 422
    held = client.post("/api/dot/delegate", json={**body, "confirm": False}).json()
    assert held["held"] and held["work"] is None
    assert client.post("/api/dot/delegate", json=body).status_code == 404
    outcome["conflict"] = True
    body["workspace_id"] = "ws"
    assert client.post("/api/dot/delegate", json=body).status_code == 409
    # A task that was made but did not start is a client error, and still tracked.
    outcome.update(conflict=False, broken=True)
    failed = client.post("/api/dot/delegate", json={**body, "title": "Broken"})
    assert failed.status_code == 400 and "task-5" in failed.json()["detail"]
    assert service.store.get_work("task-5")["title"] == "Broken"  # type: ignore[index]
    outcome["broken"] = False
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
    # A write cut off mid-character must not break the day.
    with service.store.journal_path(journal["day"]).open("ab") as stream:
        stream.write("half é".encode()[:-1])
    cut = client.get("/api/dot/journal")
    assert cut.status_code == 200 and "hello Dot" in cut.json()["text"]
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


def test_review_and_lessons_routes_and_cli(
    api: tuple[TestClient, DotService], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    _route_to_app(client, monkeypatch)
    store = service.store
    store.upsert_work("task-1", workspace_id="ws", title="Paper", status="working")
    review = {"verdict": "redo", "cause": "brief", "lesson": "Say the output path"}

    assert client.post("/api/dot/work/nope/review", json=review).status_code == 404
    assert client.post("/api/dot/work/task-1/review", json=review).status_code == 400
    assert client.post("/api/dot/work/task-1/review", json={"verdict": "meh"}).status_code == 422
    store.upsert_work("task-1", status="review", result_card="c-1", review_due=True)
    assert client.get("/api/dot/lessons").json()["stats"]["pending"] == 1
    unknown = {"verdict": "redo", "cause": "brief", "lesson_id": "l-none"}
    missing = client.post("/api/dot/work/task-1/review", json=unknown)
    assert missing.status_code == 404 and "Lesson not found" in missing.json()["detail"]
    too_long = {**review, "lesson": "x" * 201}
    assert client.post("/api/dot/work/task-1/review", json=too_long).status_code == 422

    result = CliRunner().invoke(
        cli,
        ["dot", "review", "task-1", "--verdict", "redo", "--cause", "brief",
         "--lesson", "Say the output path"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (
        "reviewed task-1: redo (brief)" in result.output and "Say the output path" in result.output
    )
    lessons = client.get("/api/dot/lessons").json()
    assert lessons["stats"] == {
        "reviewed": 1,
        "first_pass": 0,
        "rate": 0.0,
        "causes": {"brief": 1},
        "pending": 0,
        "judged": 1,
        "returned": 1,
        "confirmed": 0,
        "confirmed_first_pass": 0,
        "window": 20,
    }
    lesson_id = lessons["lessons"][0]["id"]
    assert lessons["lessons"][0]["in_session"] and lessons["lessons"][0]["reach"] == "always"

    added = client.post("/api/dot/lessons", json={"rule": "Give the output location"}).json()
    assert client.post("/api/dot/lessons", json={"rule": ""}).status_code == 422
    edited = client.patch(f"/api/dot/lessons/{added['id']}", json={"rule": "Name the output"})
    assert edited.json()["rule"] == "Name the output"
    assert client.patch("/api/dot/lessons/l-none", json={"rule": "x"}).status_code == 404
    merge = {"keep": lesson_id, "others": [added["id"]]}
    assert client.post("/api/dot/lessons/merge", json=merge).json()["votes"] == 2
    bad_merge = {"keep": lesson_id, "others": ["l-none"]}
    assert client.post("/api/dot/lessons/merge", json=bad_merge).status_code == 404
    into_merged = {"keep": added["id"], "others": [lesson_id]}
    assert client.post("/api/dot/lessons/merge", json=into_merged).status_code == 400

    store.upsert_work("task-1", brief_warnings=["No context"])
    listed = CliRunner().invoke(cli, ["dot", "lessons"])
    assert listed.exit_code == 0, listed.output
    assert "sent past brief warnings: first pass 0/1" in listed.output
    assert "first pass: 0/1" in listed.output and lesson_id in listed.output
    assert "(misses: brief 1)" in listed.output and "judged 1/1 returned" in listed.output
    assert added["id"] not in listed.output
    assert added["id"] in CliRunner().invoke(cli, ["dot", "lessons", "--all"]).output

    assert CliRunner().invoke(cli, ["dot", "lessons", "drop", lesson_id]).exit_code == 0
    assert client.get("/api/dot/lessons").json()["lessons"] == []
    store.upsert_work("task-2", workspace_id="ws", status="review", result_card="c-2",
                      review_due=True)  # fmt: skip
    revive = {"verdict": "redo", "cause": "brief", "lesson_id": added["id"]}
    dropped = client.post("/api/dot/work/task-2/review", json=revive)
    assert dropped.status_code == 400 and "dropped" in dropped.json()["detail"]
    assert client.delete("/api/dot/lessons/l-none").status_code == 404
    assert CliRunner().invoke(cli, ["dot", "lessons", "add", "Split big goals"]).exit_code == 0
    assert [row["rule"] for row in client.get("/api/dot/lessons").json()["lessons"]] == [
        "Split big goals"
    ]


def test_odd_paths_and_titles_are_client_errors(api: tuple[TestClient, DotService]) -> None:
    client, _ = api
    # A NUL byte or an over-long name is "no such file", not a server error.
    assert client.get("/api/dot/files/a%00b.png").status_code == 404
    assert client.get("/api/dot/files/" + "a" * 5000).status_code == 404
    bad_file = client.post("/api/dot/cards", json={"title": "x", "files": ["files/a\u0000b"]})
    assert bad_file.status_code == 400, bad_file.text
    assert client.post("/api/dot/focus", json={"title": "   "}).status_code == 400
    item = client.post("/api/dot/focus", json={"title": "Real"}).json()
    assert client.patch(f"/api/dot/focus/{item['id']}", json={"title": "  "}).status_code == 400
    assert client.get("/api/dot/focus").json()[0]["title"] == "Real"


def test_long_ids_and_a_full_focus_are_client_errors(
    api: tuple[TestClient, DotService],
) -> None:
    client, _ = api
    assert client.get("/api/dot/cards/" + "a" * 300).status_code == 404
    bad_link = client.post("/api/dot/cards", json={"title": "x", "links": ["a" * 300]})
    assert bad_link.status_code == 400, bad_link.text
    ids = [
        client.post("/api/dot/focus", json={"title": f"Goal {i}"}).json()["id"] for i in range(7)
    ]
    client.patch(f"/api/dot/focus/{ids[0]}", json={"status": "dropped"})
    assert client.post("/api/dot/focus", json={"title": "Goal 7"}).status_code == 200
    back = client.patch(f"/api/dot/focus/{ids[0]}", json={"status": "active"})
    assert back.status_code == 400 and "full" in back.text
    assert len(client.get("/api/dot/focus").json()) == 7
