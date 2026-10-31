"""ProviderSpec registry: golden matrix, immutability, and equivalence.

The golden fixture (``tests/fixtures/provider_spec_matrix.json``) is the
reviewable artifact for the exact per-provider matrix; the equivalence tests
pin every migrated backend constant/list/admission outcome against a spec
derivation so a future edit that drifts one without the other fails CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.api import tabs as tabs_api
from claude_hub.api import terminal as terminal_api
from claude_hub.cli.commands import dot as cli_dot
from claude_hub.cli.commands import subagents as cli_subagents
from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.models.schemas import CHAT_REMOTE_UNSUPPORTED
from claude_hub.models.task_session import ManagedExecutorConfig
from claude_hub.services import task_session_executor as tse
from claude_hub.services.agent_stream import native, registry, transcript_fork
from claude_hub.services.agent_stream.native import (
    AntigravityNativeSession,
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    TraexNativeSession,
)
from claude_hub.services.agent_stream.providers import spec as pspec
from claude_hub.services.agent_stream.providers.spec import (
    ApprovalChannel,
    ImageTransport,
    ProviderSpec,
    Surface,
    TransportKind,
    all_specs,
    get_spec,
    providers_for_surface,
)
from claude_hub.services.dot import execution as dot_execution
from claude_hub.services.subagents import _EFFORT_ENV_KEYS, _MODEL_ENV_KEYS

_FIXTURE = Path(__file__).parent / "fixtures" / "provider_spec_matrix.json"
_ALL_AGENT_TYPES = list(AgentType)


def _canonical(spec: ProviderSpec) -> dict[str, Any]:
    data = spec.model_dump(mode="json")
    data["surfaces"] = sorted(data["surfaces"])
    data["goals"]["surfaces"] = sorted(data["goals"]["surfaces"])
    return data


# ── golden matrix ────────────────────────────────────────────────────────────


def test_golden_matrix_fixture_matches_registry_exactly() -> None:
    golden = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert set(golden) == {agent.value for agent in _ALL_AGENT_TYPES}
    for agent_type in _ALL_AGENT_TYPES:
        assert _canonical(get_spec(agent_type)) == golden[agent_type.value]


def test_golden_fixture_is_sorted_for_review() -> None:
    raw = _FIXTURE.read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
def test_spec_is_complete_for_every_wire_agent_type(agent_type: AgentType) -> None:
    spec = get_spec(agent_type)
    assert spec.id is agent_type
    assert spec.display_name
    assert isinstance(spec.surfaces, frozenset)
    assert len(spec.approval_channels) == len(list(Surface))


# ── immutability ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
def test_specs_are_deeply_immutable(agent_type: AgentType) -> None:
    spec = get_spec(agent_type)
    with pytest.raises(Exception):
        spec.binary = "mutated"  # type: ignore[misc]
    with pytest.raises(Exception):
        spec.goals.usage_quality = "exact"  # type: ignore[misc]
    with pytest.raises(Exception):
        spec.auth.kind = pspec.AuthKind.NONE  # type: ignore[misc]
    with pytest.raises(Exception):
        spec.resume.verified_flag = "--resume"  # type: ignore[misc]
    with pytest.raises(Exception):
        spec.approval_channels.direct_chat = ApprovalChannel.NONE  # type: ignore[misc]
    # Mapping-style mutation on the returned approval mapping raises TypeError
    # (review nit: frozen models otherwise only raise ValidationError).
    with pytest.raises(TypeError):
        spec.approval_channels[Surface.DIRECT_CHAT] = ApprovalChannel.NONE  # type: ignore[index]
    with pytest.raises(TypeError):
        proxy = spec.approval_channels.as_read_only()
        proxy[Surface.DIRECT_CHAT] = ApprovalChannel.NONE  # type: ignore[index]
    # The exposed approval mapping is the frozen model itself, not a dict.
    assert not hasattr(spec.approval_channels, "clear")
    assert isinstance(spec.surfaces, frozenset)


def test_registry_and_unknown_cache_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        all_specs()[AgentType.CLAUDE] = get_spec(AgentType.MASO)  # type: ignore
    assert not hasattr(all_specs(), "pop")
    # Specs stay hashable (all fields frozen/hashable).
    assert len({get_spec(agent) for agent in _ALL_AGENT_TYPES}) == len(_ALL_AGENT_TYPES)


def test_approval_channel_mapping_surface_access() -> None:
    codex = get_spec(AgentType.CODEX)
    assert codex.approval_channels[Surface.DIRECT_CHAT] is ApprovalChannel.RPC_BLOCKING
    assert codex.approval_channels.for_surface(Surface.TERMINAL_TAB) is ApprovalChannel.NATIVE_TUI
    assert dict(codex.approval_channels.items())[Surface.DIRECT_CHAT] == "rpc_blocking"
    with pytest.raises(TypeError):
        codex.approval_channels["direct_chat"]  # raw strings rejected on purpose
    assert codex.approval_channels.keys() == tuple(Surface)
    assert len(codex.approval_channels) == len(tuple(Surface))


# ── unknown / fail-closed ─────────────────────────────────────────────────────


def test_unknown_provider_fails_closed_like_missing_adapter() -> None:
    # AgentType is a closed enum, so the fail-closed path is defensive; it is
    # exercised directly via the fallback factory. terminal has an explicit
    # spec and must never resolve through the fallback.
    fallback = pspec._fail_closed_spec(AgentType.CLAUDE)
    assert fallback is not get_spec(AgentType.TERMINAL)
    assert fallback is not get_spec(AgentType.CLAUDE)
    assert fallback.transport is TransportKind.SHELL
    assert fallback.binary is None
    assert fallback.surfaces == frozenset()
    assert fallback.images is ImageTransport.NONE
    assert fallback.tool_timeline is False
    assert fallback.solo == "none"
    assert fallback.headless_safe is False
    assert all(fallback.approval_channels[surface] is ApprovalChannel.NONE for surface in Surface)
    assert native.native_provider_binary(AgentType.CLAUDE) is not None
    # The fallback itself behaves binary-less even for a registered id key.
    assert fallback.binary is None


def test_registry_covers_every_adapter_and_native_transport() -> None:
    assert set(native._NATIVE_SESSION_CLASSES) | {AgentType.MASO} == {
        agent for agent, spec in all_specs().items() if spec.transport is not TransportKind.SHELL
    }


# ── equivalence: migrated constants ───────────────────────────────────────────


def test_native_binary_map_equivalence() -> None:
    assert {agent: native.native_provider_binary(agent) for agent in _ALL_AGENT_TYPES} == {
        AgentType.CLAUDE: "claude",
        AgentType.CODEX: "codex",
        AgentType.TRAEX: "traex",
        AgentType.CURSOR: "agent",
        AgentType.ANTIGRAVITY: "agyx",
        AgentType.MASO: None,
        AgentType.TERMINAL: None,
    }


def test_native_binary_availability_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.shutil, "which", lambda name: f"/usr/bin/{name}")
    available = {
        agent: native.native_provider_binary_available(agent) for agent in _ALL_AGENT_TYPES
    }
    assert available == {
        AgentType.CLAUDE: True,
        AgentType.CODEX: True,
        AgentType.TRAEX: True,
        AgentType.CURSOR: True,
        AgentType.ANTIGRAVITY: True,
        AgentType.MASO: True,  # HTTP readiness proven by preflight, no binary
        AgentType.TERMINAL: False,
    }
    monkeypatch.setattr(native.shutil, "which", lambda name: None)
    assert native.native_provider_binary_available(AgentType.CLAUDE) is False
    assert native.native_provider_binary_available(AgentType.MASO) is True


def test_factory_keys_match_chat_transport_specs() -> None:
    assert set(native._NATIVE_SESSION_CLASSES) == {
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.TRAEX,
        AgentType.CURSOR,
        AgentType.ANTIGRAVITY,
    }
    session = _managed_session(AgentType.TERMINAL)
    with pytest.raises(ValueError, match="no native transport"):
        native.create_native_session(session)


def test_subagent_env_key_maps_equivalence() -> None:
    assert _MODEL_ENV_KEYS == {
        AgentType.CLAUDE: "ANTHROPIC_MODEL",
        AgentType.CODEX: "CODEX_MODEL",
        AgentType.TRAEX: "CODEX_MODEL",
        AgentType.CURSOR: "CURSOR_MODEL",
        AgentType.ANTIGRAVITY: "ANTIGRAVITY_MODEL",
        AgentType.MASO: "MASO_MODEL",
    }
    assert _EFFORT_ENV_KEYS == {
        AgentType.CODEX: "CODEX_REASONING_EFFORT",
        AgentType.TRAEX: "CODEX_REASONING_EFFORT",
    }


def test_dot_hot_env_keys_equivalence() -> None:
    assert tabs_api._DOT_MODEL_HOT_ENV_KEYS == {
        "claude": frozenset({"ANTHROPIC_MODEL"}),
        "codex": frozenset({"CODEX_MODEL", "CODEX_REASONING_EFFORT"}),
        "cursor": frozenset({"CURSOR_MODEL"}),
        "antigravity": frozenset({"ANTIGRAVITY_MODEL"}),
        "traex": frozenset({"CODEX_MODEL", "CODEX_REASONING_EFFORT", "TRAEX_REASONING_EFFORT"}),
        "maso": frozenset({"MASO_MODEL"}),
    }
    # Derived straight from the registry, including the traex legacy key order.
    for provider, spec in all_specs().items():
        expected = frozenset(key for key in ((spec.model_env_key,) + spec.effort_env_keys) if key)
        if expected:
            assert tabs_api._DOT_MODEL_HOT_ENV_KEYS[provider.value] == expected
        else:
            assert provider.value not in tabs_api._DOT_MODEL_HOT_ENV_KEYS


def test_dot_providers_equivalence_and_order() -> None:
    assert dot_execution.PROVIDERS == (
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.CURSOR,
        AgentType.ANTIGRAVITY,
        AgentType.TRAEX,
        AgentType.MASO,
    )
    assert dot_execution.PROVIDERS == providers_for_surface(Surface.DOT)


def test_cli_provider_lists_agree_with_surface_sets() -> None:
    # `agent run` exposes exactly the SUBAGENT surface set.
    assert set(cli_subagents.PROVIDERS) == {
        p.value for p in providers_for_surface(Surface.SUBAGENT)
    }
    # The Dot CLI list predates the MASO backend addition; it is a subset of
    # DOT until Slice 5 serves the list from the registry (documented gap).
    assert set(cli_dot.AGENT_TYPES) <= {p.value for p in providers_for_surface(Surface.DOT)}


def test_terminal_probe_filter_set_equivalence() -> None:
    assert terminal_api._PROBE_RESPONSE_FILTER_PROVIDERS == frozenset(
        {
            AgentType.CLAUDE,
            AgentType.CODEX,
            AgentType.TRAEX,
            AgentType.CURSOR,
            AgentType.ANTIGRAVITY,
        }
    )
    assert terminal_api._PROBE_RESPONSE_FILTER_PROVIDERS == frozenset(
        agent for agent, spec in all_specs().items() if spec.binary is not None
    )


def test_managed_executor_capabilities_equivalence() -> None:
    caps = tse.managed_executor_capabilities()
    assert caps.supported_agent_types == [
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.CURSOR,
        AgentType.ANTIGRAVITY,
        AgentType.MASO,
    ]
    assert list(caps.chat_supported_agent_types) == list(
        providers_for_surface(Surface.MANAGED_CHAT)
    )
    assert caps.model_configurable_agent_types == [AgentType.CLAUDE, AgentType.CODEX]
    # Union of managed surfaces, in registry order.
    assert tuple(caps.supported_agent_types) == tuple(
        dict.fromkeys(
            [
                *providers_for_surface(Surface.MANAGED_CHAT),
                *providers_for_surface(Surface.MANAGED_TERMINAL),
            ]
        )
    )


# ── admission truth table ─────────────────────────────────────────────────────


def _config(
    agent_type: AgentType,
    kind: SessionKind,
    target: ExecutionTarget = ExecutionTarget.LOCAL,
) -> ManagedExecutorConfig:
    return ManagedExecutorConfig(
        agent_type=agent_type,
        session_kind=kind,
        target=target,
        env={},
        model=None,
    )


@pytest.mark.parametrize(
    ("agent_type", "kind", "accepted"),
    [
        (AgentType.CLAUDE, SessionKind.CHAT, True),
        (AgentType.CODEX, SessionKind.CHAT, True),
        (AgentType.CURSOR, SessionKind.CHAT, True),
        (AgentType.MASO, SessionKind.CHAT, True),
        (AgentType.TRAEX, SessionKind.CHAT, False),
        (AgentType.ANTIGRAVITY, SessionKind.CHAT, True),
        (AgentType.TERMINAL, SessionKind.CHAT, False),
        (AgentType.CLAUDE, SessionKind.TERMINAL, True),
        (AgentType.CODEX, SessionKind.TERMINAL, True),
        (AgentType.CURSOR, SessionKind.TERMINAL, True),
        (AgentType.MASO, SessionKind.TERMINAL, False),
        (AgentType.TRAEX, SessionKind.TERMINAL, False),
        (AgentType.ANTIGRAVITY, SessionKind.TERMINAL, False),
        (AgentType.TERMINAL, SessionKind.TERMINAL, False),
    ],
)
def test_managed_admission_truth_table(
    agent_type: AgentType, kind: SessionKind, accepted: bool
) -> None:
    if accepted:
        tse.validate_config_admission(_config(agent_type, kind))
    else:
        with pytest.raises(ValueError):
            tse.validate_config_admission(_config(agent_type, kind))


def test_managed_admission_preserves_historical_messages() -> None:
    with pytest.raises(ValueError, match="Unsupported managed session agent_type: terminal"):
        tse.validate_config_admission(_config(AgentType.TERMINAL, SessionKind.CHAT))
    with pytest.raises(ValueError, match="Unsupported managed session agent_type: traex"):
        tse.validate_config_admission(_config(AgentType.TRAEX, SessionKind.TERMINAL))
    with pytest.raises(ValueError, match="select a Chat session"):
        tse.validate_config_admission(_config(AgentType.MASO, SessionKind.TERMINAL))
    with pytest.raises(ValueError, match="select a Chat session"):
        tse.validate_config_admission(_config(AgentType.ANTIGRAVITY, SessionKind.TERMINAL))
    with pytest.raises(ValueError, match=CHAT_REMOTE_UNSUPPORTED):
        tse.validate_config_admission(
            _config(AgentType.CLAUDE, SessionKind.CHAT, ExecutionTarget.REMOTE)
        )


def test_launch_env_model_override_only_claude_codex() -> None:
    for provider in (AgentType.CLAUDE, AgentType.CODEX):
        key = get_spec(provider).model_env_key
        assert key is not None
        env = tse.launch_env(ManagedExecutorConfig(agent_type=provider, model="m1", env={}))
        assert env[key] == "m1"
    # Providers admitted to managed surfaces but without a managed model key.
    for provider, kind in (
        (AgentType.CURSOR, SessionKind.TERMINAL),
        (AgentType.MASO, SessionKind.CHAT),
        (AgentType.ANTIGRAVITY, SessionKind.CHAT),
    ):
        with pytest.raises(ValueError, match="does not support an explicit model override"):
            tse.launch_env(
                ManagedExecutorConfig(agent_type=provider, session_kind=kind, model="m1", env={})
            )
    # Non-admitted providers fail at admission first.
    for provider in (AgentType.TRAEX, AgentType.TERMINAL):
        with pytest.raises(ValueError, match="Unsupported managed session"):
            tse.launch_env(ManagedExecutorConfig(agent_type=provider, model="m1", env={}))


# ── fork / edit-resend ────────────────────────────────────────────────────────


def test_fork_predicate_support_matches_spec() -> None:
    for provider in (AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR):
        assert callable(transcript_fork._user_message_predicate(provider))
    for provider in (AgentType.TRAEX, AgentType.MASO, AgentType.ANTIGRAVITY, AgentType.TERMINAL):
        with pytest.raises(transcript_fork.TranscriptForkError, match="edit-resend"):
            transcript_fork._user_message_predicate(provider)
    assert {agent for agent, spec in all_specs().items() if spec.edit_resend} == {
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.CURSOR,
    }


# ── native transport capability projection ────────────────────────────────────


def _bare_transport(cls: Any, agent_type: AgentType) -> Any:
    """Instantiate without SessionCore.__init__ (properties need only session)."""
    transport = cls.__new__(cls)
    object.__setattr__(transport, "session", SimpleNamespace(agent_type=agent_type))
    return transport


@pytest.mark.parametrize(
    ("cls", "agent_type"),
    [
        (ClaudeNativeSession, AgentType.CLAUDE),
        (CodexNativeSession, AgentType.CODEX),
        (TraexNativeSession, AgentType.TRAEX),
        (CursorNativeSession, AgentType.CURSOR),
        (AntigravityNativeSession, AgentType.ANTIGRAVITY),
    ],
)
def test_native_transport_flags_match_spec(cls: Any, agent_type: AgentType) -> None:
    transport = _bare_transport(cls, agent_type)
    spec = get_spec(agent_type)
    assert transport.supports_tool_timeline is spec.tool_timeline
    assert transport.supports_images is (spec.images is not ImageTransport.NONE)
    assert transport.supports_goals is (Surface.DIRECT_CHAT in spec.goals.surfaces)
    assert transport.supports_native_system_instructions is (
        spec.system_instruction_channel in ("cli_flag", "rpc_config")
    )
    assert transport.goal_usage_quality == spec.goals.usage_quality
    assert transport.goal_execution_owner == spec.goals.default_owner
    assert transport.goal_completion_requires_provider_event is (
        spec.goals.completion_requires_provider_event
    )
    assert transport.eof_is_fatal is spec.eof_is_fatal


def test_native_approval_projection_per_provider() -> None:
    assert _bare_transport(ClaudeNativeSession, AgentType.CLAUDE).supports_approval_ui is False
    assert _bare_transport(CodexNativeSession, AgentType.CODEX).supports_approval_ui is True
    assert _bare_transport(TraexNativeSession, AgentType.TRAEX).supports_approval_ui is True
    # Deliberate retained override: native cursor advertises approval UI even
    # though its direct-chat channel is the one-shot steer.
    cursor = _bare_transport(CursorNativeSession, AgentType.CURSOR)
    assert (
        get_spec(AgentType.CURSOR).approval_channels[Surface.DIRECT_CHAT]
        is ApprovalChannel.ONESHOT_STEER
    )
    assert cursor.supports_approval_ui is True


def test_maso_transport_flags_match_spec() -> None:
    from claude_hub.services.agent_stream.maso_http import MasoHttpSession

    transport = _bare_transport(MasoHttpSession, AgentType.MASO)
    spec = get_spec(AgentType.MASO)
    assert transport.supports_images is True
    assert transport.supports_tool_timeline is True
    assert transport.supports_approval_ui is True
    assert transport.supports_goals is True
    assert transport.goal_execution_owner == "hub_managed"
    assert transport.goal_usage_quality == "unavailable"
    assert transport.goal_completion_requires_provider_event is True
    assert transport.eof_is_fatal is spec.eof_is_fatal


def test_transcript_adapter_flags_match_spec() -> None:
    expected_approval = {
        AgentType.CLAUDE: True,
        AgentType.CODEX: True,
        AgentType.TRAEX: True,
        AgentType.CURSOR: True,
        AgentType.ANTIGRAVITY: True,
        AgentType.MASO: True,
    }
    for agent_type, approval in expected_approval.items():
        adapter_cls = registry.get_adapter(agent_type)
        assert adapter_cls is not None
        adapter = adapter_cls()
        assert adapter.supports_approval_ui is approval, agent_type
        assert adapter.supports_tool_timeline is True, agent_type

    claude_cls = registry.get_adapter(AgentType.CLAUDE)
    traex_cls = registry.get_adapter(AgentType.TRAEX)
    maso_cls = registry.get_adapter(AgentType.MASO)
    assert claude_cls is not None and traex_cls is not None and maso_cls is not None
    assert claude_cls().spec_agent_type is AgentType.CLAUDE
    assert traex_cls().spec_agent_type is AgentType.TRAEX
    assert maso_cls().supports_transcript_discovery is False
    assert claude_cls().supports_transcript_discovery is True


# ── headless_safe / solo derivation ───────────────────────────────────────────


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
def test_headless_safe_derived_from_solo(agent_type: AgentType) -> None:
    spec = get_spec(agent_type)
    assert spec.headless_safe is (spec.solo != "none")


def test_headless_safe_is_coerced_to_follow_solo() -> None:
    coerced = ProviderSpec(
        id=AgentType.CLAUDE,
        display_name="x",
        transport=TransportKind.SHELL,
        stream_source=pspec.StreamEventSource.TRANSCRIPT_JSONL,
        solo="none",
        headless_safe=True,
    )
    assert coerced.headless_safe is False
    coerced_unsafe = ProviderSpec(
        id=AgentType.CLAUDE,
        display_name="x",
        transport=TransportKind.CLI_ONESHOT,
        stream_source=pspec.StreamEventSource.STDOUT_STREAM_JSON,
        solo="always",
        headless_safe=False,
    )
    assert coerced_unsafe.headless_safe is True


# ── helpers ───────────────────────────────────────────────────────────────────


def _managed_session(agent_type: AgentType, tmp_path: Path | None = None) -> ManagedSession:
    return ManagedSession(
        id="spec-session",
        workspace_id="spec-workspace",
        tab_id="spec-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="spec test",
        workspace_path=str(tmp_path or Path("/tmp")),
        tmux_session="spec-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
