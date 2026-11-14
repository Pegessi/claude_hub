"""Patrol scheduling, durable delivery claims and public entrypoint contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from pydantic import ValidationError

from claude_hub.api import patrol as patrol_api
from claude_hub.auth import dependencies as auth_dependencies
from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.services.demand_patrol import commands
from claude_hub.services.demand_patrol.models import PatrolConfig, PatrolRunRecord, PatrolState
from claude_hub.services.demand_patrol.service import (
    DemandPatrolService,
    due_daily_slots,
    feishu_due,
)
from claude_hub.services.demand_patrol.state import PatrolStateStore

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)  # 11:00 Asia/Shanghai


class FakeDot:
    def __init__(self, *, ready: bool = True, result: str = "sent") -> None:
        self.ready = ready
        self.result = result
        self.calls: list[tuple[str, str | None, str]] = []

    def is_set_up(self) -> bool:
        return self.ready

    async def notify(self, text: str, *, title: str | None = None, tone: str = "info") -> str:
        self.calls.append((text, title, tone))
        return self.result


class EmptyRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 30,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append(argv)
        assert argv == ["infowall", "demand", "list", "--json"]
        return {"demands": []}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PatrolStateStore:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "hub" / "workspaces"))
    return PatrolStateStore()


def service(store: PatrolStateStore, dot: FakeDot, runner: EmptyRunner) -> DemandPatrolService:
    return DemandPatrolService(
        store=store,
        dot=dot,
        runner=runner,
        task_source=lambda: ([], {}),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "offset,slots",
    [
        (-1, []),
        (0, ["2026-11-14@11:00"]),
        (89, ["2026-11-14@11:00"]),
        (90, []),
        (389, []),
        (390, ["2026-11-14@17:30"]),
        (479, ["2026-11-14@17:30"]),
        (480, []),
    ],
)
def test_exact_shanghai_daily_due_windows(offset: int, slots: list[str]) -> None:
    assert due_daily_slots(PatrolState(), NOW + timedelta(minutes=offset)) == slots


def test_default_config_and_validation() -> None:
    assert PatrolConfig().daily_slots == ["11:00", "17:30"]
    for changes in [
        {"timezone": "not/a-zone"},
        {"feishu_window_start": "9:30"},
        {"daily_slots": ["24:00"]},
        {"feishu_interval_minutes": 0},
        {"feishu_window_start": "23:00", "feishu_window_end": "10:00"},
        {"stalled_hours": float("nan")},
        {"unknown": 1},
    ]:
        with pytest.raises(ValidationError):
            PatrolConfig.model_validate(changes)


def test_state_roundtrip_bounded_history_and_isolation(store: PatrolStateStore) -> None:
    state = PatrolState(
        processed_feishu_ids=[f"message-{i}" for i in range(520)],
        fired_daily_slots=[f"slot-{i}" for i in range(80)],
        recent_runs=[
            PatrolRunRecord(channel="daily", started_at=NOW.isoformat()) for _ in range(30)
        ],
    )
    store.save(state)
    loaded = PatrolStateStore().load()
    assert loaded == state
    assert len(loaded.processed_feishu_ids) == 500
    assert loaded.processed_feishu_ids[0] == "message-20"
    assert len(loaded.fired_daily_slots) == 60
    assert len(loaded.recent_runs) == 20
    assert store.path.name == "demand_patrol_state.json"
    assert not store.path.exists()  # SQLite document, not a parallel JSON store.


def test_feishu_work_window_and_interval() -> None:
    state = PatrolState()
    assert not feishu_due(state, NOW - timedelta(minutes=91))
    assert feishu_due(state, NOW - timedelta(minutes=90))
    assert feishu_due(state, NOW + timedelta(hours=11, minutes=30))
    assert not feishu_due(state, NOW + timedelta(hours=11, minutes=31))
    state.recent_runs = [PatrolRunRecord(channel="feishu", started_at=NOW.isoformat())]
    assert not feishu_due(state, NOW + timedelta(minutes=19))
    assert feishu_due(state, NOW + timedelta(minutes=20))
    state.config.enabled = False
    assert not feishu_due(state, NOW)
    assert due_daily_slots(state, NOW) == []


async def test_daily_claim_survives_restart_and_concurrent_ticks(store: PatrolStateStore) -> None:
    state = PatrolState(config=PatrolConfig(feishu_enabled=False))
    store.save(state)
    dot, runner = FakeDot(), EmptyRunner()
    first = service(store, dot, runner)
    await asyncio.gather(first.tick(), first.tick())
    assert len(dot.calls) == 1
    assert dot.calls[0][1] == "📋 研发需求与 Work 巡检 (11:00)"
    assert dot.calls[0][2] == "info"
    assert store.load().fired_daily_slots == ["2026-11-14@11:00"]
    assert await service(store, dot, runner).tick() == []
    assert len(runner.calls) == 1


async def test_no_notify_dry_run_and_setup_gate(store: PatrolStateStore) -> None:
    dot, runner = FakeDot(ready=False), EmptyRunner()
    patrol = service(store, dot, runner)
    assert await patrol.tick() == []
    assert runner.calls == []
    before = store.load()
    preview = await patrol.run(channel="daily", dry_run=True)
    assert "📥" in preview["markdown"]
    assert store.load() == before
    await patrol.run(channel="daily", notify=False)
    assert dot.calls == []
    assert len(store.load().recent_runs) == 1
    assert store.load().fired_daily_slots == []


async def test_status_and_disable_remain_responsive_during_run(store: PatrolStateStore) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingRunner(EmptyRunner):
        async def __call__(
            self,
            argv: list[str],
            *,
            timeout: float = 30,
            input_text: str | None = None,
            env: dict[str, str] | None = None,
        ) -> Any:
            entered.set()
            await release.wait()
            return await super().__call__(argv, timeout=timeout, input_text=input_text, env=env)

    patrol = service(store, FakeDot(), BlockingRunner())
    run = asyncio.create_task(patrol.run(channel="daily", notify=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        status = await asyncio.wait_for(patrol.status(), timeout=1)
        assert status["running"] is True
        config = await asyncio.wait_for(patrol.configure({"enabled": False}), timeout=1)
        assert config.enabled is False
    finally:
        release.set()
        await run
    assert store.load().config.enabled is False
    assert (await patrol.status())["running"] is False


async def test_failed_notice_retains_claim_and_records_outcome(store: PatrolStateStore) -> None:
    store.save(PatrolState(config=PatrolConfig(feishu_enabled=False)))
    dot, runner = FakeDot(result="failed"), EmptyRunner()
    records = await service(store, dot, runner).tick()
    assert records[0].notified == 0
    assert records[0].errors
    assert store.load().fired_daily_slots == ["2026-11-14@11:00"]
    assert await service(store, dot, runner).tick() == []


async def test_background_disabled_in_tests_and_explicit_stop(
    store: PatrolStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patrol = service(store, FakeDot(ready=False), EmptyRunner())
    patrol.start(allow_test_loop=True)
    assert patrol._loop_task is None
    monkeypatch.delenv("CLAUDE_HUB_DISABLE_PATROL_LOOP")
    patrol.start()
    assert patrol._loop_task is None
    patrol.start(allow_test_loop=True)
    assert patrol._loop_task is not None
    await asyncio.sleep(0)
    await patrol.stop()
    assert patrol._loop_task is None


async def test_rest_status_partial_config_run_and_auth(
    store: PatrolStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dot, runner = FakeDot(), EmptyRunner()
    monkeypatch.setattr(patrol_api, "demand_patrol_service", service(store, dot, runner))
    app = FastAPI()
    app.include_router(patrol_api.router)
    monkeypatch.setattr(auth_dependencies, "settings", SimpleNamespace(auth_enabled=True))
    monkeypatch.setattr(auth_dependencies, "is_local_network_request", lambda request: False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        denied = await client.get("/api/patrol/status")
        assert denied.status_code in {401, 403}
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="test")
        status = await client.get("/api/patrol/status")
        assert status.status_code == 200
        assert status.json()["config"]["daily_slots"] == ["11:00", "17:30"]
        changed = await client.put("/api/patrol/config", json={"enabled": False})
        assert changed.status_code == 200
        assert changed.json()["enabled"] is False
        assert changed.json()["daily_slots"] == ["11:00", "17:30"]
        bad = await client.put("/api/patrol/config", json={"daily_slots": ["25:00"]})
        assert bad.status_code == 422
        bad = await client.put("/api/patrol/config", json={"typo": True})
        assert bad.status_code == 422
        result = await client.post("/api/patrol/run", json={"channel": "daily", "dry_run": True})
        assert result.status_code == 200
        assert result.json()["runs"][0]["llm_invoked"] == 0
        assert result.json()["markdown"]
        bad = await client.post("/api/patrol/run", json={"channel": "typo"})
        assert bad.status_code == 422
    assert dot.calls == []


@pytest.mark.parametrize(
    "args,method,path,payload",
    [
        (["status"], "GET", "/api/patrol/status", None),
        (
            ["run", "--channel", "daily", "--dry-run", "--no-notify"],
            "POST",
            "/api/patrol/run",
            {"channel": "daily", "dry_run": True, "notify": False},
        ),
        (
            [
                "config",
                "--disable",
                "--daily-slot",
                "11:00",
                "--daily-slot",
                "17:30",
                "--feishu-interval",
                "30",
                "--window-start",
                "10:00",
                "--window-end",
                "22:00",
            ],
            "PUT",
            "/api/patrol/config",
            {
                "enabled": False,
                "daily_slots": ["11:00", "17:30"],
                "feishu_interval_minutes": 30,
                "feishu_window_start": "10:00",
                "feishu_window_end": "22:00",
            },
        ),
    ],
)
def test_cli_commands_use_real_hub_client(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    method: str,
    path: str,
    payload: Any,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"config": {}, "runs": [], "markdown": "preview"})

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(
            "http://test",
            transport=httpx.MockTransport(handle),
        ),
    )
    result = CliRunner().invoke(
        cli_main.cli, ["--base-url", "http://test", "--json", "patrol", *args]
    )
    assert result.exit_code == 0, result.output
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == (method, path)
    assert (json.loads(request.content) if request.content else None) == payload
    if path.endswith("/run"):
        assert request.extensions["timeout"]["read"] > 1800


async def test_cli_json_transport_timeout_and_output_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await commands.run_command([sys.executable, "-c", 'print("{}")']) == {}
    with pytest.raises(commands.CommandError, match="timed out"):
        await commands.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05
        )
    monkeypatch.setattr(commands, "_MAX_OUTPUT_BYTES", 16)
    with pytest.raises(commands.CommandError, match="output budget"):
        await commands.run_command([sys.executable, "-c", 'print("x" * 30)'])


async def test_cli_deadline_retires_descendants_holding_stdout() -> None:
    wrapper = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); "
        "time.sleep(30)"
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(commands.CommandError, match="timed out"):
        await commands.run_command([sys.executable, "-c", wrapper], timeout=0.1)
    assert asyncio.get_running_loop().time() - started < 1.5


def test_infowall_credentials_are_path_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFOWALL_API_KEY_FILE", "/nonexistent/never-read")
    monkeypatch.setenv("INFOWALL_URL", "http://127.0.0.1:19999")
    assert commands.infowall_env() == {
        "INFOWALL_API_KEY_FILE": "/nonexistent/never-read",
        "INFOWALL_URL": "http://127.0.0.1:19999",
    }
