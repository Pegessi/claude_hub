"""Hub subagent service and API contracts with a faked Chat runtime (no providers)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

from claude_hub.api import subagents as subagents_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import AgentType, SessionKind, TerminalTab, User
from claude_hub.services import subagents as subagents_service
from claude_hub.services.subagents import (
    SUBAGENT_PREAMBLE,
    SubagentInteractionRequired,
    SubagentRuntime,
    SubagentService,
)
from claude_hub.services.terminal_runtime.retirement_listeners import notify_tab_retired

Behavior = Callable[[str, str, str, float], Awaitable[str]]


class FakeChats:
    """Stands in for ttyd_manager + dispatch_tab_chat_and_wait."""

    def __init__(self) -> None:
        self.tabs: Dict[str, TerminalTab] = {}
        self.create_calls: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.cancelled_turns: List[str] = []
        self.behavior: Behavior = self.answer
        self.counter = 0
        self.create_delay = 0.0

    def add_tab(
        self, tab_id: str, *, cwd: str, archived: bool = False, **extra: Any
    ) -> TerminalTab:
        tab = TerminalTab(
            id=tab_id,
            name=tab_id,
            cwd=cwd,
            port=0,
            created_at=datetime.now(timezone.utc),
            is_active=True,
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            archived=archived,
            **extra,
        )
        self.tabs[tab_id] = tab
        return tab

    async def create_tab(self, **kwargs: Any) -> TerminalTab:
        self.create_calls.append(kwargs)
        self.counter += 1
        tab_id = f"sub-tab-{self.counter}"
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        return self.add_tab(
            tab_id,
            cwd=kwargs["cwd"],
            solo_mode=kwargs["solo_mode"],
            parent_tab_id=kwargs.get("parent_tab_id"),
            subagent_id=kwargs.get("subagent_id"),
        )

    async def archive_tab(self, tab_id: str) -> None:
        tab = self.tabs[tab_id]
        self.tabs[tab_id] = tab.model_copy(update={"archived": True})
        notify_tab_retired(tab_id)  # the real manager notifies after persisting

    def delete_tab(self, tab_id: str) -> None:
        self.tabs.pop(tab_id)
        notify_tab_retired(tab_id)

    async def run_turn(
        self, tab_id: str, text: str, turn_id: str, visible: str, timeout: float
    ) -> str:
        self.sent.append({"tab_id": tab_id, "text": text, "turn_id": turn_id, "visible": visible})
        try:
            return await asyncio.wait_for(self.behavior(tab_id, text, turn_id, timeout), timeout)
        except asyncio.TimeoutError:
            self.cancelled_turns.append(turn_id)
            raise TimeoutError("did not complete") from None
        except asyncio.CancelledError:
            self.cancelled_turns.append(turn_id)
            raise

    @staticmethod
    async def answer(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await asyncio.sleep(0)
        return "ANSWER: " + text.rsplit("\n", 1)[-1]

    def runtime(self) -> SubagentRuntime:
        def resolve_env(preset: Optional[str], env: Dict[str, str]) -> Dict[str, str]:
            if preset == "bad":
                raise ValueError("Unknown env preset 'bad'")
            return {**({"PRESET": preset} if preset else {}), **env}

        return SubagentRuntime(
            create_tab=self.create_tab,
            run_turn=self.run_turn,
            archive_tab=self.archive_tab,
            get_tab=self.tabs.get,
            list_tabs=lambda: list(self.tabs.values()),
            resolve_env=resolve_env,
        )


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    chats = FakeChats()
    chats.add_tab("parent", cwd=str(tmp_path), solo_mode=True)
    service = SubagentService(tmp_path / "subagents.json", chats.runtime(), archive_grace_seconds=0)
    await service.start()
    monkeypatch.setattr(subagents_service, "_service", service)
    app = FastAPI()
    app.include_router(subagents_api.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local", email="local@example.test"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield chats, service, client, tmp_path
    await service.stop()


def _body(tmp_path: Path, **extra: Any) -> Dict[str, Any]:
    return {"agent_type": "codex", "prompt": "summarize X", "parent_tab_id": "parent", **extra}


async def _wait(client: httpx.AsyncClient, result: Dict[str, Any]) -> Dict[str, Any]:
    response = await client.get(
        f"/api/subagents/{result['subagent_id']}/turns/{result['turn_id']}", params={"wait": 5}
    )
    assert response.status_code == 200, response.text
    data: Dict[str, Any] = response.json()
    return data


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _eventually(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    """State saves run in a thread, so post-turn effects need real time."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_blocking_result_returns_final_answer_and_archives(env) -> None:
    chats, service, client, tmp_path = env
    created = await client.post(
        "/api/subagents", json=_body(tmp_path, model="gpt-x", effort="high")
    )
    assert created.status_code == 201, created.text
    started = created.json()
    assert started["status"] == "running"
    result = await _wait(client, started)
    assert result["status"] == "completed"
    assert result["final_text"] == "ANSWER: summarize X"
    call = chats.create_calls[0]
    assert call["session_kind"] == SessionKind.CHAT
    assert call["agent_type"] == AgentType.CODEX
    assert call["cwd"] == str(tmp_path)  # parent's cwd by default
    assert call["parent_tab_id"] == "parent"
    assert call["solo_mode"] is True  # inherited from the parent Chat
    assert call["env"] == {
        "CODEX_MODEL": "gpt-x",
        "CODEX_REASONING_EFFORT": "high",
        "CLAUDE_HUB_SUBAGENT_ID": started["subagent_id"],
    }
    # No bootstrap turn: one turn whose provider text carries a short preamble,
    # while the visible Chat text is the caller's prompt.
    assert len(chats.sent) == 1
    assert SUBAGENT_PREAMBLE in chats.sent[0]["text"]
    assert chats.sent[0]["visible"] == "summarize X"
    assert chats.sent[0]["turn_id"] == started["turn_id"]
    record = service.get(started["subagent_id"])
    await _eventually(lambda: record.closed)
    assert record.close_reason == "turn completed"
    assert chats.tabs[started["tab_id"]].archived is True
    # The record survives on disk for later `agent result` calls.
    stored = json.loads((tmp_path / "subagents.json").read_text())
    turn = stored["subagents"][started["subagent_id"]]["turns"][0]
    assert turn["status"] == "completed" and turn["final_text"] == "ANSWER: summarize X"


