"""Proxy inheritance must work without an interactive shell or fresh tmux server."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from claude_hub.models import AgentType, ExecutionTarget
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    TraexNativeSession,
)
from claude_hub.services.agent_stream.provider_network import (
    PROVIDER_NETWORK_MODE_ENV,
    ProbeResult,
    ProviderNetworkSelector,
)
from claude_hub.services.proxy_rules import PROXY_ENV_KEYS, with_proxy_env_aliases
from claude_hub.services.ttyd_manager import TTYDProcess, _agent_spawn_env
from tests.test_agent_stream_native import _SAMPLE_LIST_MODELS, _FakeListModelsProc, _session


@pytest.fixture(autouse=True)
def isolated_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.mark.parametrize("name", sorted(PROXY_ENV_KEYS))
def test_case_aliases_preserve_explicit_empty_and_conflicting_values(name: str) -> None:
    original = {name: "", name.upper(): "explicit-other-value", "OTHER": "unchanged"}
    assert with_proxy_env_aliases(original) == original
    assert with_proxy_env_aliases({name: ""}) == {name: "", name.upper(): ""}
    assert with_proxy_env_aliases({name.upper(): None}) == {name: None, name.upper(): None}


@pytest.mark.parametrize(
    "session_cls,agent_type",
    [
        (ClaudeNativeSession, AgentType.CLAUDE),
        (CodexNativeSession, AgentType.CODEX),
        (CursorNativeSession, AgentType.CURSOR),
        (TraexNativeSession, AgentType.TRAEX),
    ],
)
@pytest.mark.parametrize("override", ["session.example", "", None])
def test_native_session_proxy_override_replaces_inherited_pair(
    monkeypatch: pytest.MonkeyPatch, session_cls: Any, agent_type: AgentType, override: str | None
) -> None:
    monkeypatch.setenv("NO_PROXY", "parent.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8118")
    session = _session(agent_type)
    session.env = cast(dict[str, str], {"no_proxy": override})

    env = session_cls(session)._build_env()

    assert env["https_proxy"] == env["HTTPS_PROXY"] == "http://proxy.example:8118"
    if override is None:
        assert "no_proxy" not in env and "NO_PROXY" not in env
    else:
        assert env["NO_PROXY"] == env["no_proxy"] == override
    assert os.environ["NO_PROXY"] == "parent.example"
    assert "no_proxy" not in os.environ
    assert "https_proxy" not in os.environ


@pytest.mark.asyncio
async def test_native_cursor_inherits_bypass_rules_without_shell_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("no_proxy", ".internal.example,localhost,fd00::/8")
    session = _session(AgentType.CURSOR)
    session.env = {}

    env = await CursorNativeSession(session)._build_provider_env()

    assert env["NO_PROXY"] == env["no_proxy"] == os.environ["no_proxy"]
    assert session.env == {}
    assert "NO_PROXY" not in os.environ


@pytest.mark.asyncio
async def test_cursor_catalog_probe_receives_inherited_proxy_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("no_proxy", ".internal.example")
    monkeypatch.setenv("https_proxy", "http://proxy.example:8118")
    captured: dict[str, Any] = {}

    async def spawn(*args: str, **kwargs: Any) -> _FakeListModelsProc:
        captured.update(kwargs)
        assert args == ("agent", "--list-models")
        return _FakeListModelsProc(_SAMPLE_LIST_MODELS.encode())

    monkeypatch.setattr(native_module.asyncio, "create_subprocess_exec", spawn)
    models, fallback = await native_module._probe_cursor_models()

    assert models and not fallback
    assert captured["env"]["NO_PROXY"] == ".internal.example"
    assert captured["env"]["HTTPS_PROXY"] == "http://proxy.example:8118"
    assert "NO_PROXY" not in os.environ


@pytest.mark.parametrize("override", [None, ""])
def test_local_terminal_wrapper_repairs_stale_tmux_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None
) -> None:
    proxy = "http://proxy-user:proxy-sensitive@proxy.example:8118"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", ".internal.example")
    process = TTYDProcess(
        tab_id="proxy-inheritance",
        port=12345,
        name="Proxy inheritance",
        agent_type=AgentType.CURSOR,
        env={} if override is None else {"no_proxy": override},
    )
    probe = (
        "import json,os;print(json.dumps({k:os.environ.get(k) "
        "for k in ('HTTPS_PROXY','https_proxy','NO_PROXY','no_proxy')}))"
    )
    command = process._with_env(shlex.join([sys.executable, "-c", probe]))
    wrapper = Path(shlex.split(command)[1])
    # An older tmux server can still carry stale proxy settings. The protected
    # wrapper must override those without placing proxy credentials in argv.
    result = subprocess.run(
        shlex.split(command),
        env={"HOME": str(tmp_path), "SHELL": "/bin/sh", "no_proxy": "stale.example"},
        capture_output=True,
        text=True,
        check=True,
    )
    env = json.loads(result.stdout)

    assert env["HTTPS_PROXY"] == env["https_proxy"] == proxy
    assert env["NO_PROXY"] == env["no_proxy"] == (".internal.example" if override is None else "")
    assert "proxy-sensitive" not in command
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o600
    assert "https_proxy" not in process.to_dict()["env"]
    assert "HTTPS_PROXY" not in process.to_dict()["env"]


def test_remote_launch_and_claude_settings_do_not_capture_local_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("https_proxy", "http://local-proxy.example:8118")
    monkeypatch.setenv("no_proxy", ".local-only.example")
    remote = TTYDProcess(
        tab_id="remote-proxy",
        port=12345,
        name="Remote",
        agent_type=AgentType.CURSOR,
        target=ExecutionTarget.REMOTE,
    )
    command = remote._with_env("agent")
    assert "local-proxy" not in command and "local-only" not in command
    claude = TTYDProcess(
        tab_id="claude-proxy-settings",
        port=12346,
        name="Claude settings",
        agent_type=AgentType.CLAUDE,
        env={"ANTHROPIC_BASE_URL": "https://provider.example"},
    )
    settings_path = claude._write_launch_settings_file()
    assert settings_path is not None
    settings_env = json.loads(settings_path.read_text())["env"]
    assert not any(key.lower() in PROXY_ENV_KEYS for key in settings_env)


@pytest.mark.parametrize("current_proxy", [None, "http://current-proxy.example:8118"])
def test_local_terminal_clears_removed_proxy_keys_from_old_tmux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current_proxy: str | None
) -> None:
    if current_proxy is not None:
        monkeypatch.setenv("https_proxy", current_proxy)
    process = TTYDProcess(
        tab_id="removed-proxy",
        port=12345,
        name="Removed proxy",
        agent_type=AgentType.CURSOR,
    )
    keys = sorted(name for lower in PROXY_ENV_KEYS for name in (lower, lower.upper()))
    probe = f"import json,os;print(json.dumps({{k:os.environ.get(k) for k in {keys!r}}}))"
    command = process._with_env(shlex.join([sys.executable, "-c", probe]))
    stale = {
        name: "*" if name.lower() == "no_proxy" else "http://stale.example:8118" for name in keys
    }

    result = subprocess.run(
        shlex.split(command),
        env={**stale, "HOME": str(tmp_path), "SHELL": "/bin/sh"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        name: current_proxy if name.lower() == "https_proxy" else None for name in keys
    }


def test_ttyd_spawn_environment_normalizes_without_changing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("no_proxy", ".internal.example")
    assert _agent_spawn_env()["NO_PROXY"] == ".internal.example"
    assert "NO_PROXY" not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "direct", "proxy", "inherit"])
async def test_normalization_keeps_network_route_selection_authoritative(mode: str) -> None:
    env = with_proxy_env_aliases(
        {
            PROVIDER_NETWORK_MODE_ENV: mode,
            "https_proxy": "http://proxy.example:8118",
            "no_proxy": ".internal.example",
        }
    )

    async def reachable(endpoint: str, proxy: str | None) -> ProbeResult:
        return ProbeResult(True, "http", 401)

    selected = await ProviderNetworkSelector(probe=reachable).select("cursor", env)

    assert selected["NO_PROXY"] == selected["no_proxy"] == ".internal.example"
    if mode in {"auto", "direct"}:
        assert "HTTPS_PROXY" not in selected and "https_proxy" not in selected
    else:
        assert selected["HTTPS_PROXY"] == selected["https_proxy"] == "http://proxy.example:8118"
