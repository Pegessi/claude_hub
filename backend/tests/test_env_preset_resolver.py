"""Tests for env preset resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models import AgentType, EnsureWorkspaceAgentRequest, User, WorkspaceCreate
from claude_hub.services.env_preset_resolver import (
    EnvPresetNotFoundError,
    EnvPresetParseError,
    merge_env_with_preset,
    parse_env_text,
    resolve_env_preset,
    resolved_env_preset_keys,
)
from claude_hub.services.env_presets import EnvPresetManager
from claude_hub.services.workspace_manager import workspace_manager

SENTINEL = "SENTINEL_DAY1_LEAK_PROBE_XYZ789"


def _invalid_day1_preset_text(sentinel: str = SENTINEL) -> str:
    return "\n".join(
        [
            "export ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/coding",
            f"export 1BAD={sentinel}",
            "export ANTHROPIC_MODEL=doubao-seed-2.0-code",
        ]
    )


def _assert_no_sentinel(text: str, sentinel: str = SENTINEL) -> None:
    assert sentinel not in text


def _install_bad_day1_preset(tmp_path: Path, monkeypatch: MonkeyPatch) -> EnvPresetManager:
    import claude_hub.services.env_preset_resolver as resolver_module

    manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    manager.create_preset(
        name="day1-bad",
        text=_invalid_day1_preset_text(),
        preset_id="bad-day1",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", manager)
    return manager


def test_parse_env_text_ignores_comments() -> None:
    env = parse_env_text("# comment\nFOO=bar\n\nBAZ=1")
    assert env == {"FOO": "bar", "BAZ": "1"}


def test_parse_env_text_supports_export_and_quotes() -> None:
    env = parse_env_text(
        "\n".join(
            [
                "export ANTHROPIC_BASE_URL=https://example.test",
                'export ANTHROPIC_AUTH_TOKEN="secret token"',
                "CLAUDE_CODE_SUBAGENT_MODEL='model-x'",
            ]
        )
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://example.test"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "secret token"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "model-x"


def test_parse_env_text_supports_export_tab_and_day1_shape() -> None:
    env = parse_env_text(
        "\n".join(
            [
                "export\tANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/coding",
                'export ANTHROPIC_AUTH_TOKEN="sk-day1-token"',
                "export ANTHROPIC_MODEL=doubao-seed-2.0-code",
            ]
        )
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://ark.cn-beijing.volces.com/api/coding"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-day1-token"
    assert env["ANTHROPIC_MODEL"] == "doubao-seed-2.0-code"


def test_parse_env_text_invalid_line_does_not_leak_value() -> None:
    with pytest.raises(EnvPresetParseError, match="KEY=VALUE"):
        parse_env_text("NOT_A_VALID_LINE")


def test_parse_env_text_rejects_invalid_env_key_names() -> None:
    with pytest.raises(EnvPresetParseError, match="invalid environment variable name") as exc:
        parse_env_text("export BAD KEY=secret-value")
    assert exc.value.line_no == 1
    assert exc.value.key is None
    assert "secret-value" not in str(exc.value)

    with pytest.raises(EnvPresetParseError, match="invalid environment variable name") as exc:
        parse_env_text("export 1BAD=also-secret")
    assert exc.value.line_no == 1
    assert exc.value.key is None
    assert "also-secret" not in str(exc.value)


def test_resolve_env_preset_rejects_invalid_custom_preset_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    import claude_hub.services.env_preset_resolver as resolver_module

    manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    manager.create_preset(
        name="bad-keys",
        text="export BAD KEY=leak-me\nexport OK=1",
        preset_id="bad-keys",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", manager)

    with pytest.raises(EnvPresetParseError, match="invalid environment variable name") as exc:
        resolve_env_preset("bad-keys")
    assert "leak-me" not in str(exc.value)
    assert exc.value.key is None


def test_parse_env_text_preserves_hash_in_unquoted_values() -> None:
    """Match frontend parseLaunchEnv: unquoted # is part of the value."""
    env = parse_env_text("TOKEN=abc#def\nURL=https://example.com/path#frag")
    assert env["TOKEN"] == "abc#def"
    assert env["URL"] == "https://example.com/path#frag"


def test_parse_env_text_preserves_inline_hash_like_frontend() -> None:
    env = parse_env_text('export FOO=bar # not stripped\nBAZ="x # y"')
    assert env["FOO"] == "bar # not stripped"
    assert env["BAZ"] == "x # y"