async def test_background_polling_and_latest_turn(env) -> None:
    chats, service, client, tmp_path = env
    gate = asyncio.Event()

    async def slow(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await gate.wait()
        return "done later"

    chats.behavior = slow
    started = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    pending = await client.get(
        f"/api/subagents/{started['subagent_id']}/turns/latest", params={"wait": 0.05}
    )
    assert pending.json()["status"] == "running"
    gate.set()
    result = await _wait(client, started)
    assert result["status"] == "completed" and result["final_text"] == "done later"


async def test_same_call_id_is_idempotent(env) -> None:
    chats, service, client, tmp_path = env
    body = _body(tmp_path, call_id="call-1")
    first, second = await asyncio.gather(
        client.post("/api/subagents", json=body), client.post("/api/subagents", json=body)
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["subagent_id"] == second.json()["subagent_id"]
    assert first.json()["turn_id"] == second.json()["turn_id"]
    await _wait(client, first.json())
    replay = await client.post("/api/subagents", json=body)
    assert replay.json()["turn_id"] == first.json()["turn_id"]
    assert replay.json()["status"] == "completed"
    assert len(chats.create_calls) == 1 and len(chats.sent) == 1
    conflict = await client.post("/api/subagents", json={**body, "prompt": "other"})
    assert conflict.status_code == 409


async def test_kept_subagent_follow_up_and_close(env) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    assert (await _wait(client, started))["status"] == "completed"
    await _settle()
    assert service.get(started["subagent_id"]).closed is False
    url = f"/api/subagents/{started['subagent_id']}/turns"
    follow = await client.post(url, json={"prompt": "and Y?", "call_id": "f-1"})
    assert follow.status_code == 201, follow.text
    again = await client.post(url, json={"prompt": "and Y?", "call_id": "f-1"})
    assert again.json()["turn_id"] == follow.json()["turn_id"]
    result = await _wait(client, follow.json())
    assert result["final_text"] == "ANSWER: and Y?"
    # Same conversation tab, and follow-ups carry no preamble.
    assert [s["tab_id"] for s in chats.sent] == [started["tab_id"]] * 2
    assert chats.sent[1]["text"] == "and Y?"
    listed = (await client.get("/api/subagents", params={"parent_tab_id": "parent"})).json()
    assert [row["id"] for row in listed] == [started["subagent_id"]]
    assert listed[0]["turn_count"] == 2
    closed = await client.post(f"/api/subagents/{started['subagent_id']}/close")
    assert closed.json()["closed"] is True
    assert chats.tabs[started["tab_id"]].archived is True
    refused = await client.post(url, json={"prompt": "more"})
    assert refused.status_code == 409


async def test_follow_up_requires_keep(env) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    await _wait(client, started)
    response = await client.post(
        f"/api/subagents/{started['subagent_id']}/turns", json={"prompt": "again"}
    )
    assert response.status_code == 409


async def test_timeout_cancels_turn(env) -> None:
    chats, service, client, tmp_path = env

    async def never(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await asyncio.Event().wait()
        return ""

    chats.behavior = never
    started = (
        await client.post("/api/subagents", json=_body(tmp_path, timeout_seconds=0.05))
    ).json()
    result = await _wait(client, started)
    assert result["status"] == "timeout"
    assert started["turn_id"] in chats.cancelled_turns
    await _eventually(lambda: chats.tabs[started["tab_id"]].archived)


async def test_cancel_running_turn(env) -> None:
    chats, service, client, tmp_path = env

    async def never(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await asyncio.Event().wait()
        return ""

    chats.behavior = never
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _settle()
    cancelled = await client.post(f"/api/subagents/{started['subagent_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["turn"]["status"] == "cancelled"
    assert started["turn_id"] in chats.cancelled_turns
    # Kept subagents stay open after a cancelled turn.
    assert service.get(started["subagent_id"]).closed is False


async def test_failed_turn_reports_error(env) -> None:
    chats, service, client, tmp_path = env

    async def boom(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        raise RuntimeError("provider exploded")

    chats.behavior = boom
    started = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    result = await _wait(client, started)
    assert result["status"] == "failed" and result["error"] == "provider exploded"


@pytest.mark.parametrize("retire", ["archive", "delete"])
async def test_parent_retirement_closes_subagents(env, retire: str) -> None:
    chats, service, client, tmp_path = env

    async def never(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await asyncio.Event().wait()
        return ""

    chats.behavior = never
    running = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    await _settle()
    chats.behavior = chats.answer
    kept = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _wait(client, kept)
    if retire == "archive":
        await chats.archive_tab("parent")
    else:
        chats.delete_tab("parent")
    for _ in range(50):
        if service.get(running["subagent_id"]).closed and service.get(kept["subagent_id"]).closed:
            break
        await asyncio.sleep(0.01)
    assert service.get(running["subagent_id"]).close_reason == "parent tab archived or deleted"
    assert service.get(kept["subagent_id"]).closed is True
    assert (await _wait(client, running))["status"] == "cancelled"
    assert chats.tabs[running["tab_id"]].archived and chats.tabs[kept["tab_id"]].archived


async def test_validation_errors(env) -> None:
    chats, service, client, tmp_path = env
    cases: List[tuple[Dict[str, Any], int]] = [
        ({"agent_type": "terminal"}, 400),
        ({"agent_type": "nope"}, 422),
        ({"prompt": "   "}, 422),
        ({"effort": "high", "agent_type": "claude"}, 400),
        ({"effort": "galactic"}, 400),
        ({"env_preset": "bad"}, 400),
        ({"parent_tab_id": "missing"}, 404),
        ({"parent_tab_id": None, "cwd": "relative/dir"}, 400),
        ({"parent_tab_id": None}, 400),
        ({"timeout_seconds": 0}, 422),
    ]
    for extra, status in cases:
        response = await client.post("/api/subagents", json=_body(tmp_path, **extra))
        assert response.status_code == status, (extra, response.text)
    chats.add_tab("old", cwd=str(tmp_path), archived=True)
    response = await client.post("/api/subagents", json=_body(tmp_path, parent_tab_id="old"))
    assert response.status_code == 409
    assert chats.create_calls == []
    missing = await client.get("/api/subagents/sa-nope/turns/latest")
    assert missing.status_code == 404


async def test_restart_fails_inflight_turns_and_closes(tmp_path: Path) -> None:
    chats = FakeChats()
    chats.add_tab("parent", cwd=str(tmp_path))
    gate = asyncio.Event()

    async def slow(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await gate.wait()
        return "x"

    chats.behavior = slow
    path = tmp_path / "subagents.json"
    first = SubagentService(path, chats.runtime())
    await first.start()
    record, turn = await first.create(
        subagents_service.SubagentCreate(
            agent_type=AgentType.CODEX, prompt="p", parent_tab_id="parent"
        )
    )
    # Simulate a crash: the task dies without recording a result.
    task = first._tasks[turn.turn_id]
    first._shutting_down = True
    task.cancel()
    await asyncio.wait([task])
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "subagents": {record.id: {**record.model_dump(mode="json"), "closed": False}},
            }
        )
    )
    stored = json.loads(path.read_text())
    stored["subagents"][record.id]["turns"][0]["status"] = "running"
    path.write_text(json.dumps(stored))
    # An orphan tab from a crash between tab creation and record update.
    chats.add_tab("orphan", cwd=str(tmp_path), subagent_id="sa-unknown")
    second = SubagentService(path, chats.runtime())
    await second.start()
    restored, restored_turn = await second.wait_turn(record.id, None)
    assert restored_turn.status.value == "failed"
    assert "restarted" in (restored_turn.error or "")
    assert restored.closed is True  # not kept → archived on recovery
    assert chats.tabs["orphan"].archived is True
    await second.stop()


def test_tab_record_parent_link_round_trip_and_legacy_normalization() -> None:
    from claude_hub.services.ttyd_manager import TTYDProcess

    linked = TTYDProcess(
        tab_id="child", port=1, name="child", parent_tab_id="parent", subagent_id="sa-1"
    )
    assert linked.to_dict()["parent_tab_id"] == "parent"
    assert linked.to_schema().subagent_id == "sa-1"
    # Records written before the link existed (missing or malformed) read as None.
    legacy = TTYDProcess(tab_id="old", port=2, name="old", parent_tab_id="", subagent_id=None)
    assert legacy.parent_tab_id is None and legacy.to_schema().parent_tab_id is None


# ── review follow-ups ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("parent_solo", "requested", "expected"),
    [(True, None, True), (False, None, False), (False, True, True), (True, False, False)],
)
async def test_solo_mode_inherits_parent_unless_overridden(
    env, parent_solo: bool, requested: Optional[bool], expected: bool
) -> None:
    chats, service, client, tmp_path = env
    chats.add_tab("parent", cwd=str(tmp_path), solo_mode=parent_solo)
    extra = {} if requested is None else {"solo_mode": requested}
    started = (await client.post("/api/subagents", json=_body(tmp_path, **extra))).json()
    assert chats.create_calls[-1]["solo_mode"] is expected
    assert service.get(started["subagent_id"]).solo_mode is expected


async def test_solo_mode_defaults_on_without_parent(env) -> None:
    chats, service, client, tmp_path = env
    body = _body(tmp_path, parent_tab_id=None, cwd=str(tmp_path))
    assert (await client.post("/api/subagents", json=body)).status_code == 201
    assert chats.create_calls[-1]["solo_mode"] is True


@pytest.mark.parametrize("solo", [False, True])
async def test_interaction_required_explains_how_to_proceed(env, solo: bool) -> None:
    chats, service, client, tmp_path = env

    async def asks(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        raise SubagentInteractionRequired("Chat turn requires an approval or answer in Web Chat")

    chats.behavior = asks
    started = (await client.post("/api/subagents", json=_body(tmp_path, solo_mode=solo))).json()
    result = await _wait(client, started)
    assert result["status"] == "failed"
    assert started["tab_id"] in result["error"] and "--keep" in result["error"]
    assert ("Re-run with --solo" in result["error"]) is (not solo)


async def test_nesting_depth_is_limited(env) -> None:
    chats, service, client, tmp_path = env
    chats.add_tab("child", cwd=str(tmp_path), subagent_id="sa-a", parent_tab_id="parent")
    chats.add_tab("grandchild", cwd=str(tmp_path), subagent_id="sa-b", parent_tab_id="child")
    allowed = await client.post("/api/subagents", json=_body(tmp_path, parent_tab_id="child"))
    assert allowed.status_code == 201, allowed.text
    refused = await client.post("/api/subagents", json=_body(tmp_path, parent_tab_id="grandchild"))
    assert refused.status_code == 400
    assert "nesting limit" in refused.json()["detail"]
    assert len(chats.create_calls) == 1


@pytest.mark.parametrize("identity", ["tab", "subagent-id"])
async def test_subagent_caller_cannot_escape_depth_or_parent(env, identity: str) -> None:
    chats, service, client, tmp_path = env
    # A depth-1 subagent (record + tab) calling `agent run`.
    record, _turn = await service.create(
        subagents_service.SubagentCreate(
            agent_type=AgentType.CODEX, prompt="p", parent_tab_id="parent", keep=True
        )
    )
    assert record.tab_id
    caller = (
        {"caller_tab_id": record.tab_id} if identity == "tab" else {"caller_subagent_id": record.id}
    )
    unlinked = await client.post(
        "/api/subagents", json=_body(tmp_path, parent_tab_id=None, cwd=str(tmp_path), **caller)
    )
    assert unlinked.status_code == 400 and "--no-parent" in unlinked.json()["detail"]
    other = await client.post("/api/subagents", json=_body(tmp_path, keep=True, **caller))
    assert other.status_code == 201, other.text
    # Linked to the caller, whatever parent the request named.
    assert chats.create_calls[-1]["parent_tab_id"] == record.tab_id
    grandchild = service.get(other.json()["subagent_id"])
    assert grandchild.parent_tab_id == record.tab_id
    # The depth-2 subagent cannot go deeper, with or without a parent.
    deep = {"caller_tab_id": grandchild.tab_id}
    for extra in ({}, {"parent_tab_id": None, "cwd": str(tmp_path)}):
        refused = await client.post("/api/subagents", json=_body(tmp_path, **extra, **deep))
        assert refused.status_code == 400 and "nesting limit" in refused.json()["detail"]
    # Closing the caller cascades to the subagent it started.
    await service.close(record.id)
    await _eventually(lambda: grandchild.closed)


async def test_top_level_caller_may_run_unlinked_and_unknown_subagent_is_refused(env) -> None:
    chats, service, client, tmp_path = env
    body = _body(tmp_path, parent_tab_id=None, cwd=str(tmp_path), caller_tab_id="parent")
    assert (await client.post("/api/subagents", json=body)).status_code == 201
    assert chats.create_calls[-1]["parent_tab_id"] is None
    ghost = await client.post("/api/subagents", json=_body(tmp_path, caller_subagent_id="sa-x"))
    assert ghost.status_code == 409


async def test_closed_is_reported_only_after_the_tab_is_archived(env) -> None:
    chats, service, client, tmp_path = env
    seen: List[bool] = []
    real_archive = chats.archive_tab

    async def archive(tab_id: str) -> None:
        seen.extend(r.closed for r in service.list_subagents(include_closed=True))
        await asyncio.sleep(0.02)
        await real_archive(tab_id)

    service.runtime.archive_tab = archive
    started = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    record = service.get(started["subagent_id"])
    await _eventually(lambda: record.closed)
    assert chats.tabs[started["tab_id"]].archived is True
    assert seen == [False]


async def test_stop_persists_state_one_last_time(env) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _wait(client, started)
    record = service.get(started["subagent_id"])
    record.name = "renamed in memory only"
    await service.stop()
    stored = json.loads((tmp_path / "subagents.json").read_text())
    assert stored["subagents"][record.id]["name"] == "renamed in memory only"


async def test_create_in_flight_during_shutdown_spawns_nothing(env) -> None:
    chats, service, client, tmp_path = env
    chats.create_delay = 0.1
    pending = asyncio.ensure_future(client.post("/api/subagents", json=_body(tmp_path)))
    await _eventually(lambda: bool(chats.create_calls))
    await service.stop()
    response = await pending
    assert response.status_code == 503, response.text
    assert "shutting down" in response.json()["detail"]
    assert chats.tabs["sub-tab-1"].archived is True
    assert chats.sent == [] and service._background == set()
    assert service.list_subagents(include_closed=True) == []
    stored = json.loads((tmp_path / "subagents.json").read_text())
    assert stored["subagents"] == {}


def test_subagent_chats_get_guidance_without_agent_run_recipe() -> None:
    from claude_hub.services.agent_stream.providers.guidance import (
        HUB_RUNTIME_GUIDANCE,
        SUBAGENT_HUB_RUNTIME_GUIDANCE,
    )

    assert "agent run" in HUB_RUNTIME_GUIDANCE
    assert "agent run" not in SUBAGENT_HUB_RUNTIME_GUIDANCE
    assert "Task tools:" in SUBAGENT_HUB_RUNTIME_GUIDANCE


@pytest.mark.parametrize(
    ("link", "is_subagent"), [("none", False), ("tab", True), ("env_only", False)]
)
async def test_first_turn_guidance_omits_recipe_for_subagent_tabs(
    monkeypatch: pytest.MonkeyPatch, link: str, is_subagent: bool
) -> None:
    from unittest.mock import AsyncMock

    from claude_hub.services.agent_stream.native import CodexNativeSession
    from tests.test_native_goal_transport import _session

    session = _session()
    if link == "tab":
        session.subagent_id = "sa-1"
    elif link == "env_only":
        # A stale env key (e.g. an old copy) does not make an ordinary Chat a subagent.
        session.env = {**session.env, "CLAUDE_HUB_SUBAGENT_ID": "sa-1"}
    transport = CodexNativeSession(session)
    monkeypatch.setattr(transport, "_prepare_turn_context", AsyncMock(return_value=""))
    wrapped, _ = await transport._with_first_turn_prefixes("hello")
    assert "Task tools:" in wrapped
    assert ("claude-hub agent run" in wrapped) is (not is_subagent)


async def test_final_text_and_turn_history_are_bounded(env) -> None:
    chats, service, client, tmp_path = env

    async def huge(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        return "x" * 70_000

    chats.behavior = huge
    body = _body(tmp_path, keep=True, call_id="c-trim")
    started = (await client.post("/api/subagents", json=body)).json()
    result = await _wait(client, started)
    assert result["final_text_truncated"] is True
    assert len(result["final_text"]) < 64_200 and "truncated by Claude Hub" in result["final_text"]
    chats.behavior = chats.answer
    url = f"/api/subagents/{started['subagent_id']}/turns"
    for index in range(22):
        follow = await client.post(url, json={"prompt": f"q{index}"})
        assert follow.status_code == 201, follow.text
        await _wait(client, follow.json())
    record = service.get(started["subagent_id"])
    assert len(record.turns) == 21  # the first turn plus the last 20
    assert record.turns[0].turn_id == started["turn_id"]
    assert record.turns[-1].final_text == "ANSWER: q21"
    # The create call_id still replays after trimming.
    replay = await client.post("/api/subagents", json=body)
    assert replay.json()["turn_id"] == started["turn_id"]


async def test_state_is_written_off_the_event_loop(env, monkeypatch) -> None:
    import threading

    chats, service, client, tmp_path = env
    writers: List[int] = []
    real = service._write_state

    def spy(state: Dict[str, Any]) -> None:
        writers.append(threading.get_ident())
        real(state)

    monkeypatch.setattr(service, "_write_state", spy)
    started = (await client.post("/api/subagents", json=_body(tmp_path))).json()
    await _wait(client, started)
    assert writers and threading.get_ident() not in writers


async def test_concurrent_saves_land_in_call_order(env, monkeypatch) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _wait(client, started)
    record = service.get(started["subagent_id"])
    names = ["a", "b", "c", "d"]

    async def rename(name: str) -> None:
        record.name = name
        await service._save()

    await asyncio.gather(*(rename(n) for n in names))
    stored = json.loads((tmp_path / "subagents.json").read_text())
    assert stored["subagents"][record.id]["name"] == "d"


async def test_failed_create_save_archives_tab_and_retry_succeeds(env, monkeypatch) -> None:
    chats, service, client, tmp_path = env
    real = service._write_state
    failures = [OSError("disk full")]

    def flaky(state: Dict[str, Any]) -> None:
        if failures:
            raise failures.pop()
        real(state)

    monkeypatch.setattr(service, "_write_state", flaky)
    body = _body(tmp_path, call_id="call-save")
    failed = await client.post("/api/subagents", json=body)
    assert failed.status_code == 503
    assert "same call_id" in failed.json()["detail"]
    assert chats.tabs["sub-tab-1"].archived is True
    assert service.list_subagents(include_closed=True) == []
    assert chats.sent == []
    retried = await client.post("/api/subagents", json=body)
    assert retried.status_code == 201, retried.text
    assert retried.json()["tab_id"] == "sub-tab-2"
    assert (await _wait(client, retried.json()))["status"] == "completed"


async def test_failed_follow_up_save_leaves_no_running_turn(env, monkeypatch) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _wait(client, started)

    def broken(state: Dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_state", broken)
    url = f"/api/subagents/{started['subagent_id']}/turns"
    response = await client.post(url, json={"prompt": "more", "call_id": "f-x"})
    assert response.status_code == 503
    assert len(service.get(started["subagent_id"]).turns) == 1
    assert len(chats.sent) == 1


async def test_concurrent_same_call_id_creates_one_subagent_while_tab_starts(env) -> None:
    chats, service, client, tmp_path = env
    chats.create_delay = 0.1
    body = _body(tmp_path, call_id="call-slow")
    responses = await asyncio.gather(*(client.post("/api/subagents", json=body) for _ in range(3)))
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["turn_id"] for r in responses}) == 1
    assert len(chats.create_calls) == 1


async def test_cancel_targets_only_the_given_turn(env) -> None:
    chats, service, client, tmp_path = env
    gate = asyncio.Event()

    async def slow(tab_id: str, text: str, turn_id: str, timeout: float) -> str:
        await gate.wait()
        return "late"

    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    await _wait(client, started)
    chats.behavior = slow
    url = f"/api/subagents/{started['subagent_id']}"
    second = (await client.post(f"{url}/turns", json={"prompt": "two"})).json()
    await _settle()
    # Cancelling the earlier, finished turn must not touch the running one.
    stale = await client.post(f"{url}/cancel", params={"turn_id": started["turn_id"]})
    assert stale.json()["turn"]["status"] == "completed"
    assert service.get(started["subagent_id"]).turns[-1].status.value == "running"
    current = await client.post(f"{url}/cancel", params={"turn_id": second["turn_id"]})
    assert current.json()["turn"]["status"] == "cancelled"
    missing = await client.post(f"{url}/cancel", params={"turn_id": "nope"})
    assert missing.status_code == 404


async def test_non_kept_archive_waits_for_grace_and_stop_joins_pending_close(
    tmp_path: Path,
) -> None:
    chats = FakeChats()
    chats.add_tab("parent", cwd=str(tmp_path))
    service = SubagentService(
        tmp_path / "subagents.json", chats.runtime(), archive_grace_seconds=0.3
    )
    await service.start()
    record, turn = await service.create(
        subagents_service.SubagentCreate(
            agent_type=AgentType.CODEX, prompt="p", parent_tab_id="parent"
        )
    )
    await service.wait_turn(record.id, turn.turn_id, 5)
    assert turn.status.value == "completed"
    assert record.tab_id is not None
    await asyncio.sleep(0.05)
    assert not record.closed and not chats.tabs[record.tab_id].archived
    await _eventually(lambda: chats.tabs[record.tab_id or ""].archived)
    assert record.closed

    # A close still in its grace period is tracked and joined by stop().
    second, second_turn = await service.create(
        subagents_service.SubagentCreate(
            agent_type=AgentType.CODEX, prompt="q", parent_tab_id="parent"
        )
    )
    await service.wait_turn(second.id, second_turn.turn_id, 5)
    await _eventually(lambda: any("close" in t.get_name() for t in service._background))
    await service.stop()
    assert service._background == set()
    assert not second.closed  # the next start's reconciliation closes it
    restarted = SubagentService(tmp_path / "subagents.json", chats.runtime())
    await restarted.start()
    assert restarted.get(second.id).closed
    assert chats.tabs[second.tab_id or ""].archived
    await restarted.stop()


async def test_prefer_final_text_returns_answer_after_last_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from claude_hub.api import agent_stream as stream_api
    from claude_hub.models.agent_stream import AgentStreamEventType
    from claude_hub.services import dot as dot_module

    monkeypatch.setattr(
        dot_module, "get_dot_service", lambda: SimpleNamespace(is_dot_tab=lambda _t: False)
    )
    payload = {"assistant_text": "Reading.\nDone: 3 fixes.", "final_text": "Done: 3 fixes."}

    class Manager:
        async def subscribe_completion(self, _session: Any) -> asyncio.Queue[Any]:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            queue.put_nowait(
                SimpleNamespace(
                    turn_id="t-1", type=AgentStreamEventType.TURN_COMPLETED, payload=payload
                )
            )
            return queue

        def unsubscribe_completion(self, _session_id: str, _queue: Any) -> None:
            pass

    async def dispatch(_tab_id: str, request: Any, **_kwargs: Any) -> str:
        return str(request.client_turn_id)

    session = SimpleNamespace(id="terminal-tab-tab-1", tab_id="tab-1")
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: Manager())
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", dispatch)
    plain = await stream_api.dispatch_tab_chat_and_wait("tab-1", "hi", "t-1")
    preferred = await stream_api.dispatch_tab_chat_and_wait(
        "tab-1", "hi", "t-1", prefer_final_text=True
    )
    assert plain == "Reading.\nDone: 3 fixes."
    assert preferred == "Done: 3 fixes."


# ── real terminal manager: retirement listener and unarchive detach ──────


def _real_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import importlib

    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")

    runtime_home = tmp_path / "runtime"
    runtime_home.mkdir()
    monkeypatch.setattr(ttyd_manager_module, "_RUNTIME_HOME", runtime_home)
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", runtime_home / "tabs.json")
    monkeypatch.setattr(ttyd_manager_module, "ORDER_FILE", runtime_home / "tab_order.json")
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", runtime_home / "launch_env")

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        return None

    async def fake_stop_session_stream(session_id: str) -> None:
        return None

    async def fake_ensure_tab_running(self: TTYDManager, tab_id: str) -> None:
        return None

    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)
    monkeypatch.setattr(
        "claude_hub.services.agent_stream.tailer.stop_session_stream", fake_stop_session_stream
    )
    monkeypatch.setattr(TTYDManager, "ensure_tab_running", fake_ensure_tab_running)
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 14000
    manager.processes = {}
    manager._tab_order = []
    manager._start_locks = {}
    for index, (tab_id, parent_id) in enumerate([("parent", None), ("child", "parent")]):
        process = TTYDProcess(
            tab_id=tab_id,
            port=14001 + index,
            name=tab_id,
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            cwd=str(tmp_path),
            env={"CLAUDE_HUB_SUBAGENT_ID": "sa-real"} if parent_id else None,
            parent_tab_id=parent_id,
            subagent_id="sa-real" if parent_id else None,
        )
        manager.processes[tab_id] = process
        manager._tab_order.append(tab_id)
    return manager


def _seed_record(path: Path, tmp_path: Path, *, keep: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": "sa-real",
        "tab_id": "child",
        "parent_tab_id": "parent",
        "agent_type": "codex",
        "cwd": str(tmp_path),
        "name": "child",
        "keep": keep,
        "created_at": now,
        "turns": [
            {
                "turn_id": "turn-1",
                "status": "completed",
                "final_text": "ok",
                "started_at": now,
                "completed_at": now,
                "timeout_seconds": 60,
            }
        ],
    }
    path.write_text(json.dumps({"version": 1, "subagents": {"sa-real": record}}))


def _manager_runtime(manager: Any) -> SubagentRuntime:
    async def no_turns(*_args: Any) -> str:
        raise AssertionError("no turn expected")

    return SubagentRuntime(
        create_tab=manager.create_tab,
        run_turn=no_turns,
        archive_tab=manager.archive_tab,
        get_tab=manager.get_tab,
        list_tabs=lambda: [p.to_schema() for p in manager.processes.values()],
        resolve_env=lambda _preset, env: env,
    )


@pytest.mark.parametrize("retire", ["archive", "delete"])
async def test_real_manager_parent_retirement_notifies_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, retire: str
) -> None:
    manager = _real_manager(monkeypatch, tmp_path)
    path = tmp_path / "subagents.json"
    _seed_record(path, tmp_path)
    service = SubagentService(path, _manager_runtime(manager), archive_grace_seconds=0)
    await service.start()
    assert not service.get("sa-real").closed
    if retire == "archive":
        await manager.archive_tab("parent")
    else:
        assert await manager.delete_tab("parent") is True
    await _eventually(lambda: service.get("sa-real").closed)
    assert service.get("sa-real").close_reason == "parent tab archived or deleted"
    await _eventually(lambda: manager.get_tab("child").archived)
    await service.stop()


async def test_unarchived_subagent_tab_is_detached_and_never_re_archived(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _real_manager(monkeypatch, tmp_path)
    path = tmp_path / "subagents.json"
    _seed_record(path, tmp_path)
    service = SubagentService(path, _manager_runtime(manager), archive_grace_seconds=0)
    await service.start()
    # The user archives the subagent Chat (closing the record), then restores it.
    await manager.archive_tab("child")
    await _eventually(lambda: service.get("sa-real").closed)
    restored = await manager.unarchive_tab("child")
    assert restored is not None and restored.archived is False
    assert restored.subagent_id is None and restored.parent_tab_id is None
    assert "CLAUDE_HUB_SUBAGENT_ID" not in restored.env
    # Neither parent closure nor startup reconciliation re-archives it.
    await manager.archive_tab("parent")
    await _settle()
    await service.stop()
    again = SubagentService(path, _manager_runtime(manager), archive_grace_seconds=0)
    await again.start()
    assert manager.get_tab("child").archived is False
    await again.stop()


async def test_reconciliation_archives_orphans_but_keeps_detached_tabs(tmp_path: Path) -> None:
    chats = FakeChats()
    chats.add_tab("parent", cwd=str(tmp_path))
    chats.add_tab("orphan", cwd=str(tmp_path), subagent_id="sa-gone", parent_tab_id="parent")
    chats.add_tab("detached", cwd=str(tmp_path))  # a restored subagent: link cleared
    chats.add_tab("stale-owner", cwd=str(tmp_path), subagent_id="sa-real")
    path = tmp_path / "subagents.json"
    _seed_record(path, tmp_path)
    stored = json.loads(path.read_text())
    stored["subagents"]["sa-real"]["tab_id"] = "detached"
    path.write_text(json.dumps(stored))
    service = SubagentService(path, chats.runtime(), archive_grace_seconds=0)
    await service.start()
    assert chats.tabs["orphan"].archived is True
    assert chats.tabs["stale-owner"].archived is True  # record points at another tab
    # Its record closes (the tab no longer belongs to it) but the tab stays live.
    assert service.get("sa-real").close_reason == "subagent tab detached"
    assert chats.tabs["detached"].archived is False
    await service.stop()


async def test_close_stays_open_and_retryable_when_the_tab_will_not_archive(env) -> None:
    chats, service, client, tmp_path = env
    started = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    assert (await _wait(client, started))["status"] == "completed"
    await _settle()
    working_archive = service.runtime.archive_tab

    async def broken_archive(tab_id: str) -> None:
        raise RuntimeError("runtime shutdown failed")

    service.runtime = dataclasses.replace(service.runtime, archive_tab=broken_archive)
    failed = await client.post(f"/api/subagents/{started['subagent_id']}/close")
    assert failed.status_code == 503
    assert service.get(started["subagent_id"]).closed is False
    assert chats.tabs[started["tab_id"]].archived is False

    service.runtime = dataclasses.replace(service.runtime, archive_tab=working_archive)
    closed = await client.post(f"/api/subagents/{started['subagent_id']}/close")
    assert closed.status_code == 200 and closed.json()["closed"] is True
    assert chats.tabs[started["tab_id"]].archived is True


async def test_close_retry_after_a_partial_archive_still_closes_children(env) -> None:
    chats, service, client, tmp_path = env
    parent = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    assert (await _wait(client, parent))["status"] == "completed"
    child_body = {**_body(tmp_path, keep=True), "parent_tab_id": parent["tab_id"]}
    child = (await client.post("/api/subagents", json=child_body)).json()
    assert (await _wait(client, child))["status"] == "completed"
    await _settle()
    working_archive = service.runtime.archive_tab

    async def partial_archive(tab_id: str) -> None:
        # Like the real manager: flagged archived, then shutdown fails before
        # the retirement callback runs.
        chats.tabs[tab_id] = chats.tabs[tab_id].model_copy(update={"archived": True})
        raise RuntimeError("runtime shutdown failed")

    service.runtime = dataclasses.replace(service.runtime, archive_tab=partial_archive)
    failed = await client.post(f"/api/subagents/{parent['subagent_id']}/close")
    assert failed.status_code == 503
    assert service.get(child["subagent_id"]).closed is False

    service.runtime = dataclasses.replace(service.runtime, archive_tab=working_archive)
    retried = await client.post(f"/api/subagents/{parent['subagent_id']}/close")
    assert retried.status_code == 200 and retried.json()["closed"] is True
    assert service.get(child["subagent_id"]).closed is True
    assert chats.tabs[child["tab_id"]].archived is True


async def test_parent_stays_open_until_its_child_closes(env) -> None:
    chats, service, client, tmp_path = env
    parent = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    assert (await _wait(client, parent))["status"] == "completed"
    child_body = {**_body(tmp_path, keep=True), "parent_tab_id": parent["tab_id"]}
    child = (await client.post("/api/subagents", json=child_body)).json()
    assert (await _wait(client, child))["status"] == "completed"
    await _settle()
    working_archive = service.runtime.archive_tab

    async def child_will_not_archive(tab_id: str) -> None:
        if tab_id == child["tab_id"]:
            raise RuntimeError("child runtime shutdown failed")
        await working_archive(tab_id)

    service.runtime = dataclasses.replace(service.runtime, archive_tab=child_will_not_archive)
    failed = await client.post(f"/api/subagents/{parent['subagent_id']}/close")
    assert failed.status_code == 503
    assert service.get(parent["subagent_id"]).closed is False
    assert service.get(child["subagent_id"]).closed is False

    service.runtime = dataclasses.replace(service.runtime, archive_tab=working_archive)
    retried = await client.post(f"/api/subagents/{parent['subagent_id']}/close")
    assert retried.status_code == 200
    assert service.get(child["subagent_id"]).closed is True


async def test_parent_close_waits_for_a_child_close_in_progress(env) -> None:
    chats, service, client, tmp_path = env
    parent = (await client.post("/api/subagents", json=_body(tmp_path, keep=True))).json()
    assert (await _wait(client, parent))["status"] == "completed"
    child_body = {**_body(tmp_path, keep=True), "parent_tab_id": parent["tab_id"]}
    child = (await client.post("/api/subagents", json=child_body)).json()
    assert (await _wait(client, child))["status"] == "completed"
    await _settle()
    child_record = service.get(child["subagent_id"])
    service._closing.add(child_record.id)  # another close owns the child

    async def finish_other_close() -> None:
        await asyncio.sleep(0.2)
        child_record.closed = True
        service._closing.discard(child_record.id)

    other = asyncio.create_task(finish_other_close())
    closed = await client.post(f"/api/subagents/{parent['subagent_id']}/close")
    await other
    assert closed.status_code == 200 and closed.json()["closed"] is True
