"""Hub subagent service and API contracts with a faked Chat runtime (no providers)."""

from __future__ import annotations

import asyncio
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
        return self.add_tab(
            f"sub-tab-{self.counter}",
            cwd=kwargs["cwd"],
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
    chats.add_tab("parent", cwd=str(tmp_path))
    service = SubagentService(tmp_path / "subagents.json", chats.runtime())
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
    assert call["solo_mode"] is True
    assert call["env"] == {"CODEX_MODEL": "gpt-x", "CODEX_REASONING_EFFORT": "high"}
    # No bootstrap turn: one turn whose provider text carries a short preamble,
    # while the visible Chat text is the caller's prompt.
    assert len(chats.sent) == 1
    assert SUBAGENT_PREAMBLE in chats.sent[0]["text"]
    assert chats.sent[0]["visible"] == "summarize X"
    assert chats.sent[0]["turn_id"] == started["turn_id"]
    await _settle()
    record = service.get(started["subagent_id"])
    assert record.closed and record.close_reason == "turn completed"
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
    await _settle()
    assert chats.tabs[started["tab_id"]].archived is True


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
    first._tasks[turn.turn_id].cancel()
    first._shutting_down = True
    await asyncio.sleep(0)
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
