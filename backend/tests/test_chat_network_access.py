"""Per-turn/thread sandbox + ``networkAccess`` policy for Codex and TraeX.

Stock Codex used to send no ``sandboxPolicy``/``approvalPolicy`` for Chat, so
its seatbelt sandbox denied ALL outbound TCP (loopback included) and the agent
could not reach the local Hub (``Errno 1 Operation not permitted``). These
tests pin, for every Chat mode, the exact policy each provider emits and prove
that the full-outbound ``networkAccess`` lift is an **opt-in** knob
(``HUB_CHAT_ALLOW_NETWORK``) — never on by default, never attached to plan
(read-only) or solo (danger-full-access, already full egress).

The boolean-only, no-loopback-tier protocol fact behind the opt-in was verified
live against codex app-server 0.156.1 (``thread/start`` is permissive but
``turn/start`` serde rejects every non-bool ``networkAccess`` with
"expected a boolean"); see
``docs/working-logs/2026-09-25-codex-chat-network-access.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from claude_hub.models import AgentType, ChatMode
from claude_hub.services.agent_stream.native import (
    HUB_CHAT_NETWORK_ENV,
    CodexNativeSession,
    TraexNativeSession,
    _env_flag,
)
from tests.test_agent_stream_native import _FakeProcess, _session

_NET = HUB_CHAT_NETWORK_ENV


def _native(agent_type: AgentType, *, solo: bool, mode: ChatMode, env=None):
    session = _session(agent_type)
    session.solo_mode = solo
    session.chat_mode = mode
    if env is not None:
        session.env = dict(env)
    if agent_type == AgentType.CODEX:
        return CodexNativeSession(session)
    return TraexNativeSession(session)


# ── env-flag parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["1", "true", "TRUE", "True", "yes", "Y", "on", " 1 ", " On "],
)
def test_env_flag_truthy(value: str) -> None:
    assert _env_flag({_NET: value}, _NET) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "please", "2"])
def test_env_flag_falsy(value: str) -> None:
    assert _env_flag({_NET: value}, _NET) is False


def test_env_flag_missing_is_false() -> None:
    assert _env_flag({}, _NET) is False
    assert _env_flag({"OTHER": "1"}, _NET) is False


# ── turn/start policy: Codex + TraeX (identical camelCase sandboxPolicy) ─────


# (solo, mode, flag, expected approvalPolicy, expected sandboxPolicy)
TURN_CASES = [
    # default mode, non-solo: workspace-write, approval on-request; network is
    # opt-in and OFF by default (no networkAccess key at all).
    (False, ChatMode.DEFAULT, None, "on-request", {"type": "workspaceWrite"}),
    # Explicit false / garbage also leave the key off.
    (
        False,
        ChatMode.DEFAULT,
        "0",
        "on-request",
        {"type": "workspaceWrite"},
    ),
    (
        False,
        ChatMode.DEFAULT,
        "false",
        "on-request",
        {"type": "workspaceWrite"},
    ),
    # Opt-in lifts the workspace-write network ban — full outbound.
    (
        False,
        ChatMode.DEFAULT,
        "1",
        "on-request",
        {"type": "workspaceWrite", "networkAccess": True},
    ),
    (
        False,
        ChatMode.DEFAULT,
        "true",
        "on-request",
        {"type": "workspaceWrite", "networkAccess": True},
    ),
    # Solo default-mode: never + dangerFullAccess; the flag is ignored because
    # danger-full-access already grants full egress.
    (True, ChatMode.DEFAULT, None, "never", {"type": "dangerFullAccess"}),
    (
        True,
        ChatMode.DEFAULT,
        "1",
        "never",
        {"type": "dangerFullAccess"},
    ),
    # Plan overrides solo → read-only + on-request; network never attached
    # even with the flag (the model issues no shell commands in plan).
    (True, ChatMode.PLAN, None, "on-request", {"type": "readOnly"}),
    (True, ChatMode.PLAN, "1", "on-request", {"type": "readOnly"}),
    (False, ChatMode.PLAN, "1", "on-request", {"type": "readOnly"}),
]


@pytest.mark.parametrize(
    "solo,mode,flag,approval,sandbox",
    TURN_CASES,
)
def test_codex_turn_config_matrix(
    solo: bool, mode: ChatMode, flag, approval: str, sandbox: dict
) -> None:
    env = {_NET: flag} if flag is not None else None
    native = _native(AgentType.CODEX, solo=solo, mode=mode, env=env)
    config = native._turn_config()
    assert config["approvalPolicy"] == approval
    assert config["sandboxPolicy"] == sandbox


@pytest.mark.parametrize(
    "solo,mode,flag,approval,sandbox",
    TURN_CASES,
)
def test_traex_turn_config_matrix(
    solo: bool, mode: ChatMode, flag, approval: str, sandbox: dict
) -> None:
    env = {_NET: flag} if flag is not None else None
    native = _native(AgentType.TRAEX, solo=solo, mode=mode, env=env)
    config = native._turn_config()
    assert config["approvalPolicy"] == approval
    assert config["sandboxPolicy"] == sandbox


# ── thread/start config ──────────────────────────────────────────────────────


def test_codex_thread_config_defaults_to_workspace_write_with_cwd() -> None:
    native = _native(AgentType.CODEX, solo=False, mode=ChatMode.DEFAULT)
    assert native._thread_config() == {
        "cwd": "/tmp",
        "approvalPolicy": "on-request",
        "sandboxPolicy": {"type": "workspaceWrite"},
    }


def test_codex_thread_config_carries_network_flag() -> None:
    native = _native(AgentType.CODEX, solo=False, mode=ChatMode.DEFAULT, env={_NET: "yes"})
    assert native._thread_config() == {
        "cwd": "/tmp",
        "approvalPolicy": "on-request",
        "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True},
    }


def test_traex_thread_config_uses_kebab_sandbox_and_no_network_field() -> None:
    # TraeX thread/start takes the kebab sandbox string; the network lift lives
    # only on turn/start's sandboxPolicy (the thread field cannot carry it).
    native = _native(AgentType.TRAEX, solo=False, mode=ChatMode.DEFAULT, env={_NET: "1"})
    config = native._thread_config()
    assert config["cwd"] == "/tmp"
    assert config["approvalPolicy"] == "on-request"
    assert config["sandbox"] == "workspace-write"
    assert "sandboxPolicy" not in config
    assert "networkAccess" not in config


def test_traex_thread_config_plan_and_solo_kebab() -> None:
    plan = _native(AgentType.TRAEX, solo=True, mode=ChatMode.PLAN)
    assert plan._thread_config()["sandbox"] == "read-only"
    solo = _native(AgentType.TRAEX, solo=True, mode=ChatMode.DEFAULT)
    assert solo._thread_config()["sandbox"] == "danger-full-access"


# ── end-to-end: the flag reaches the actual turn/start JSON-RPC params ───────


async def test_codex_turn_start_includes_network_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native(AgentType.CODEX, solo=False, mode=ChatMode.DEFAULT, env={_NET: "1"})
    native._started = True
    native._process = _FakeProcess([])
    native._thread_id = "thread"
    send = AsyncMock(return_value={"turn": {"id": "turn"}})
    monkeypatch.setattr(native, "_send_request", send)
    await native.send_message("run", [])
    params = send.await_args.args[1]
    assert params["sandboxPolicy"] == {"type": "workspaceWrite", "networkAccess": True}
    assert params["approvalPolicy"] == "on-request"


async def test_codex_turn_start_omits_network_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native(AgentType.CODEX, solo=False, mode=ChatMode.DEFAULT)
    native._started = True
    native._process = _FakeProcess([])
    native._thread_id = "thread"
    send = AsyncMock(return_value={"turn": {"id": "turn"}})
    monkeypatch.setattr(native, "_send_request", send)
    await native.send_message("run", [])
    assert send.await_args.args[1]["sandboxPolicy"] == {"type": "workspaceWrite"}


# ── Claude is untouched ──────────────────────────────────────────────────────


async def test_claude_command_unaffected_by_network_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.services.agent_stream.native import ClaudeNativeSession

    def build(flag: str | None):
        session = _session(AgentType.CLAUDE)
        session.solo_mode = True
        if flag is not None:
            session.env = {_NET: flag}
        transport = ClaudeNativeSession(session)
        return transport._build_command()

    without = build(None)
    with_flag = build("1")
    assert without == with_flag
    assert "--dangerously-skip-permissions" in without
    assert not any("networkAccess" in part or "sandboxPolicy" in part for part in without)
