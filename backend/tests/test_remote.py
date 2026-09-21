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
from claude_hub.api.remote import parse_remote_listing_stdout, remote_listing_command
from claude_hub.models import RemoteProfile
from claude_hub.services.remote_profiles import profile_uses_stdin_shell


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
        self.received_input: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.received_input = input
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


def test_parse_remote_listing_stdout_skips_motd() -> None:
    payload = {
        "current_path": "/home/tiger",
        "parent_path": "/home",
        "items": [{"name": "src", "path": "/home/tiger/src", "is_dir": True, "is_symlink": False}],
    }
    raw = "Welcome to merlin\n{not json\n" + json.dumps(payload) + "\n"
    assert parse_remote_listing_stdout(raw)["current_path"] == "/home/tiger"


def test_parse_remote_listing_stdout_rejects_noise() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_remote_listing_stdout("Trial worker init $")


@pytest.mark.asyncio
async def test_listing_with_motd_returns_payload(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    payload = {
        "current_path": "/home/deploy",
        "parent_path": "/home",
        "items": [
            {"name": "app", "path": "/home/deploy/app", "is_dir": True, "is_symlink": False},
        ],
    }
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured.append([str(a) for a in args])
        return FakeProcess(stdout=b"login banner\n" + json.dumps(payload).encode())

    _patch_profile(monkeypatch, _profile())
    monkeypatch.setattr(remote_api.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "prod"})

    assert resp.status_code == 200
    assert resp.json()["current_path"] == "/home/deploy"
    ssh_cmd = captured[0]
    assert ssh_cmd[:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in ssh_cmd
    assert "RequestTTY=no" in ssh_cmd
    assert ssh_cmd[-1] != "prod"
    assert "python3 -c" in ssh_cmd[-1]


def test_profile_uses_stdin_shell_for_merlin_and_seedjob() -> None:
    assert profile_uses_stdin_shell(
        RemoteProfile(id="merlin_dev", name="merlin_dev", ssh_host="merlin_dev")
    )
    assert profile_uses_stdin_shell(
        RemoteProfile(id="merlin_dev_2", name="merlin_dev_2", ssh_host="merlin_dev_2")
    )
    assert profile_uses_stdin_shell(
        RemoteProfile(id="merlin_dev_evo", name="merlin_dev_evo", ssh_host="merlin_dev_evo")
    )
    assert not profile_uses_stdin_shell(
        RemoteProfile(
            id="merlin_persistent",
            name="merlin_persistent",
            ssh_host="merlin_persistent",
        )
    )
    assert profile_uses_stdin_shell(
        RemoteProfile(
            id="proxy",
            name="proxy",
            ssh_host="merlin-ssh-proxy-cn.byted.org",
            user="abc.worker_0.seedjob.wangzehua-ict",
        )
    )
    assert profile_uses_stdin_shell(
        RemoteProfile(
            id="candy",
            name="candy",
            ssh_host="ssh-candy-wlby.workspace.byted.org",
            user="abc.worker_0.seedjob.wangzehua-ict",
        )
    )
    assert not profile_uses_stdin_shell(_profile())


@pytest.mark.asyncio
async def test_merlin_listing_sends_command_on_stdin(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    payload = {
        "current_path": "/root",
        "parent_path": "/",
        "items": [{"name": "opt", "path": "/root/opt", "is_dir": True, "is_symlink": False}],
    }
    captured: list[list[str]] = []
    proc = FakeProcess(stdout=json.dumps(payload).encode())

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured.append([str(a) for a in args])
        assert kwargs.get("stdin") is not None
        return proc

    _patch_profile(
        monkeypatch,
        RemoteProfile(id="merlin_dev", name="merlin_dev", ssh_host="merlin_dev"),
    )
    monkeypatch.setattr(remote_api.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "merlin_dev"})

    assert resp.status_code == 200
    assert resp.json()["current_path"] == "/root"
    ssh_cmd = captured[0]
    assert ssh_cmd[-1] == "merlin_dev"
    assert "python3" not in " ".join(ssh_cmd)
    assert proc.received_input is not None
    assert proc.received_input.startswith(b"python3 -c")
    assert proc.received_input.endswith(b"\n")
    assert proc.received_input.count(b"\n") == 1
    assert b"base64" in proc.received_input


def test_stdin_shell_listing_command_is_one_line() -> None:
    command = remote_listing_command("~", stdin_shell=True)
    assert "\n" not in command
    assert command.startswith("python3 -c")
    argv_command = remote_listing_command("~", stdin_shell=False)
    assert "import json" in argv_command
    assert "\n" in argv_command
