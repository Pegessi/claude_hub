import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient) -> None:
    """Test that the health check endpoint works."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient) -> None:
    """The configured root serves either the production SPA or API metadata."""
    response = await client.get("/")
    assert response.status_code == 200
    if response.headers["content-type"].startswith("text/html"):
        assert "<!doctype html" in response.text.lower()
    else:
        data = response.json()
        assert data["message"] == "Claude Hub API"
        assert "version" in data
        assert data["docs"] == "/docs"
