"""Tests for the local filesystem listing API.

In the test environment auth is disabled, so the ``get_current_user`` dependency
yields a dummy local user and these endpoints are reachable without a cookie.
Listings are driven against real ``tmp_path`` trees to exercise the 404/400
error branches and the directories-first sort order.
"""

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from claude_hub.api.filesystem import safe_list_dir


@pytest.mark.asyncio
async def test_list_directory_returns_sorted_dirs_first(client: AsyncClient, tmp_path) -> None:
    (tmp_path / "b_file.txt").write_text("x")
    (tmp_path / "A_dir").mkdir()
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("x")

    resp = await client.get("/api/filesystem/list", params={"path": str(tmp_path)})

    assert resp.status_code == 200
    body = resp.json()
    names = [item["name"] for item in body["items"]]
    # Directories first (case-insensitive sort), then files.
    assert names == ["A_dir", "z_dir", "a_file.txt", "b_file.txt"]
    assert body["current_path"] == str(tmp_path.resolve())
    assert body["parent_path"] == str(tmp_path.resolve().parent)


@pytest.mark.asyncio
async def test_list_directory_reports_dir_and_symlink_flags(client: AsyncClient, tmp_path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    resp = await client.get("/api/filesystem/list", params={"path": str(tmp_path)})

    items = {item["name"]: item for item in resp.json()["items"]}
    assert items["real"]["is_dir"] is True
    assert items["real"]["is_symlink"] is False
    assert items["link"]["is_symlink"] is True


@pytest.mark.asyncio
async def test_list_missing_path_returns_404(client: AsyncClient, tmp_path) -> None:
    resp = await client.get("/api/filesystem/list", params={"path": str(tmp_path / "nope")})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_file_path_returns_400(client: AsyncClient, tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("hi")

    resp = await client.get("/api/filesystem/list", params={"path": str(file_path)})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_defaults_to_home_when_no_path(client: AsyncClient) -> None:
    resp = await client.get("/api/filesystem/list")
    assert resp.status_code == 200
    # "~" expands to the user's home directory.
    assert resp.json()["current_path"]


@pytest.mark.asyncio
async def test_home_endpoint_returns_home_path(client: AsyncClient) -> None:
    from pathlib import Path

    resp = await client.get("/api/filesystem/home")

    assert resp.status_code == 200
    assert resp.json() == str(Path.home())


def test_safe_list_dir_raises_404_for_missing_path(tmp_path) -> None:
    with pytest.raises(HTTPException) as exc:
        safe_list_dir(str(tmp_path / "missing"))
    assert exc.value.status_code == 404


def test_safe_list_dir_raises_400_for_file(tmp_path) -> None:
    file_path = tmp_path / "f.txt"
    file_path.write_text("x")
    with pytest.raises(HTTPException) as exc:
        safe_list_dir(str(file_path))
    assert exc.value.status_code == 400
