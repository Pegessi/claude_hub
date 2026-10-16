"""Deployment-only .env defaults must reach children without becoming tab data."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from claude_hub.config import Settings
from claude_hub.models import AgentStreamEventType, AgentType, ExecutionTarget, SessionKind
from claude_hub.services import provider_proxy
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    TraexNativeSession,
)
from claude_hub.services.agent_stream.redaction import _redact_value, redact_event
from claude_hub.services.proxy_rules import PROXY_ENV_KEYS
from claude_hub.services.ttyd_manager import TTYDProcess, _agent_spawn_env
from tests.test_agent_stream import _ctx
from tests.test_agent_stream_native import _SAMPLE_LIST_MODELS, _FakeListModelsProc, _session

PROXY = "http://user:private-password@cursor-proxy.example.test:8080"


@pytest.fixture(autouse=True)
def private_deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    for key in list(os.environ):
        if key.lower() in PROXY_ENV_KEYS or key.lower().endswith(("_proxy_url", "_no_proxy")):
            monkeypatch.delenv(key)
    path = tmp_path / ".env"
    path.write_text(
        f"CURSOR_PROXY_URL={PROXY}\nCURSOR_NO_PROXY=localhost,127.0.0.1,::1,.internal.test\n"
        "CURSOR_MODELS_PROBE_TIMEOUT_SECONDS=15\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    config = Settings(_env_file=path)
    monkeypatch.setattr(provider_proxy, "settings", config)
    monkeypatch.setattr(native_module, "settings", config)
    return config


def test_dotenv_is_private_and_does_not_modify_process(
    private_deployment: Settings,
) -> None:
    assert private_deployment.cursor_proxy_url is not None
    assert private_deployment.cursor_proxy_url.get_secret_value() == PROXY
    assert private_deployment.cursor_models_probe_timeout_seconds == 15
    for serialized in (repr(private_deployment), private_deployment.model_dump_json()):
        assert "cursor-proxy" not in serialized
        assert "private-password" not in serialized
        assert "internal.test" not in serialized
    assert "CURSOR_PROXY_URL" not in os.environ
    assert "HTTPS_PROXY" not in os.environ


def test_process_setting_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "override.env"
    path.write_text(f"CURSOR_PROXY_URL={PROXY}\n", encoding="utf-8")
    monkeypatch.setenv("CURSOR_PROXY_URL", "direct")
    config = Settings(_env_file=path)
    assert config.cursor_proxy_url is not None
    assert config.cursor_proxy_url.get_secret_value() == "direct"


@pytest.mark.parametrize("provider", ["claude", "codex", "traex", "terminal", "maso"])
def test_other_providers_keep_process_defaults(provider: str) -> None:
    inherited = {
        "HTTPS_PROXY": "http://existing.example.test:3128",
        "OTHER": "unchanged",
    }
    result = provider_proxy.provider_subprocess_env(provider, inherited)
    assert result["https_proxy"] == inherited["HTTPS_PROXY"]
    assert result["OTHER"] == "unchanged"
    assert "NO_PROXY" not in result


def test_provider_defaults_replace_all_inherited_proxy_routes() -> None:
    inherited = {
        "HTTPS_PROXY": "http://existing.example.test:3128",
        "ALL_PROXY": "socks5://old.example.test:1080",
        "NO_PROXY": "*",
        "CLAUDE_PROXY_URL": "http://other-secret.example.test",
        "CURSOR_PROXY_URL": PROXY,
        "codex_no_proxy": "private.example.test",
    }
    result = provider_proxy.provider_subprocess_env("cursor", inherited)
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        assert result[key] == PROXY
    assert result["NO_PROXY"] == result["no_proxy"] == "localhost,127.0.0.1,::1,.internal.test"
    assert result["ALL_PROXY"] == result["all_proxy"] == ""
    assert not any(key.lower().endswith(("_proxy_url", "_no_proxy")) for key in result)
    assert inherited["NO_PROXY"] == "*"


@pytest.mark.parametrize("value", [None, "", "http://session.example.test:8080"])
def test_session_override_replaces_both_cases(value: str | None) -> None:
    result = provider_proxy.provider_subprocess_env("cursor", {}, {"https_proxy": value})
    if value is None:
        assert "HTTPS_PROXY" not in result and "https_proxy" not in result
    else:
        assert result["HTTPS_PROXY"] == result["https_proxy"] == value


@pytest.mark.parametrize("url", [None, "", "direct"])
def test_unset_empty_and_direct_are_distinct(
    url: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Settings(_env_file=None, cursor_proxy_url=SecretStr(url) if url is not None else None)
    monkeypatch.setattr(provider_proxy, "settings", config)
    inherited = {"https_proxy": PROXY, "ALL_PROXY": "socks5://old.example.test:1080"}
    result = provider_proxy.provider_subprocess_env("cursor", inherited)
    if url == "direct":
        assert all(result[key] == "" for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
    else:
        assert result["HTTPS_PROXY"] == PROXY
        assert result["all_proxy"] == inherited["ALL_PROXY"]


@pytest.mark.parametrize(
    "session_cls,agent_type",
    [
        (ClaudeNativeSession, AgentType.CLAUDE),
        (CodexNativeSession, AgentType.CODEX),
        (CursorNativeSession, AgentType.CURSOR),
        (TraexNativeSession, AgentType.TRAEX),
    ],
)
@pytest.mark.asyncio
async def test_native_providers_consume_their_own_dotenv(
    session_cls: Any,
    agent_type: AgentType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "providers.env"
    path.write_text(f"{agent_type.value.upper()}_PROXY_URL={PROXY}\n", encoding="utf-8")
    monkeypatch.setattr(provider_proxy, "settings", Settings(_env_file=path))
    session = _session(agent_type)
    session.env = {}
    env = await session_cls(session)._build_provider_env()
    assert env["HTTPS_PROXY"] == env["https_proxy"] == PROXY
    assert session.env == {}


@pytest.mark.asyncio
async def test_cursor_catalog_and_request_share_dotenv(
    monkeypatch: pytest.MonkeyPatch, private_deployment: Settings
) -> None:
    captured: dict[str, Any] = {}

    async def spawn(*args: str, **kwargs: Any) -> _FakeListModelsProc:
        captured.update(kwargs)
        return _FakeListModelsProc(_SAMPLE_LIST_MODELS.encode())

    monkeypatch.setattr(native_module.asyncio, "create_subprocess_exec", spawn)
    models, fallback = await native_module._probe_cursor_models()
    session = _session(AgentType.CURSOR)
    session.env = {}
    request_env = await CursorNativeSession(session)._build_provider_env()
    assert models and not fallback
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert request_env[key] == captured["env"][key]
    assert session.env == {}


def test_terminal_private_wrapper_hides_deployment_from_tab_and_argv(
    tmp_path: Path,
) -> None:
    process = TTYDProcess(
        tab_id="dotenv-cursor", port=12345, name="dotenv", agent_type=AgentType.CURSOR
    )
    probe = "import os,json; print(json.dumps({k:os.environ.get(k) for k in ('HTTPS_PROXY','NO_PROXY')}))"
    command = process._with_env(shlex.join([sys.executable, "-c", probe]))
    wrapper = Path(shlex.split(command)[1])
    result = subprocess.run(
        shlex.split(command),
        env={"HOME": str(tmp_path), "SHELL": "/bin/sh"},
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["HTTPS_PROXY"] == PROXY
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o600
    assert "private-password" not in command and "cursor-proxy" not in command
    assert "private-password" not in json.dumps(process.to_dict())
    assert "cursor-proxy" not in json.dumps(process.to_dict())


def test_claude_private_settings_receive_only_its_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = Settings(_env_file=None, claude_proxy_url=SecretStr(PROXY))
    monkeypatch.setattr(provider_proxy, "settings", config)
    process = TTYDProcess(
        tab_id="dotenv-claude", port=12345, name="dotenv", agent_type=AgentType.CLAUDE
    )
    path = process._write_launch_settings_file()
    assert path is not None
    assert json.loads(path.read_text())["env"]["HTTPS_PROXY"] == PROXY
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private-password" not in json.dumps(process.to_dict())


def test_remote_launch_does_not_forward_local_deployment() -> None:
    process = TTYDProcess(
        tab_id="dotenv-remote",
        port=12345,
        name="remote",
        agent_type=AgentType.CURSOR,
        target=ExecutionTarget.REMOTE,
    )
    assert "cursor-proxy" not in process._with_env("agent")


def test_scoped_process_secrets_are_not_in_ttyd_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_PROXY_URL", PROXY)
    assert "CURSOR_PROXY_URL" not in _agent_spawn_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://user:private-password@bad.example.test:99999",
        "socks5://user:private-password@bad.example.test",
        "http://user:private-password@bad.example.test/path",
    ],
)
def test_invalid_proxy_error_does_not_echo_secrets(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, cursor_proxy_url=SecretStr(url))
    assert "private-password" not in str(caught.value)
    assert "bad.example.test" not in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_catalog_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cursor_models_probe_timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_provider_stderr_is_private_before_runtime_diagnostics(tmp_path: Path) -> None:
    session = _session(AgentType.CURSOR)
    session.env = {}
    native = CursorNativeSession(session)
    native._cwd = str(tmp_path)
    script = "import os,sys; print(os.environ['HTTPS_PROXY'], file=sys.stderr); sys.exit(1)"
    try:
        await native._spawn_oneshot([sys.executable, "-c", script], "")
        assert native._reader_task is not None
        await asyncio.wait_for(native._reader_task, timeout=5)
        assert native.exit_error is not None
        assert "provider exited with code 1" in native.exit_error
        assert "private-password" not in native.exit_error
        assert "cursor-proxy" not in native.exit_error
    finally:
        await native.stop()


@pytest.mark.parametrize("key", ["message", "error", "detail", "stderr", "reason"])
def test_structured_error_diagnostics_hide_private_proxy(key: str) -> None:
    message = f"proxy failed: {PROXY}, host CURSOR-PROXY.EXAMPLE.TEST, auth private-password"
    redacted = _redact_value(key, message)
    assert "private-password" not in redacted
    assert "cursor-proxy" not in redacted.lower()
    assert "proxy failed" in redacted
    # Ordinary content keeps its existing redaction contract.
    assert _redact_value("text", "private-password") == "private-password"


@pytest.mark.parametrize("url", [PROXY, "direct"])
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/dash", "/bin/bash", "/bin/zsh"])
def test_deployment_proxy_survives_login_shell(
    url: str, shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(shell).is_file():
        pytest.skip("login shell is not installed on this platform")
    monkeypatch.setattr(
        provider_proxy, "settings", Settings(_env_file=None, cursor_proxy_url=SecretStr(url))
    )
    home = tmp_path / "login-home"
    home.mkdir()
    rc = "export HTTPS_PROXY=http://login.example.test:9090\n"
    (home / ".zprofile").write_text(rc)
    (home / ".bash_profile").write_text(rc)
    process = TTYDProcess(
        tab_id="login-proxy", port=12345, name="login", agent_type=AgentType.CURSOR
    )
    probe = "import os,json;print(json.dumps(os.environ.get('HTTPS_PROXY')))"
    command = process._with_env(shlex.join([sys.executable, "-c", probe]))
    result = subprocess.run(
        shlex.split(command),
        env={"HOME": str(home), "ZDOTDIR": str(home), "SHELL": shell},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == ("" if url == "direct" else PROXY)


@pytest.mark.asyncio
async def test_clear_claude_override_restores_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider_proxy, "settings", Settings(_env_file=None, claude_proxy_url=SecretStr(PROXY))
    )
    process = TTYDProcess(
        tab_id="clear-claude",
        port=12345,
        name="clear",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        env={"HTTPS_PROXY": "http://prior.example.test:8080"},
    )
    await process.switch_env({})
    path = process._write_launch_settings_file()
    assert path is not None
    assert json.loads(path.read_text())["env"]["HTTPS_PROXY"] == PROXY


def test_codex_status_error_redacts_proxy_components(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    config = Settings(
        _env_file=None,
        codex_proxy_url=SecretStr(PROXY),
        codex_no_proxy=SecretStr(".private-bypass.example.test"),
    )
    monkeypatch.setattr(provider_proxy, "settings", config)
    message = f"Reconnecting... 1/5: {PROXY} user user bypass PRIVATE-BYPASS.EXAMPLE.TEST"
    events = CodexJsonlAdapter().normalize_line(
        {"method": "error", "params": {"error": {"message": message}}}, _ctx()
    )
    assert events
    assert events[0].type == AgentStreamEventType.STATUS
    assert "text" in events[0].payload
    payload = json.dumps([redact_event(e).payload for e in events])
    for value in ("private-password", "cursor-proxy.example.test", "private-bypass.example.test"):
        assert value not in payload.lower()
    assert "[REDACTED]" in payload


@pytest.mark.parametrize("url", [None, PROXY, "direct"])
def test_session_home_profile_still_discovers_provider(
    url: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        provider_proxy,
        "settings",
        Settings(_env_file=None, cursor_proxy_url=SecretStr(url) if url else None),
    )
    home = tmp_path / "session-home"
    (home / "bin").mkdir(parents=True)
    (home / ".bash_profile").write_text(
        'export PATH="$HOME/bin:$PATH"\nexport HTTPS_PROXY=http://login.example.test:9090\n'
    )
    fake = home / "bin/ch-proxy-fake-provider"
    fake.write_text('#!/bin/sh\nprintf "%s" "$HTTPS_PROXY"\n')
    fake.chmod(0o700)
    process = TTYDProcess(
        tab_id="session-home-proxy",
        port=12345,
        name="home",
        agent_type=AgentType.CURSOR,
        env={"HOME": str(home), "SHELL": "/bin/bash"},
    )
    command = process._with_env("ch-proxy-fake-provider")
    result = subprocess.run(
        shlex.split(command),
        env={"HOME": str(tmp_path), "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout == (
        "http://login.example.test:9090" if url is None else "" if url == "direct" else PROXY
    )
