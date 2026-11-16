"""Patrol scheduling, durable delivery claims and public entrypoint contracts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
from claude_hub.services.demand_patrol.models import (
    PatrolConfig,
    PatrolRunRecord,
    PatrolState,
)
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
    assert due_daily_slots(PatrolState(), NOW + timedelta(minutes=offset)) == []
    enabled_state = PatrolState(config=PatrolConfig(enabled=True))
    assert due_daily_slots(enabled_state, NOW + timedelta(minutes=offset)) == slots


def test_default_config_is_disabled_and_validation() -> None:
    default_cfg = PatrolConfig()
    assert default_cfg.enabled is False
    assert default_cfg.feishu_enabled is True
    assert default_cfg.daily_enabled is True
    assert default_cfg.daily_slots == ["11:00", "17:30"]
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
    assert not feishu_due(PatrolState(), NOW)
    state = PatrolState(config=PatrolConfig(enabled=True))
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


async def test_daily_claim_survives_restart_and_concurrent_ticks(
    store: PatrolStateStore,
) -> None:
    state = PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=False))
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


async def test_default_disabled_gate_no_notify_dry_run_and_setup_gate(
    store: PatrolStateStore,
) -> None:
    # Even when Dot is set up, default disabled patrol never runs on scheduled ticks.
    ready_dot, ready_runner = FakeDot(ready=True), EmptyRunner()
    assert await service(store, ready_dot, ready_runner).tick() == []
    assert ready_runner.calls == []
    assert ready_dot.calls == []

    # Once enabled, still requires Dot setup to tick.
    store.save(PatrolState(config=PatrolConfig(enabled=True)))
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


async def test_status_and_disable_remain_responsive_during_run(
    store: PatrolStateStore,
) -> None:
    store.save(PatrolState(config=PatrolConfig(enabled=True)))
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


async def test_failed_notice_retains_claim_and_records_outcome(
    store: PatrolStateStore,
) -> None:
    store.save(PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=False)))
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
    from claude_hub.api import dot as dot_api

    dot, runner = FakeDot(), EmptyRunner()
    svc = service(store, dot, runner)
    monkeypatch.setattr(patrol_api, "demand_patrol_service", svc)
    app = FastAPI()
    app.include_router(patrol_api.router)
    app.include_router(dot_api.router)
    monkeypatch.setattr(auth_dependencies, "settings", SimpleNamespace(auth_enabled=True))
    monkeypatch.setattr(auth_dependencies, "is_local_network_request", lambda request: False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        denied = await client.get("/api/patrol/status")
        assert denied.status_code in {401, 403}
        denied_dot = await client.get("/api/dot/patrol")
        assert denied_dot.status_code in {401, 403}
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="test")
        status = await client.get("/api/patrol/status")
        assert status.status_code == 200
        assert status.json()["config"]["enabled"] is False
        assert status.json()["config"]["daily_slots"] == ["11:00", "17:30"]
        dot_status = await client.get("/api/dot/patrol")
        assert dot_status.status_code == 200
        assert dot_status.json()["config"]["enabled"] is False
        dot_enabled = await client.patch(
            "/api/dot/patrol",
            json={"enabled": True, "feishu_enabled": False},
        )
        assert dot_enabled.status_code == 200
        assert dot_enabled.json()["enabled"] is True
        assert dot_enabled.json()["feishu_enabled"] is False
        assert dot_enabled.json()["daily_enabled"] is True
        changed = await client.put("/api/patrol/config", json={"enabled": False})
        assert changed.status_code == 200
        assert changed.json()["enabled"] is False
        assert changed.json()["daily_slots"] == ["11:00", "17:30"]
        bad = await client.put("/api/patrol/config", json={"daily_slots": ["25:00"]})
        assert bad.status_code == 422
        bad = await client.put("/api/patrol/config", json={"typo": True})
        assert bad.status_code == 422
        bad_dot = await client.patch("/api/dot/patrol", json={"typo": True})
        assert bad_dot.status_code == 422
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
        (["patrol", "status"], "GET", "/api/patrol/status", None),
        (
            ["patrol", "run", "--channel", "daily", "--dry-run", "--no-notify"],
            "POST",
            "/api/patrol/run",
            {"channel": "daily", "dry_run": True, "notify": False},
        ),
        (
            [
                "patrol",
                "config",
                "--enable",
                "--no-feishu",
                "--daily",
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
                "enabled": True,
                "feishu_enabled": False,
                "daily_enabled": True,
                "daily_slots": ["11:00", "17:30"],
                "feishu_interval_minutes": 30,
                "feishu_window_start": "10:00",
                "feishu_window_end": "22:00",
            },
        ),
        (["dot", "patrol"], "GET", "/api/dot/patrol", None),
        (
            ["dot", "patrol", "--enable", "--no-daily"],
            "PATCH",
            "/api/dot/patrol",
            {"enabled": True, "daily_enabled": False},
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
        return httpx.Response(
            200, json={"config": {"enabled": False}, "runs": [], "markdown": "preview"}
        )

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(
            "http://test",
            transport=httpx.MockTransport(handle),
        ),
    )
    result = CliRunner().invoke(cli_main.cli, ["--base-url", "http://test", "--json", *args])
    assert result.exit_code == 0, result.output
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == (method, path)
    assert (json.loads(request.content) if request.content else None) == payload
    if path.endswith("/run"):
        assert request.extensions["timeout"]["read"] > 1800


async def test_cli_json_transport_timeout_and_output_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_infowall_loopback_bypasses_inherited_proxies(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    monkeypatch.setenv("INFOWALL_URL", f"http://{host}:18899")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setenv("no_proxy", "example.test")
    env = commands.infowall_env()
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost,::1"


def test_infowall_remote_endpoint_keeps_proxy_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFOWALL_URL", "https://infowall.example.test")
    env = commands.infowall_env()
    assert "NO_PROXY" not in env and "no_proxy" not in env


@pytest.mark.parametrize(
    "name,relative",
    [
        ("infowall", ".local/opt/infowall/current/infowall"),
        ("lark-cli", ".local/bin/lark-cli"),
    ],
)
@pytest.mark.parametrize("location", ["path", "home", "home-nonexecutable", "host", "missing"])
def test_cli_executable_resolution_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    relative: str,
    location: str,
) -> None:
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("HOME", str(home))
    on_path = "/test-bin/" + name
    monkeypatch.setattr(
        commands.shutil, "which", lambda value: on_path if location == "path" else None
    )
    checked: list[Path] = []
    candidates = [home / relative, Path("/home/tiger") / relative]

    def is_installed(path: Path) -> bool:
        checked.append(path)
        if location == "home-nonexecutable":
            return path in candidates
        return (
            path == candidates[0]
            if location == "home"
            else path == candidates[1] and location == "host"
        )

    def is_executable(path: Path, mode: int) -> bool:
        assert mode == os.X_OK
        return not (location == "home-nonexecutable" and path == candidates[0])

    monkeypatch.setattr(Path, "is_file", is_installed)
    monkeypatch.setattr(commands.os, "access", is_executable)
    actual = commands._resolve_executable(name)
    expected = {
        "path": on_path,
        "home": str(candidates[0]),
        "home-nonexecutable": str(candidates[1]),
        "host": str(candidates[1]),
        "missing": name,
    }
    assert actual == expected[location]
    assert checked == (
        [] if location == "path" else candidates[:1] if location == "home" else candidates
    )


@pytest.mark.parametrize("cleanup_error", [TimeoutError, OSError])
async def test_cli_cleanup_preserves_primary_error_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: type[Exception],
) -> None:
    stdout = asyncio.StreamReader()
    stdout.feed_data(b"x" * 32)
    stdout.feed_eof()
    transport = Mock()
    process = SimpleNamespace(
        stdout=stdout,
        returncode=0,
        pid=12345,
        wait=AsyncMock(side_effect=cleanup_error("cleanup failure")),
        _transport=transport,
    )
    monkeypatch.setattr(commands, "_MAX_OUTPUT_BYTES", 16)
    monkeypatch.setattr(commands.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    kill = Mock(side_effect=AssertionError("an exited process must not be signalled"))
    monkeypatch.setattr(commands.os, "killpg", kill)
    with pytest.raises(commands.CommandError, match="output budget"):
        await commands.run_command([sys.executable, "-c", "unused"])
    kill.assert_not_called()
    process.wait.assert_awaited_once()
    transport.close.assert_called_once()


@pytest.mark.parametrize(
    "output_bytes,outcome",
    [
        (6_488_929, "success"),
        (33 * 1024 * 1024, "bounded"),
    ],
)
def test_infowall_large_json_and_overflow_close_transports_before_loop_shutdown(
    tmp_path: Path,
    output_bytes: int,
    outcome: str,
) -> None:
    # Execute a fake CLI through the real pipe reader in a disposable interpreter;
    # pytest's long-lived loop cannot hide transport destructors running after close.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "infowall"
    executable.write_text(
        f"#!{sys.executable}\n" + textwrap.dedent("""\
        import json
        import os
        import sys
        assert sys.argv[1:] == ["demand", "list", "--json"]
        payload = {"demands": [{"id": str(i)} for i in range(365)], "padding": "",
                   "proxy_excluded": os.environ.get("NO_PROXY")}
        padding_size = int(os.environ["PATROL_TEST_PAYLOAD_BYTES"]) - len(json.dumps(payload).encode())
        payload["padding"] = "x" * padding_size
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
        """),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    probe = textwrap.dedent("""\
        import asyncio
        import gc
        import json
        import sys
        sys.path.insert(0, sys.argv[1])
        from claude_hub.services.demand_patrol.commands import CommandError, infowall_env, run_command
        try:
            payload = asyncio.run(run_command(["infowall", "demand", "list", "--json"],
                                             timeout=15, env=infowall_env()))
            print(json.dumps({"outcome": "success", "demands": len(payload["demands"]),
                              "bytes": len(json.dumps(payload).encode()),
                              "proxy_excluded": payload["proxy_excluded"]}))
        except CommandError as exc:
            print(json.dumps({"outcome": "bounded", "error": str(exc)}))
        gc.collect()
        """)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_HUB_HOME": str(tmp_path / "hub"),
        "CLAUDE_HUB_STATE_ROOT": str(tmp_path / "state"),
        "CLAUDE_HUB_TMUX_SOCKET": "ch-patrol-r2-pipes",
        "CLAUDE_HUB_DISABLE_PATROL_LOOP": "1",
        "INFOWALL_URL": "http://127.0.0.1:18899",
        "INFOWALL_API_KEY_FILE": "/nonexistent/never-read",
        "HTTP_PROXY": "http://proxy.invalid:8080",
        "PATH": str(bin_dir),
        "PATROL_TEST_PAYLOAD_BYTES": str(output_bytes),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-W",
            "always",
            "-c",
            probe,
            str(Path(__file__).resolve().parents[1]),
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == outcome
    if outcome == "success":
        assert report["demands"] == 365 and report["bytes"] == output_bytes
        assert report["proxy_excluded"] == "127.0.0.1,localhost,::1"
    else:
        assert "output budget" in report["error"]
    assert result.stderr == "", result.stderr
