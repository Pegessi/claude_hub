import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch


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

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.set_tab_order", fake_set_tab_order
    )

    response = await client.put("/api/tabs/order", json={"tab_ids": []})

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert captured_order == []
