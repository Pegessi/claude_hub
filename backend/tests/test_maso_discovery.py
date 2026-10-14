"""Discovery must select one validated instance and retain explicit intent."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.main import app
from claude_hub.maso_probe import main as probe_main
from claude_hub.models import AgentType, SessionKind
from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable, maso_base_url
from claude_hub.services.maso_discovery import discover_maso_service
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess


@pytest.fixture
def local_services(tmp_path, monkeypatch):
    monkeypatch.delenv("MASO_BASE_URL", raising=False)
    monkeypatch.delenv("MASO_PORT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    registry = tmp_path / ".maso" / "instances.json"
    registry.parent.mkdir()
    calls = []
    state = {"ready": set(), "stall": False}
    original = httpx.AsyncClient

    async def handle(request):
        calls.append((request.method, str(request.url)))
        if state["stall"]:
            await asyncio.sleep(10)
        if request.url.port not in state["ready"]:
            raise httpx.ConnectError("stale instance")
        if request.url.path == "/api/models":
            return httpx.Response(200, json=[{"key": "seed"}])
        assert request.url.path == "/api/models/default"
        return httpx.Response(200, json={"key": "seed"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return registry, state, calls


@pytest.mark.asyncio
async def test_registry_discovers_nonstandard_service_and_ignores_stale_exports(local_services):
    registry, state, calls = local_services
    registry.write_text(
        json.dumps(
            {
                "11004": {"port": 11004, "mlx_export_url": "https://never-contact.test"},
                "stale": {"port": 11005},
                "invalid": {"port": True},
                "invalid2": {"port": 70000},
            }
        )
    )
    state["ready"].add(11004)
    base, models, default = await discover_maso_service({})
    assert base == "http://127.0.0.1:11004"
    assert len(models) == 1 and default == "seed"
    assert all(method == "GET" and url.startswith("http://127.0.0.1:") for method, url in calls)
    assert {httpx.URL(url).port for _, url in calls} == {9796, 11004, 11005}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry_text",
    [None, "not json", "[]", '{"old": {"port": "11004"}}', "[" * 10000 + "]" * 10000],
)
async def test_legacy_default_remains_usable_without_valid_registry(local_services, registry_text):
    registry, state, _calls = local_services
    if registry_text is not None:
        registry.write_text(registry_text)
    state["ready"].add(9796)
    assert (await discover_maso_service({}))[0] == "http://127.0.0.1:9796"


@pytest.mark.asyncio
async def test_multiple_healthy_instances_are_refused_instead_of_picking_first(local_services):
    registry, state, _calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].update({9796, 11004})
    with pytest.raises(MasoServiceUnavailable, match="Multiple local.*MASO_BASE_URL"):
        await discover_maso_service({})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat_url", "hub_url", "hub_port"])
async def test_explicit_configuration_probes_only_its_target(local_services, monkeypatch, source):
    registry, state, calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].update({9796, 11004, 11006})
    key = "MASO_BASE_URL" if source.endswith("url") else "MASO_PORT"
    value = "http://127.0.0.1:11006" if key == "MASO_BASE_URL" else "11006"
    env = {key: value} if source.startswith("chat") else {}
    if not env:
        monkeypatch.setenv(key, value)
    assert (await discover_maso_service(env))[0] == "http://127.0.0.1:11006"
    assert {httpx.URL(url).port for _, url in calls} == {11006}


@pytest.mark.asyncio
async def test_broken_explicit_url_never_falls_back_to_healthy_registry(
    local_services, monkeypatch
):
    registry, state, calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].add(11004)
    monkeypatch.setenv("MASO_BASE_URL", "http://127.0.0.1:11007")
    with pytest.raises(MasoServiceUnavailable):
        await discover_maso_service({})
    assert {httpx.URL(url).port for _, url in calls} == {11007}


def test_url_precedes_port_and_invalid_port_fails_before_http(local_services, monkeypatch):
    _registry, _state, calls = local_services
    monkeypatch.setenv("MASO_BASE_URL", "http://127.0.0.1:11004")
    monkeypatch.setenv("MASO_PORT", "bad")
    assert maso_base_url({"MASO_PORT": "bad"}) == "http://127.0.0.1:11004"
    assert maso_base_url({"MASO_BASE_URL": "http://127.0.0.1:11008/"}) == "http://127.0.0.1:11008"
    monkeypatch.delenv("MASO_BASE_URL")
    for value in ("0", "65536", "bad", "-1", "1.5", "１１００４"):
        monkeypatch.setenv("MASO_PORT", value)
        with pytest.raises(ValueError, match="MASO_PORT"):
            asyncio.run(discover_maso_service({}))
    assert not calls


@pytest.mark.asyncio
async def test_chat_port_alone_does_not_bypass_ambiguous_ui_preflight(local_services):
    registry, state, _calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].update({9796, 11004})
    with TestClient(app) as client:
        response = client.post("/api/maso/preflight", json={})
    assert response.json()["available"] is False
    with pytest.raises(MasoServiceUnavailable, match="Multiple local"):
        await discover_maso_service({"MASO_PORT": "11004"})


@pytest.mark.asyncio
@pytest.mark.parametrize("oversize", ["bytes", "instances"])
async def test_registry_limits_require_explicit_configuration(local_services, oversize):
    registry, _state, calls = local_services
    if oversize == "bytes":
        registry.write_bytes(b" " * 65537)
    else:
        registry.write_text(json.dumps({str(p): {"port": p} for p in range(11000, 11020)}))
    with pytest.raises(MasoServiceUnavailable, match="MASO_BASE_URL"):
        await discover_maso_service({})
    assert not calls


@pytest.mark.asyncio
async def test_all_candidates_share_a_bounded_read_only_probe(local_services):
    registry, state, calls = local_services
    registry.write_text(json.dumps({str(p): {"port": p} for p in range(11000, 11005)}))
    state["stall"] = True
    started = asyncio.get_running_loop().time()
    with pytest.raises(MasoServiceUnavailable):
        await discover_maso_service({})
    assert asyncio.get_running_loop().time() - started < 4.5
    assert len(calls) == 6


def test_preflight_returns_the_discovered_url_and_never_creates_a_session(local_services):
    registry, state, calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].add(11004)
    with TestClient(app) as client:
        response = client.post("/api/maso/preflight", json={})
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "base_url": "http://127.0.0.1:11004",
        "message": "MASO service is ready.",
        "model_count": 1,
    }
    assert all(method == "GET" for method, _url in calls)


@pytest.mark.asyncio
async def test_created_chat_pins_discovered_url_after_registry_changes(local_services, monkeypatch):
    registry, state, _calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].add(11004)

    async def inert_start(_self):
        return False

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", inert_start)
    monkeypatch.setattr(TTYDProcess, "start", inert_start)
    manager = TTYDManager()
    tab = await manager.create_tab(
        name="discovered", agent_type=AgentType.MASO, session_kind=SessionKind.CHAT
    )
    assert tab.env["MASO_BASE_URL"] == "http://127.0.0.1:11004"
    registry.write_text('{"b": {"port": 11006}}')
    state["ready"] = {11006}
    assert (await discover_maso_service({}))[0] == "http://127.0.0.1:11006"
    restored = TTYDManager().processes[tab.id]
    assert restored.env["MASO_BASE_URL"] == "http://127.0.0.1:11004"
    with pytest.raises(ValueError, match="new MASO Chat"):
        await restored.switch_env({"MASO_BASE_URL": "http://127.0.0.1:11006"})


def test_startup_probe_reports_address_and_missing_maso_does_not_block_hub(local_services, capsys):
    registry, state, _calls = local_services
    registry.write_text('{"a": {"port": 11004}}')
    state["ready"].add(11004)
    assert probe_main([]) == 0
    assert json.loads(capsys.readouterr().out)["base_url"] == "http://127.0.0.1:11004"
    state["ready"].clear()
    assert probe_main(["--startup"]) == 0
    assert "MASO unavailable" in capsys.readouterr().out
    assert probe_main([]) == 1
    assert json.loads(capsys.readouterr().out)["available"] is False
