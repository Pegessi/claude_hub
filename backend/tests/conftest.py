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
async def health_check():
    return {"status": "healthy"}


@test_app.get("/")
async def root():
    return {
        "message": "Claude Hub API",
        "version": "0.1.0",
        "docs": "/docs",
    }


@pytest_asyncio.fixture
async def client():
    """Test client for the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as test_client:
        yield test_client
