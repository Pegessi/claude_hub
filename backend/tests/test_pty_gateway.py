"""Tests for the PTY-gateway remote transport.

Covers:
* capability resolution (normal / pty_gateway / browse-only tri-state) and the
  interactive gate (gateways allowed, only explicit browse-only rejected);
* PTY-exec prompt detection, sentinel framing/parsing, and timeout propagation;
* the IO-free bootstrap handshake state machine;
* the typed-bridge launcher argv and reverse-forward parameters;
* one-shot capture + directory-listing routing (PTY for gateway, argv ssh for a
  normal host regression).

Nothing here opens a network socket. The end-to-end handshake test drives a
local ``bash`` under a PTY as a stand-in for the remote gateway shell.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys

import pytest

from claude_hub.models import RemoteProfile
from claude_hub.models.schemas import (
    NONINTERACTIVE_REMOTE_UNSUPPORTED,
    ExecutionTarget,
    RemoteTransport,
)
from claude_hub.services import pty_exec
from claude_hub.services.pty_exec import PtyExecError
from claude_hub.services.pty_gateway_bridge import BootstrapHandshake, build_bootstrap_line
from claude_hub.services.remote_profiles import (
    profile_capabilities,
    profile_uses_stdin_shell,
    reject_unsupported_interactive,
    resolve_transport,
)

# The package ``claude_hub.services.ttyd_manager`` re-exports a manager singleton
# that shadows the submodule attribute; reach the real module via sys.modules.
tmod = sys.modules["claude_hub.services.ttyd_manager"]


# --------------------------------------------------------------------------- #
# Capability resolution
# --------------------------------------------------------------------------- #


def test_transport_detects_gateway_signatures() -> None:
    cases = [
        RemoteProfile(id="merlin_dev", name="m", ssh_host="merlin_dev"),
        RemoteProfile(id="p", name="p", ssh_host="merlin-ssh-proxy-cn.byted.org", user="u"),
        RemoteProfile(
            id="c",
            name="c",
            ssh_host="ssh-candy.workspace.byted.org",
            user="a.worker_0.seedjob.x",
        ),
        RemoteProfile(id="w", name="w", ssh_host="whatever", user="a.worker_9.seedjob.x"),
    ]
    for profile in cases:
        caps = profile_capabilities(profile)
        assert caps.transport == RemoteTransport.PTY_GATEWAY
        assert caps.interactive_supported is True
        assert caps.requires_pty_exec is True
        assert caps.argv_command_works is False
        assert profile_uses_stdin_shell(profile) is True


def test_transport_normal_host() -> None:
    profile = RemoteProfile(id="mac_mini", name="m", ssh_host="mac-mini.local")
    caps = profile_capabilities(profile)
    assert caps.transport == RemoteTransport.NORMAL
    assert caps.argv_command_works is True
    assert caps.requires_pty_exec is False


def test_explicit_transport_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    forced = RemoteProfile(id="plain", name="p", ssh_host="plain.example")
    assert resolve_transport(forced) == RemoteTransport.NORMAL
    forced = forced.model_copy(update={"transport": RemoteTransport.PTY_GATEWAY})
    assert resolve_transport(forced) == RemoteTransport.PTY_GATEWAY
    # String value from JSON config is accepted.
    strp = RemoteProfile(id="plain2", name="p", ssh_host="plain2.example", transport="pty_gateway")
    assert resolve_transport(strp) == RemoteTransport.PTY_GATEWAY
    # Env override classifies otherwise-unknown hosts.
    monkeypatch.setenv("CLAUDE_HUB_PTY_GATEWAY_HOSTS", "custom-gateway.internal")
    envp = RemoteProfile(id="c", name="c", ssh_host="custom-gateway.internal")
    assert resolve_transport(envp) == RemoteTransport.PTY_GATEWAY


def test_gateway_is_interactive_but_explicit_browse_only_is_rejected() -> None:
    gateway = RemoteProfile(id="merlin_dev", name="m", ssh_host="merlin_dev")
    # A gateway no longer raises: it is interactive via the typed PTY.
    reject_unsupported_interactive(gateway)
    reject_unsupported_interactive(None)

    browse_only = RemoteProfile(id="x", name="x", ssh_host="merlin_dev", interactive=False)
    assert profile_capabilities(browse_only).interactive_supported is False
    with pytest.raises(ValueError, match="browse-only"):
        reject_unsupported_interactive(browse_only)
    try:
        reject_unsupported_interactive(browse_only)
    except ValueError as exc:
        assert str(exc) == NONINTERACTIVE_REMOTE_UNSUPPORTED


# --------------------------------------------------------------------------- #
# Prompt detection + sentinel framing
# --------------------------------------------------------------------------- #


def test_real_prompt_detection_skips_init_splash() -> None:
    assert not pty_exec.real_prompt_seen(b"<Trial worker_0 ...> init $ ")
    assert pty_exec.real_prompt_seen(b"tiger@n1-2-3:~$ ")
    assert pty_exec.real_prompt_seen(b"\x1b[01;32mroot@host\x1b[00m:/etc# ")


def test_guarded_line_is_single_line_and_roundtrips() -> None:
    command = "echo a; echo b"
    line = pty_exec.build_guarded_line(command, "TOK")
    assert "\n" not in line
    assert base64.b64encode(command.encode()).decode() in line

    stream = (line + "\r\n").encode() + b"__CHPBEGIN_TOK__\r\na\r\nb\r\n__CHPEND_TOK__:0\r\n"
    result = pty_exec.extract_guarded_result(stream, "TOK")
    assert result is not None
    assert result.body == "a\nb"
    assert result.returncode == 0

    # Echoed input alone (no executed end marker yet) is not a result.
    assert pty_exec.extract_guarded_result((line + "\npartial").encode(), "TOK") is None

    bad = b"__CHPBEGIN_Z__\r\nboom\r\n__CHPEND_Z__:42\r\n"
    rz = pty_exec.extract_guarded_result(bad, "Z")
    assert rz is not None and rz.returncode == 42 and rz.body == "boom"


def test_build_ssh_argv_is_tty_and_has_no_command() -> None:
    profile = RemoteProfile(
        id="m", name="m", ssh_host="merlin-ssh-proxy-cn.byted.org", user="u", port=22222
    )
    argv = pty_exec.build_ssh_argv(profile)
    assert argv[0] == "ssh"
    assert "-tt" in argv
    assert argv[-1] == "u@merlin-ssh-proxy-cn.byted.org"  # host is last, no argv command
    assert "-p" in argv and "22222" in argv


# --------------------------------------------------------------------------- #
# Bootstrap handshake state machine
# --------------------------------------------------------------------------- #


def test_handshake_gates_keys_until_two_prompts() -> None:
    bootstrap = build_bootstrap_line(
        "tmux has-session -t s || tmux new-session -d -s s; exec tmux attach -t s"
    )
    hs = BootstrapHandshake(bootstrap)
    # Connect flash must not trigger typing.
    assert hs.feed(b"<Trial worker_0> init ") == b""
    assert hs.feed(b"$ ") == b""
    assert not hs.ready
    # Prompt can straddle reads.
    assert hs.feed(b"tiger@host:") == b""
    assert hs.feed(b"~$ ") == b"\r"
    assert not hs.ready
    # Second prompt after the synchronising Enter releases the bootstrap.
    injected = hs.feed(b"\r\ntiger@host:~$ ")
    assert injected.endswith(b"\r") and injected.startswith(b"echo ")
    assert b"| base64 -d | bash" in injected
    assert hs.ready
    decoded = base64.b64decode(injected.split(b"echo ", 1)[1].split(b" | base64", 1)[0]).decode()
    assert "exec tmux attach" in decoded
    # Once ready, feed emits nothing (the bridge owns transparent passthrough).
    assert hs.feed(b"anything") == b""


# --------------------------------------------------------------------------- #
# pty_exec orchestration with a scripted channel
# --------------------------------------------------------------------------- #


class _ScriptedChannel:
    def __init__(self, *, fail: str | None = None, body: str = "ok") -> None:
        self.fail = fail
        self.body = body
        self.closed = False
        self.commands: list[str] = []

    async def wait_for_real_prompt(self, timeout: float = 0.0) -> None:
        if self.fail == "prompt":
            raise PtyExecError("timed out waiting for PTY condition")

    async def run_guarded(self, command: str, timeout: float = 0.0) -> pty_exec.PtyExecResult:
        self.commands.append(command)
        return pty_exec.PtyExecResult(body=self.body, returncode=0)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pty_exec_returns_body_and_closes() -> None:
    channel = _ScriptedChannel(body="payload")

    async def connector(_profile: RemoteProfile) -> _ScriptedChannel:
        return channel

    out = await pty_exec.pty_exec(
        RemoteProfile(id="m", name="m", ssh_host="m"), "cmd", connector=connector
    )
    assert out == "payload"
    assert channel.closed is True
    assert channel.commands == ["cmd"]


@pytest.mark.asyncio
async def test_pty_exec_propagates_handshake_failure() -> None:
    channel = _ScriptedChannel(fail="prompt")

    async def connector(_profile: RemoteProfile) -> _ScriptedChannel:
        return channel

    with pytest.raises(PtyExecError):
        await pty_exec.pty_exec(
            RemoteProfile(id="m", name="m", ssh_host="m"), "cmd", connector=connector
        )
    assert channel.closed is True  # channel is always cleaned up


@pytest.mark.asyncio
async def test_pty_exec_against_local_bash_pty() -> None:
    """End-to-end over a real PTY: local bash stands in for the gateway shell."""

    async def local_bash_connector(_profile: RemoteProfile) -> pty_exec.SshPty:
        env = dict(os.environ)
        env["PS1"] = "fakeuser@fakehost:~$ "
        env["TERM"] = "xterm"
        return await pty_exec.spawn_pty(["bash", "--noprofile", "--norc", "-i"], env=env)

    profile = RemoteProfile(id="fake", name="fake", ssh_host="fake", transport="pty_gateway")
    body = await pty_exec.pty_exec(
        profile,
        "echo GATEWAY_OK; for i in 1 2; do echo line$i; done",
        prompt_timeout=15,
        command_timeout=15,
        connector=local_bash_connector,
    )
    assert "GATEWAY_OK" in body
    assert "line1" in body and "line2" in body


# --------------------------------------------------------------------------- #
# Launcher construction
# --------------------------------------------------------------------------- #


_GW = RemoteProfile(
    id="merlin_dev", name="merlin_dev", ssh_host="merlin_dev", transport="pty_gateway"
)
_NORMAL = RemoteProfile(id="mac_mini", name="m", ssh_host="mac-mini.local", transport="normal")


def _process(profile_id: str, *, forward: int | None = None, reconnect: bool = True) -> object:
    return tmod.TTYDProcess(
        "deadbeef-1234",
        19173,
        "remote-tab",
        target=ExecutionTarget.REMOTE,
        remote_profile_id=profile_id,
        remote_cwd="~",
        remote_forward_port=forward,
        remote_reconnect=reconnect,
    )


def test_gateway_launcher_types_bootstrap_with_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tmod.remote_profile_manager,
        "get_profile",
        lambda pid: _GW if pid == "merlin_dev" else _NORMAL,
    )
    proc = _process("merlin_dev", forward=18173)
    launcher = proc._build_remote_launcher()
    assert launcher[0] in ("/bin/zsh", "/bin/bash")
    command = launcher[-1]
    assert "-m claude_hub.services.pty_gateway_bridge" in command
    assert "--target merlin_dev" in command
    assert "--remote-forward 18173:%d" % tmod.settings.port in command
    assert "--reconnect" in command
    # The typed bootstrap is the remote tmux attach script, base64-encoded.
    b64 = command.split("--bootstrap-b64 ", 1)[1].split(" ", 1)[0]
    decoded = base64.b64decode(b64).decode()
    assert proc.tmux_session in decoded
    assert "tmux attach-session" in decoded


def test_gateway_launcher_no_forward_no_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tmod.remote_profile_manager,
        "get_profile",
        lambda pid: _GW if pid == "merlin_dev" else _NORMAL,
    )
    proc = _process("merlin_dev", forward=None, reconnect=False)
    command = proc._build_remote_launcher()[-1]
    assert "--remote-forward" not in command
    assert "--no-reconnect" in command


def test_normal_launcher_uses_argv_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tmod.remote_profile_manager,
        "get_profile",
        lambda pid: _NORMAL,
    )
    proc = _process("mac_mini")
    command = proc._build_remote_launcher()[-1]
    assert "ssh -tt" in command
    assert "pty_gateway_bridge" not in command


# --------------------------------------------------------------------------- #
# One-shot capture + listing routing
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout, self.stderr_bytes, self.returncode = stdout, stderr, returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self.stderr_bytes


@pytest.mark.asyncio
async def test_normal_capture_uses_argv_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmod.remote_profile_manager, "get_profile", lambda pid: _NORMAL)
    captured: list[list[str]] = []

    async def fake_exec(*argv: object, **_kwargs: object) -> _FakeProc:
        captured.append([str(a) for a in argv])
        return _FakeProc(stdout=b"history-bytes")

    monkeypatch.setattr(tmod.asyncio, "create_subprocess_exec", fake_exec)
    proc = _process("mac_mini")
    out = await proc._run_remote_capture_command("echo hi")
    assert out == "history-bytes"
    assert captured[0][0] == "ssh" and captured[0][1] == "-T"


@pytest.mark.asyncio
async def test_gateway_capture_uses_pty_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmod.remote_profile_manager, "get_profile", lambda pid: _GW)
    seen: list[str] = []

    async def fake_pty_exec(profile: RemoteProfile, command: str) -> str:
        seen.append(command)
        return "pty-bytes"

    monkeypatch.setattr(tmod.pty_exec, "pty_exec", fake_pty_exec)

    async def boom(*_a: object, **_k: object) -> _FakeProc:
        raise AssertionError("gateway must not spawn argv ssh")

    monkeypatch.setattr(tmod.asyncio, "create_subprocess_exec", boom)
    proc = _process("merlin_dev")
    out = await proc._run_remote_capture_command("echo hi")
    assert out == "pty-bytes"
    assert seen == ["echo hi"]


@pytest.mark.asyncio
async def test_gateway_listing_api_returns_payload(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from claude_hub.api import remote as remote_api

    payload = {
        "current_path": "/home/tiger",
        "parent_path": "/home",
        "items": [{"name": "opt", "path": "/home/tiger/opt", "is_dir": True, "is_symlink": False}],
    }

    async def fake_pty_exec(profile: RemoteProfile, command: str) -> str:
        assert "import json" in command
        return _json.dumps(payload)

    monkeypatch.setattr(remote_api.remote_profile_manager, "get_profile", lambda pid: _GW)
    monkeypatch.setattr(remote_api, "pty_exec", fake_pty_exec)

    resp = await client.get("/api/remote/filesystem/list", params={"profile_id": "merlin_dev"})
    assert resp.status_code == 200
    assert resp.json()["current_path"] == "/home/tiger"
