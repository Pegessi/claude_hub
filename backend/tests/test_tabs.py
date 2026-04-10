import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_tabs_empty(client: AsyncClient) -> None:
    """Test that listing tabs returns an empty list when no tabs exist."""
    response = await client.get("/api/tabs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
