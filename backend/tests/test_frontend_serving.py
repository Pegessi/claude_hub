"""Tests for production frontend serving (FastAPI StaticFiles mount).

When ``serve_frontend`` is enabled and a built ``frontend/dist`` exists, the
backend serves the SPA at the same origin as the API. Otherwise it falls back
to a JSON root endpoint (dev/CI without a build).
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from claude_hub.main import register_frontend


@pytest.mark.asyncio
async def test_register_frontend_serves_built_spa(tmp_path) -> None:
    """With serve=True and a dist present, "/" serves index.html and assets resolve."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>Hub</title></head><body>SPA</body></html>"
    )
    (dist / "assets" / "app.js").write_text("console.log('hub')")
    (dist / "favicon.svg").write_text("<svg></svg>")

    app = FastAPI()
    register_frontend(app, serve=True, dist=dist)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert "SPA" in root.text

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        favicon = await client.get("/favicon.svg")
        assert favicon.status_code == 200

        # No client-side router: unknown paths 404 rather than serving index.
        missing = await client.get("/no/such/route")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_register_frontend_falls_back_to_json_root(tmp_path) -> None:
    """With serve=False (or no dist), "/" returns the JSON API info."""
    dist = tmp_path / "dist"  # intentionally does not exist

    app = FastAPI()
    register_frontend(app, serve=False, dist=dist)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        data = root.json()
        assert data["message"] == "Claude Hub API"
        assert data["version"] == "0.1.0"
        assert data["docs"] == "/docs"
