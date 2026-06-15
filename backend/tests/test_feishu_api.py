"""Tests for the Feishu card-action result API endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from claude_hub.services.feishu_card_results import card_result_store


@pytest.fixture(autouse=True)
def _clean_store():
    """Each test starts and ends with an empty shared store."""
    card_result_store._entries.clear()
    yield
    card_result_store._entries.clear()


@pytest.mark.asyncio
async def test_register_returns_pending(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/feishu/cards/register",
        json={"token": "tok-abc123", "chat_id": "oc_1", "kind": "approval"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["token"] == "tok-abc123"
    assert data["status"] == "pending"
    assert data["chat_id"] == "oc_1"
    assert data["kind"] == "approval"


@pytest.mark.asyncio
async def test_get_unknown_token(client: AsyncClient) -> None:
    resp = await client.get("/api/feishu/cards/result/never-registered")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unknown"
    assert data["token"] == "never-registered"


@pytest.mark.asyncio
async def test_register_submit_get_round_trip(client: AsyncClient) -> None:
    token = "tok-roundtrip"
    reg = await client.post(
        "/api/feishu/cards/register",
        json={"token": token, "chat_id": "oc_1", "kind": "needs_input"},
    )
    assert reg.status_code == 200

    sub = await client.post(
        "/api/feishu/cards/result",
        json={
            "token": token,
            "action": "submit",
            "form": {"reply": "ship it"},
            "operator_id": "ou_x",
        },
    )
    assert sub.status_code == 200, sub.text
    sub_data = sub.json()
    assert sub_data["status"] == "resolved"
    assert sub_data["action"] == "submit"
    assert sub_data["form"] == {"reply": "ship it"}
    assert sub_data["operator_id"] == "ou_x"

    got = await client.get(f"/api/feishu/cards/result/{token}")
    assert got.status_code == 200
    assert got.json()["status"] == "resolved"


@pytest.mark.asyncio
async def test_submit_unknown_token_409(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/feishu/cards/result",
        json={"token": "ghost-token", "action": "approve"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_double_submit_409(client: AsyncClient) -> None:
    token = "tok-double"
    await client.post("/api/feishu/cards/register", json={"token": token})
    first = await client.post(
        "/api/feishu/cards/result", json={"token": token, "action": "approve"}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/feishu/cards/result", json={"token": token, "action": "reject"}
    )
    assert second.status_code == 409
    # First decision must stand.
    got = await client.get(f"/api/feishu/cards/result/{token}")
    assert got.json()["action"] == "approve"


@pytest.mark.asyncio
async def test_register_rejects_short_token(client: AsyncClient) -> None:
    resp = await client.post("/api/feishu/cards/register", json={"token": "short"})
    assert resp.status_code == 422
