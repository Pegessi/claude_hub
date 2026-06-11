"""Tests for the remote directory listing API.

``list_remote_directory`` shells out to ``ssh`` via
``asyncio.create_subprocess_exec``. These tests stub that subprocess with a fake
process so the timeout (504), non-zero exit (502), invalid-JSON (502),
payload-error status mapping, missing-profile (404), and success paths are all
exercised without touching the network.
"""

import asyncio
import json

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.api import remote as remote_api
from claude_hub.models import RemoteProfile


class FakeProcess:
    """Stand-in for an asyncio subprocess returned by create_subprocess_exec."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr


def _profile() -> RemoteProfile:
    return RemoteProfile(id="prod", name="prod", ssh_host="example.invalid", user="deploy")


def _patch_profile(monkeypatch: MonkeyPatch, profile: RemoteProfile | None) -> None:
    monkeypatch.setattr(
        remote_api.remote_profile_manager,
        "get_profile",
        lambda profile_id: profile,
    )


def _patch_subprocess(monkeypatch: MonkeyPatch, proc: FakeProcess) -> None:
    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        return proc

    monkeypatch.setattr(
        remote_api.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )


@pytest.mark.asyncio
async def test_missing_profile_returns_404(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    _patch_profile(monkeypatch, None)

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "ghost"})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_successful_listing_returns_payload(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    payload = {
        "current_path": "/home/deploy",
        "parent_path": "/home",
        "items": [
            {"name": "app", "path": "/home/deploy/app", "is_dir": True, "is_symlink": False},
        ],
    }
    _patch_profile(monkeypatch, _profile())
    _patch_subprocess(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "prod"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_path"] == "/home/deploy"
    assert body["items"][0]["name"] == "app"


@pytest.mark.asyncio
async def test_timeout_returns_504(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    _patch_profile(monkeypatch, _profile())
    _patch_subprocess(monkeypatch, FakeProcess(hang=True))

    async def fast_wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        # Close the pending coroutine and surface the timeout the route handles.
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(remote_api.asyncio, "wait_for", fast_wait_for)

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "prod"})

    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_non_zero_exit_returns_502(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    _patch_profile(monkeypatch, _profile())
    _patch_subprocess(
        monkeypatch,
        FakeProcess(stderr=b"ssh: connect failed", returncode=255),
    )

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "prod"})

    assert resp.status_code == 502
    assert "connect failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_json_returns_502(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    _patch_profile(monkeypatch, _profile())
    _patch_subprocess(monkeypatch, FakeProcess(stdout=b"not json at all"))

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "prod"})

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_payload_error_status_is_propagated(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _patch_profile(monkeypatch, _profile())
    _patch_subprocess(
        monkeypatch,
        FakeProcess(stdout=json.dumps({"error": "Path not found", "status": 404}).encode()),
    )

    resp = await client.get(
        "/api/remote/filesystem/list",
        params={"profile_id": "prod", "path": "/nope"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Path not found"


@pytest.mark.asyncio
async def test_profiles_endpoint_lists_profiles(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(
        remote_api.remote_profile_manager,
        "list_profiles",
        lambda: [_profile()],
    )

    resp = await client.get("/api/remote/profiles")

    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "prod"
