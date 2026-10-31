"""The quota endpoint always answers 200, with one entry per provider.

A passive panel must keep reporting the providers it can read, so a failing
provider appears as an explained unavailable entry rather than an error status.
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.services import provider_quota as pq
from claude_hub.services.agent_stream import maso_http
from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable

FUTURE_RESET = 4102444800  # 2100-01-01, always ahead of the test clock

MASO_PAYLOAD = {
    "state": "within",
    "models": [
        {
            "model": "gpt-6-astra",
            "state": "within",
            "percentUsed": 14,
            "effectiveState": "within",
            "effectiveUsedPercent": 14,
            "resetAt": "2026-10-15T16:00:00.000Z",
        }
    ],
    "grants": [],
    "enforcing": True,
    "fetchedAt": 1792036198.71,
}


def _write_codex_reading(home: Path, used_percent: float = 58.0) -> None:
    """Write one complete Codex rollout under a redirected Codex home."""
    path = home / "sessions" / "2026" / "10" / "15" / "rollout-test.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-10-15T04:07:36.155Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10080,
                    "resets_at": FUTURE_RESET,
                },
                "secondary": None,
                "credits": {"has_credits": True, "unlimited": False, "balance": "62500"},
                "plan_type": "pro",
            },
        },
    }
    path.write_text("{}\n" + json.dumps(record) + "\n")
    os.utime(path, (1000.0, 1000.0))


@pytest.fixture(autouse=True)
def clear_quota_cache(tmp_path: Path, monkeypatch: MonkeyPatch):
    """Keep the module-level reading cache and host Antigravity home isolated."""
    setattr(pq, "_QUOTA_CACHE", None)
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(tmp_path / "antigravity-empty"))
    yield
    setattr(pq, "_QUOTA_CACHE", None)


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Redirect the Codex home at an empty temporary directory."""
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def maso_service(monkeypatch: MonkeyPatch):
    """Mock discovery plus the HTTP transport; never touch a live service."""
    state = {"payload": MASO_PAYLOAD}
    original = httpx.AsyncClient

    async def discover(env):
        return "http://maso.test", [], "seed"

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=state["payload"])

    def client(**kwargs):
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(pq, "discover_maso_service", discover)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


@pytest.mark.asyncio
async def test_quota_returns_both_providers_in_order(
    client: AsyncClient, codex_home: Path, maso_service
) -> None:
    _write_codex_reading(codex_home)

    response = await client.get("/api/quota")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [provider["provider"] for provider in providers] == [
        "codex",
        "maso",
        "antigravity",
    ]
    codex, maso, antigravity = providers
    assert codex["available"] is True
    assert codex["state"] == "within"
    assert codex["plan"] == "pro"
    assert codex["credit_balance"] == "62500"
    assert codex["enforcing"] is None
    assert codex["limits"][0]["scope"] == "weekly"
    assert codex["limits"][0]["used_percent"] == 58
    assert codex["limits"][0]["remaining_percent"] == 42
    assert maso["available"] is True
    assert maso["enforcing"] is True
    assert maso["plan"] is None and maso["credit_balance"] is None
    assert maso["limits"][0]["scope"] == "gpt-6-astra"
    assert antigravity["available"] is False
    assert antigravity["state"] == "not_configured"


@pytest.mark.asyncio
async def test_quota_keeps_serving_when_maso_cannot_be_discovered(
    client: AsyncClient, codex_home: Path, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _write_codex_reading(codex_home)
    # Pin the host signals: MASO is installed here (registry entry) but down,
    # which is the fault case rather than the not-configured one.
    monkeypatch.delenv("MASO_BASE_URL", raising=False)
    monkeypatch.delenv("MASO_PORT", raising=False)
    monkeypatch.setattr(maso_http.settings, "maso_base_url", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    registry = tmp_path / ".maso" / "instances.json"
    registry.parent.mkdir()
    registry.write_text('{"11004": {"port": 11004}}')

    async def discover(env):
        raise MasoServiceUnavailable(
            "MASO service is unavailable: no usable local service was found. "
            "Start MASO or set MASO_BASE_URL, then retry."
        )

    monkeypatch.setattr(pq, "discover_maso_service", discover)

    response = await client.get("/api/quota")

    assert response.status_code == 200
    codex, maso, antigravity = response.json()["providers"]
    assert codex["available"] is True and codex["limits"]
    assert maso["available"] is False
    assert maso["state"] == "unavailable"
    assert maso["limits"] == []
    assert "MASO" in maso["message"]
    assert antigravity["state"] == "not_configured"


@pytest.mark.asyncio
async def test_quota_reports_an_absent_maso_as_not_configured(
    client: AsyncClient, codex_home: Path, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A Codex-only deployment must not show a permanent MASO alarm."""
    _write_codex_reading(codex_home)
    monkeypatch.delenv("MASO_BASE_URL", raising=False)
    monkeypatch.delenv("MASO_PORT", raising=False)
    monkeypatch.setattr(maso_http.settings, "maso_base_url", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def discover(env):
        raise MasoServiceUnavailable(
            "MASO service is unavailable: no usable local service was found. "
            "Start MASO or set MASO_BASE_URL, then retry."
        )

    monkeypatch.setattr(pq, "discover_maso_service", discover)

    response = await client.get("/api/quota")

    assert response.status_code == 200
    codex, maso, antigravity = response.json()["providers"]
    assert codex["available"] is True and codex["limits"]
    assert maso["available"] is False
    assert maso["state"] == "not_configured"
    assert maso["limits"] == [] and maso["as_of"] is None
    assert "No MASO service is configured" in maso["message"]
    assert "Start MASO" not in maso["message"]
    assert antigravity["state"] == "not_configured"


@pytest.mark.asyncio
async def test_quota_keeps_serving_when_codex_has_no_reading(
    client: AsyncClient, codex_home: Path, maso_service
) -> None:
    response = await client.get("/api/quota")

    assert response.status_code == 200
    codex, maso, antigravity = response.json()["providers"]
    assert codex["available"] is False
    assert codex["state"] == "no_data"
    assert codex["message"]
    assert maso["available"] is True and maso["limits"]
    assert antigravity["state"] == "not_configured"


@pytest.mark.asyncio
async def test_quota_refresh_bypasses_the_cached_reading(
    client: AsyncClient, codex_home: Path, maso_service
) -> None:
    _write_codex_reading(codex_home, used_percent=10.0)
    assert (await client.get("/api/quota")).json()["providers"][0]["limits"][0][
        "used_percent"
    ] == 10

    _write_codex_reading(codex_home, used_percent=70.0)
    cached = await client.get("/api/quota")
    assert cached.json()["providers"][0]["limits"][0]["used_percent"] == 10

    refreshed = await client.get("/api/quota", params={"refresh": "true"})
    assert refreshed.status_code == 200
    assert refreshed.json()["providers"][0]["limits"][0]["used_percent"] == 70
