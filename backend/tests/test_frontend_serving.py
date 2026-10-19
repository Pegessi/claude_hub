"""Tests for production frontend serving (FastAPI StaticFiles mount).

When ``serve_frontend`` is enabled and a built ``frontend/dist`` exists, the
backend serves the SPA at the same origin as the API. Otherwise it falls back
to a JSON root endpoint (dev/CI without a build).
"""

from urllib.parse import parse_qs, quote, urlparse

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


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("custom_404", [False, True])
async def test_missing_absolute_html_link_redirects_to_registered_reader(
    tmp_path, method, custom_404
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA")
    if custom_404:
        (dist / "404.html").write_text("custom missing")
    app = FastAPI()
    register_frontend(app, serve=True, dist=dist)
    path = "/mnt/hdfs/reports/记录 # &?.HTML"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, quote(path, safe="/"))
        assert response.status_code == 307
        location = urlparse(response.headers["location"])
        assert location.path == "/"
        assert parse_qs(location.query) == {"attachment": [path], "registered": ["1"]}
        assert response.headers["cache-control"] == "no-store"
        assert "<!doctype" not in response.text
        shell = await client.get(response.headers["location"])
        assert shell.status_code == 200 and shell.text == "SPA"


@pytest.mark.parametrize(
    "path",
    [
        "/api/absent.html",
        "/assets/absent.html",
        "/static/absent.htm",
        "/missing.json",
        "/missing",
        "/mnt/%2e%2e/private.html",
        "/mnt/bad%5cname.html",
    ],
)
async def test_html_redirect_does_not_change_other_missing_routes(tmp_path, path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA")
    app = FastAPI()
    register_frontend(app, serve=True, dist=dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert "location" not in response.headers


async def test_existing_html_api_and_static_methods_keep_their_behavior(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA")
    (dist / "help.html").write_text("bundled help")
    app = FastAPI()

    @app.get("/api/existing.html")
    async def existing_api():
        return {"source": "api"}

    register_frontend(app, serve=True, dist=dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        static = await client.get("/help.html")
        assert static.status_code == 200 and static.text == "bundled help"
        assert "location" not in static.headers
        api = await client.get("/api/existing.html")
        assert api.json() == {"source": "api"}
        missing_post = await client.post("/mnt/report.html")
        assert missing_post.status_code == 405
        assert "location" not in missing_post.headers
