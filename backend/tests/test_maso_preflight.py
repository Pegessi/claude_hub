"""Readiness is bounded, read-only, and enforced before allocating a Chat."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.main import app
from claude_hub.services.agent_stream.maso_http import (
    MasoServiceUnavailable,
    maso_model_catalog,
)
from claude_hub.services.ttyd_manager import TTYDProcess


@pytest.fixture
def maso_http(monkeypatch):
    calls = []
    state = {"failure": None, "models": [{"key": "seed"}], "default": {"key": "seed"}}
    original = httpx.AsyncClient

    async def handle(request):
        calls.append((request.method, str(request.url)))
        failure = state["failure"]
        if failure == "connect":
            raise httpx.ConnectError("private upstream detail must not be exposed")
        if failure == "http":
            return httpx.Response(503, text="secret upstream response")
        if failure == "json":
            return httpx.Response(200, text="not json")
        if request.url.path == "/api/models":
            return httpx.Response(200, json=state["models"])
        assert request.url.path == "/api/models/default"
        return httpx.Response(200, json=state["default"])

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state, calls


def test_preflight_probes_hub_default_and_override_without_creating_session(maso_http, monkeypatch):
    state, calls = maso_http
    monkeypatch.setenv("MASO_BASE_URL", "http://127.0.0.1:9796")
    with TestClient(app) as client:
        response = client.post("/api/maso/preflight", json={})
        assert response.status_code == 200
        assert response.json() == {
            "available": True,
            "base_url": "http://127.0.0.1:9796",
            "message": "MASO service is ready.",
            "model_count": 1,
        }
        response = client.post(
            "/api/maso/preflight", json={"base_url": "http://maso-other.test:9796/"}
        )
        assert response.json()["base_url"] == "http://maso-other.test:9796"
    assert [url for _, url in calls] == [
        "http://127.0.0.1:9796/api/models",
        "http://127.0.0.1:9796/api/models/default",
        "http://maso-other.test:9796/api/models",
        "http://maso-other.test:9796/api/models/default",
    ]
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("failure", ["connect", "http", "json", "empty", "default"])
def test_preflight_unavailable_explains_failure_without_exposing_upstream(maso_http, failure):
    state, calls = maso_http
    state["failure"] = failure
    if failure == "empty":
        state["models"] = []
    if failure == "default":
        state["default"] = {"key": "unknown"}
    with TestClient(app) as client:
        response = client.post("/api/maso/preflight", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False and body["model_count"] == 0
    assert "retry" in body["message"].lower()
    assert "secret" not in json.dumps(body) and "private upstream" not in json.dumps(body)
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize(
    "address",
    [
        "http://user:secret@maso.test",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:99999",
    ],
)
def test_invalid_preflight_address_rejected_before_http(maso_http, address):
    _state, calls = maso_http
    with TestClient(app) as client:
        response = client.post("/api/maso/preflight", json={"base_url": address})
    assert response.status_code == 400
    assert "secret" not in response.text
    assert not calls


def test_create_rechecks_unavailable_service_before_tmux_or_tab_state(maso_http, monkeypatch):
    state, calls = maso_http

    async def forbidden_start(_self):
        pytest.fail("unavailable MASO must not allocate tmux")

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", forbidden_start)
    with TestClient(app) as client:
        # A formerly successful preflight is not an admission token.
        assert client.post("/api/maso/preflight", json={}).json()["available"]
        state["failure"] = "connect"
        before = client.get("/api/tabs").json()
        response = client.post(
            "/api/tabs",
            json={
                "name": "must not create",
                "agent_type": "maso",
                "session_kind": "chat",
            },
        )
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"]
        assert client.get("/api/tabs").json() == before
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_preflight_wall_clock_deadline_includes_both_requests() -> None:
    calls = []

    async def stalled(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # Each response individually fits the 2s HTTP deadline, but their
        # combined duration must still exceed the entire 3s probe budget.
        await asyncio.sleep(1.6)
        return httpx.Response(
            200, json=[{"key": "seed"}] if request.url.path == "/api/models" else {"key": "seed"}
        )

    async with httpx.AsyncClient(
        base_url="http://maso.test",
        transport=httpx.MockTransport(stalled),
        trust_env=False,
    ) as client:
        started = asyncio.get_running_loop().time()
        with pytest.raises(MasoServiceUnavailable, match="unavailable"):
            await maso_model_catalog(client)
        assert asyncio.get_running_loop().time() - started < 4.0
        assert calls == ["/api/models", "/api/models/default"]
