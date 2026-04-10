from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from claude_hub.config import settings

# Create a simple test app just for basic endpoint testing
test_app = FastAPI(
    title="Claude Hub API",
    description="Test API",
    version="0.1.0",
)

# Add CORS middleware
test_app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add test endpoints
@test_app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@test_app.get("/")
async def root() -> dict[str, str]:
    return {
        "message": "Claude Hub API",
        "version": "0.1.0",
        "docs": "/docs",
    }


@test_app.get("/api/tabs")
async def list_tabs() -> list[dict[str, str]]:
    return []


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Test client for the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as test_client:
        yield test_client
