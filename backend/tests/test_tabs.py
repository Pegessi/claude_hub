from datetime import datetime

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.models import AgentType, TerminalTab


@pytest.mark.asyncio
async def test_list_tabs_empty(client: AsyncClient) -> None:
    """Test that listing tabs returns an empty list when no tabs exist."""
    response = await client.get("/api/tabs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_update_tab_order_route_not_shadowed(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Test that the static order route is not handled as a tab ID update."""
    captured_order: list[str] = []

    def fake_set_tab_order(tab_ids: list[str]) -> None:
        captured_order.extend(tab_ids)

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.set_tab_order", fake_set_tab_order)

    response = await client.put("/api/tabs/order", json={"tab_ids": []})

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert captured_order == []


@pytest.mark.asyncio
async def test_duplicate_tab_route_preserves_solo_mode(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    captured_tab_ids: list[str] = []

    async def fake_duplicate_tab(tab_id: str) -> TerminalTab:
        captured_tab_ids.append(tab_id)
        return TerminalTab(
            id="copy-id",
            name="Codex (copy)",
            shell="codex",
            cwd="/tmp",
            solo_mode=True,
            agent_type=AgentType.CODEX,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/source-id/duplicate")

    assert response.status_code == 201
    assert captured_tab_ids == ["source-id"]
    data = response.json()
    assert data["name"] == "Codex (copy)"
    assert data["solo_mode"] is True
    assert data["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_duplicate_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_duplicate_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/missing-id/duplicate")

    assert response.status_code == 404
