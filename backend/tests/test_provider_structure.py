"""Behavioral contracts for the provider implementation/compatibility boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    StreamModeOption,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import codex_jsonl, native, provider_network
from claude_hub.services.agent_stream.providers.network_types import ProbeResult
from claude_hub.services.agent_stream.providers.process import ProcessSession


@pytest.fixture
def session(tmp_path: Path) -> ManagedSession:
    return ManagedSession(
        id="structure-session",
        workspace_id="structure-workspace",
        tab_id="structure-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        title="structure test",
        workspace_path=str(tmp_path),
        tmux_session="structure-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class _UnconfiguredSession(ProcessSession):
    def _build_command(self) -> list[str]:
        return ["unused"]

    async def _send_text(self, text: str) -> None:
        raise AssertionError("this unconfigured class must never be instantiated")


def test_implementation_requires_an_explicit_runtime_owner(session) -> None:
    # Importing an implementation cannot silently install process-global hooks.
    with pytest.raises(TypeError, match="_runtime"):
        _UnconfiguredSession(session)  # type: ignore[abstract]


def test_runtime_hooks_are_readonly_and_resolve_facade_values(monkeypatch, session) -> None:
    transport = native.CodexNativeSession(session)
    with pytest.raises(AttributeError):
        setattr(transport, "_runtime", None)
    with pytest.raises(FrozenInstanceError):
        setattr(transport._runtime, "startup_grace_s", lambda: 999)

    replacement = StreamModeOption(id="patched", label="Patched", description="")
    monkeypatch.setattr(native, "_DEFAULT_MODE", replacement)
    monkeypatch.setattr(native, "_STARTUP_GRACE_S", 0.125)
    assert transport._runtime.default_mode() is replacement
    assert transport._runtime.startup_grace_s() == 0.125
    assert transport.available_modes() == [replacement]


def test_static_composition_keeps_one_session_state_owner(session) -> None:
    codex = native.CodexNativeSession(session)
    traex = native.TraexNativeSession(session)
    assert isinstance(traex, native.CodexNativeSession)
    assert isinstance(codex, native.ProviderSession)
    assert isinstance(traex, native.ProviderSession)
    assert native.CodexNativeSession.__module__ == native.__name__
    assert native.TraexNativeSession.__module__ == native.__name__
    assert codex._send_lock is not traex._send_lock
    assert codex._start_lock is not traex._start_lock
    assert codex._stdout_queue is not traex._stdout_queue
    assert codex._notification_queue is not traex._notification_queue
    assert codex._pending_requests is not traex._pending_requests
    assert codex._pending_questions is not traex._pending_questions


@pytest.mark.asyncio
async def test_network_default_probe_is_bound_per_instance_without_registration(
    monkeypatch,
) -> None:
    first_probe = AsyncMock(return_value=ProbeResult(True, "http", 200))
    second_probe = AsyncMock(return_value=ProbeResult(True, "http", 204))
    singleton = provider_network._default_selector
    monkeypatch.setattr(provider_network, "_probe_endpoint", first_probe)
    first = provider_network.ProviderNetworkSelector()
    monkeypatch.setattr(provider_network, "_probe_endpoint", second_probe)
    second = provider_network.ProviderNetworkSelector()
    env = {
        "ANTHROPIC_BASE_URL": "https://provider.example/anthropic",
        "CLAUDE_HUB_PROVIDER_NETWORK_MODE": "direct",
    }

    assert await first.select("claude", env) == env
    assert await second.select("claude", env) == env
    first_probe.assert_awaited_once_with(env["ANTHROPIC_BASE_URL"], None)
    second_probe.assert_awaited_once_with(env["ANTHROPIC_BASE_URL"], None)
    assert provider_network._default_selector is singleton
    assert first._cache is not second._cache
    assert first._lock is not second._lock
    assert provider_network.ProbeResult is ProbeResult


def test_codex_source_discovery_keeps_old_patch_targets(monkeypatch, session, tmp_path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.touch()
    candidates = MagicMock(
        return_value=[(session.created_at.timestamp(), "provider-id", str(path))]
    )
    pick = MagicMock(return_value="provider-id")
    monkeypatch.setattr(codex_jsonl, "_codex_candidates_for_cwd", candidates)
    monkeypatch.setattr(codex_jsonl, "_pick_backfill_session", pick)
    monkeypatch.setattr(codex_jsonl, "resolve_process_hint", lambda _session: (None, None))
    adapter = codex_jsonl.CodexJsonlAdapter()

    assert adapter.discover_source(session) == path
    candidates.assert_called_once_with(session.workspace_path)
    pick.assert_called_once()


def test_transcript_composition_keeps_public_identity_and_private_state() -> None:
    codex = codex_jsonl.CodexJsonlAdapter()
    traex = codex_jsonl.TraexJsonlAdapter()
    assert isinstance(traex, codex_jsonl.CodexJsonlAdapter)
    assert type(codex).__module__ == codex_jsonl.__name__
    assert type(traex).__module__ == codex_jsonl.__name__
    assert codex._spawned_threads is not traex._spawned_threads
    assert codex._messaged_threads is not traex._messaged_threads
    assert codex._nested_owner_by_parent is not traex._nested_owner_by_parent
    assert codex._inflight_reasoning is not traex._inflight_reasoning


def test_image_operations_keep_native_limits_and_directory_patches(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(native, "_MAX_IMAGE_BYTES", 8)
    assert native._detect_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert native._detect_image_mime(b"\x89PNG\r\n\x1a\nmore") is None
    monkeypatch.setattr(native, "_codex_image_temp_dir", lambda: tmp_path)
    retained = tmp_path / "subdirectory"
    retained.mkdir()
    image = tmp_path / "orphan.png"
    image.write_bytes(b"image")
    assert native.cleanup_codex_temp_dir() == 1
    assert not image.exists()
    assert retained.is_dir()