def test_parse_env_text_unclosed_quote_preserved_like_frontend() -> None:
    env = parse_env_text('export TOKEN="unclosed secret')
    assert env["TOKEN"] == '"unclosed secret'


def test_resolve_builtin_preset_by_id() -> None:
    preset_id, env = resolve_env_preset("none")
    assert preset_id == "none"
    assert env == {}


def test_resolve_arbitrary_custom_preset_by_name_and_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import claude_hub.services.env_preset_resolver as resolver_module

    manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    manager.create_preset(
        name="GPU Debug",
        text="CUDA_LAUNCH_BLOCKING=1\nTRACE_LEVEL=verbose",
        preset_id="gpu-debug",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", manager)

    by_name = resolve_env_preset("gpu debug")
    by_id = resolve_env_preset("gpu-debug")
    assert (
        by_name
        == by_id
        == (
            "gpu-debug",
            {"CUDA_LAUNCH_BLOCKING": "1", "TRACE_LEVEL": "verbose"},
        )
    )


def test_resolve_unknown_preset_fail_closed_without_echoing_input() -> None:
    with pytest.raises(EnvPresetNotFoundError) as exc:
        resolve_env_preset("super-secret-preset-name")
    assert "super-secret-preset-name" not in str(exc.value)


def test_merge_explicit_env_overrides_preset(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import claude_hub.services.env_preset_resolver as resolver_module

    manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    manager.create_preset(
        name="day1",
        text='export FOO=from_preset\nBAR="quoted value"',
        preset_id="custom-day1",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", manager)

    merged = merge_env_with_preset(preset="day1", explicit_env={"FOO": "override"})
    assert merged["FOO"] == "override"
    assert merged["BAR"] == "quoted value"
    assert resolved_env_preset_keys(merged) == ["BAR", "FOO"]


def test_invalid_preset_secret_not_in_exc_cli_or_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _install_bad_day1_preset(tmp_path, monkeypatch)
    text = _invalid_day1_preset_text()

    with pytest.raises(EnvPresetParseError) as exc_info:
        parse_env_text(text)
    _assert_no_sentinel(str(exc_info.value))
    _assert_no_sentinel(repr(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert exc_info.value.line_no == 2
    assert exc_info.value.key is None

    with pytest.raises(EnvPresetParseError) as exc_info:
        resolve_env_preset("day1-bad")
    _assert_no_sentinel(str(exc_info.value))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        try:
            merge_env_with_preset(
                preset=body.get("env_preset"),
                explicit_env=body.get("env") or {},
            )
        except EnvPresetParseError as exc:
            return httpx.Response(400, json={"detail": str(exc)})
        return httpx.Response(200, json={"id": "s1"})

    def fake_get_client(ctx: Any) -> HubClient:
        transport = httpx.MockTransport(handler)
        return HubClient(base_url="http://testserver", transport=transport)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    runner = CliRunner()
    for args in (
        ["agent", "create", "ws1", "--env-preset", "day1-bad"],
        ["--json", "agent", "create", "ws1", "--env-preset", "day1-bad"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code != 0
        _assert_no_sentinel(result.output)
        for line in result.output.splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                _assert_no_sentinel(json.dumps(json.loads(stripped)))


@pytest.mark.asyncio
async def test_invalid_preset_secret_not_in_api_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _install_bad_day1_preset(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = workspace_manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="u1",
        name="tester",
        email="tester@example.com",
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/workspaces/{ws.id}/agent",
            json={
                "agent_type": AgentType.CLAUDE.value,
                "env_preset": "day1-bad",
            },
        )
        assert response.status_code == 400
        _assert_no_sentinel(response.text)
        _assert_no_sentinel(json.dumps(response.json()))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invalid_preset_secret_not_in_ensure_workspace_agent_value_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from claude_hub.services.workspace_manager import WorkspaceManager

    _install_bad_day1_preset(tmp_path, monkeypatch)
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    with pytest.raises(ValueError) as exc_info:
        await manager.ensure_workspace_agent(
            ws.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.CLAUDE,
                env_preset="day1-bad",
            ),
        )
    _assert_no_sentinel(str(exc_info.value))
    assert exc_info.value.__cause__ is None
