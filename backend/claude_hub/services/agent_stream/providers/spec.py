"""Static per-provider capability registry (ProviderSpec).

This module is the single declarative source for facts about a Chat provider
that do not change at runtime: transport kind, wire channels, launch/resume
shape, capability flags, model/effort env channels, and the set of Hub
surfaces that may launch the provider. Runtime-probed state (binary presence,
login, reachability, model catalog, quota) stays with the existing probes and
is *not* part of the spec.

Design packet (review-validated):
``docs/design/2026-10-27-provider-spec-capability-registry-proposal.md``.

The registry and every object returned by :func:`get_spec` are immutable; the
per-surface approval mapping is a frozen sub-model (``ConfigDict(frozen=True)``
alone does not deep-freeze a plain ``dict``). Unknown agent types receive a
capability-empty shell record, matching the historical
``get_adapter(...) -> None -> structured=False`` fail-closed behavior.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Tuple, cast

from pydantic import BaseModel, ConfigDict, model_validator

from ....models import AgentType


class TransportKind(str, Enum):
    CLI_ONESHOT = "cli_oneshot"  # claude --print / agent --print
    CLI_PERSISTENT = "cli_persistent"  # codex/traex app-server JSON-RPC
    HTTP_SSE = "http_sse"  # maso
    SHELL = "shell"  # terminal pseudo-provider and unknown fail-closed record


class ImageTransport(str, Enum):
    NONE = "none"
    SDK_BASE64 = "sdk_base64"  # claude SDKUserMessage blocks
    RPC_TEMP_FILE = "rpc_temp_file"  # codex/traex staged files over RPC
    PROMPT_FILE_REF = "prompt_file_ref"  # cursor sentinel path block + Read
    HTTP_CHUNKED = "http_chunked"  # maso 512 KiB chunks, 8 MiB cap


class ApprovalChannel(str, Enum):
    NONE = "none"
    NATIVE_TUI = "native_tui"  # in-terminal approvals
    ONESHOT_STEER = "oneshot_steer"  # answered as a later turn
    RPC_BLOCKING = "rpc_blocking"  # server->client JSON-RPC card
    HTTP_CONFIRM = "http_confirm"  # maso confirm endpoint


class PlanMode(str, Enum):
    NONE = "none"
    CLI_FLAG_PROBED = "cli_flag_probed"  # --permission-mode/--mode, help-probed
    RPC_COLLABORATION = "rpc_collaboration"  # collaborationMode/list


class ModelCatalogSource(str, Enum):
    STATIC_CURATED = "static_curated"  # native._STATIC_MODELS fallback
    TAB_ENV = "tab_env"  # claude: per-tab env wins, static fallback
    RPC_LIST = "rpc_list"  # codex model/list, static fallback
    CLI_LIST_MODELS = "cli_list_models"  # cursor agent --list-models
    HTTP_CATALOG = "http_catalog"  # maso /api/models


class ReasoningEffortKind(str, Enum):
    NONE = "none"
    ENV_KEY = "env_key"  # codex/traex
    MODEL_ID_SUFFIX = "model_id_suffix"  # cursor gpt-..-high
    MODEL_VARIANTS = "model_variants"  # maso per-model variant ids
    MODEL_SELECTION = "model_selection"  # claude: pick a model variant


class NativeGoals(str, Enum):
    NONE = "none"  # no provider API; Hub-managed or unsupported
    RPC_THREAD = "rpc_thread"  # codex/traex thread/goal/*


class AuthKind(str, Enum):
    NONE = "none"  # local service / ambient
    ENV_OR_SETTINGS = "env_or_settings"  # api key + base url in env/settings
    LOGIN_STATUS_PROBE = "login_status_probe"  # plus `codex login status`
    HTTP_SERVICE = "http_service"  # maso readiness endpoint


class QuotaSource(str, Enum):
    NONE = "none"
    LOCAL_ROLLOUT = "local_rollout"  # codex
    HTTP_ACCOUNTING = "http_accounting"  # maso


class Surface(str, Enum):
    DIRECT_CHAT = "direct_chat"
    SUBAGENT = "subagent"  # `agent run`
    MANAGED_CHAT = "managed_chat"  # workspace worker/reviewer Chat
    MANAGED_TERMINAL = "managed_terminal"  # workspace worker/reviewer TUI
    RESIDENT = "resident"
    SCHEDULED = "scheduled"
    DOT = "dot"
    TERMINAL_TAB = "terminal_tab"


class StreamEventSource(str, Enum):
    STDOUT_STREAM_JSON = "stdout_stream_json"
    RPC_NOTIFICATIONS = "rpc_notifications"
    SSE = "sse"
    TRANSCRIPT_JSONL = "transcript_jsonl"
    TRANSCRIPT_SNAPSHOT = "transcript_snapshot"


# Surface order mirrors the field order on ApprovalChannels.
_ALL_SURFACES: Tuple[Surface, ...] = (
    Surface.DIRECT_CHAT,
    Surface.SUBAGENT,
    Surface.MANAGED_CHAT,
    Surface.MANAGED_TERMINAL,
    Surface.RESIDENT,
    Surface.SCHEDULED,
    Surface.DOT,
    Surface.TERMINAL_TAB,
)


class ResumeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    # How the provider conversation is resumed after restart/new turn.
    kind: Literal["none", "verified_flag", "constructive_flag", "rpc_thread", "http_session"]
    verified_flag: Optional[str] = None  # e.g. "--resume"
    constructive_flag: Optional[str] = None  # e.g. "--session-id"
    constructive_always: bool = False  # cursor: --resume creates if absent


class GoalSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    api: NativeGoals = NativeGoals.NONE
    # Static advertisement; codex flips the owner per session after native
    # method-not-found negotiation (providers/codex.py:438-445).
    default_owner: Literal["provider_native", "hub_managed"] = "hub_managed"
    usage_quality: Literal["exact", "estimated", "unavailable"] = "unavailable"
    # Provider VERBATIM wire token that closes one Hub turn:
    # claude "result", codex/traex JSON-RPC "turn/completed",
    # cursor "turn_ended", maso SSE "agent_end".
    completion_event: Literal["result", "turn/completed", "turn_ended", "agent_end"] = "result"
    completion_event_scope: Literal["any", "root_only"] = "any"
    completion_requires_provider_event: bool = False
    # Goals are accepted only on direct top-level Chat tabs today
    # (api/goal_runs.py:104-113).
    surfaces: frozenset[Surface] = frozenset()


class AuthSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: AuthKind = AuthKind.NONE
    endpoint_env_names: Tuple[str, ...] = ()
    default_endpoint: Optional[str] = None
    login_probe_argv: Tuple[str, ...] = ()  # ("codex", "login", "status")
    requires_loopback_for_managed: bool = False
    network_route_selection: bool = False  # direct/proxy preflight + select


class ApprovalChannels(BaseModel):
    """Immutable per-surface approval-channel mapping.

    A frozen sub-model is used because ``ConfigDict(frozen=True)`` does not
    deep-freeze a plain dict. Every surface has a key; ``NONE`` means the
    provider runs on that surface without an approval channel (or does not run
    on it at all — admission additionally checks ``ProviderSpec.surfaces``).

    The object is a read-only mapping structurally (``channels[surface]``,
    iteration, ``len``); mapping-style mutation raises ``TypeError`` while
    attribute reassignment is blocked by the frozen config.
    """

    model_config = ConfigDict(frozen=True)

    direct_chat: ApprovalChannel = ApprovalChannel.NONE
    subagent: ApprovalChannel = ApprovalChannel.NONE
    managed_chat: ApprovalChannel = ApprovalChannel.NONE
    managed_terminal: ApprovalChannel = ApprovalChannel.NONE
    resident: ApprovalChannel = ApprovalChannel.NONE
    scheduled: ApprovalChannel = ApprovalChannel.NONE
    dot: ApprovalChannel = ApprovalChannel.NONE
    terminal_tab: ApprovalChannel = ApprovalChannel.NONE

    @classmethod
    def for_surfaces(cls, mapping: Mapping[Surface, ApprovalChannel]) -> "ApprovalChannels":
        """Build from a partial ``{Surface: channel}`` mapping; rest is NONE."""
        values: dict[str, ApprovalChannel] = {
            surface.value: ApprovalChannel.NONE for surface in _ALL_SURFACES
        }
        for surface, channel in mapping.items():
            if surface not in _ALL_SURFACES:
                raise ValueError(f"unknown approval surface: {surface!r}")
            values[surface.value] = channel
        return cls(**values)

    def for_surface(self, surface: Surface) -> ApprovalChannel:
        return cast(ApprovalChannel, getattr(self, surface.value))

    def __getitem__(self, key: object) -> ApprovalChannel:
        if not isinstance(key, Surface):
            raise TypeError(f"surface key must be a Surface, got {type(key)!r}")
        return cast(ApprovalChannel, getattr(self, key.value))

    def __len__(self) -> int:
        return len(_ALL_SURFACES)

    def __setitem__(self, key: object, value: object) -> None:
        raise TypeError("approval channel mapping is immutable")

    def __delitem__(self, key: object) -> None:
        raise TypeError("approval channel mapping is immutable")

    def keys(self) -> tuple[Surface, ...]:
        return tuple(_ALL_SURFACES)

    def values(self) -> tuple[ApprovalChannel, ...]:
        return tuple(cast(ApprovalChannel, getattr(self, s.value)) for s in _ALL_SURFACES)

    def items(self) -> tuple[tuple[Surface, ApprovalChannel], ...]:
        return tuple((s, self[s]) for s in _ALL_SURFACES)

    def get(self, key: object, default: ApprovalChannel = ApprovalChannel.NONE) -> ApprovalChannel:
        return self[key] if isinstance(key, Surface) else default

    def as_read_only(self) -> Mapping[Surface, ApprovalChannel]:
        """Return an immutable ``Mapping`` view keyed by :class:`Surface`."""
        return MappingProxyType({surface: self[surface] for surface in _ALL_SURFACES})


class ProviderSpec(BaseModel):
    """Immutable static facts for one provider (see module docstring)."""

    model_config = ConfigDict(frozen=True)

    id: AgentType
    display_name: str
    transport: TransportKind
    stream_source: StreamEventSource
    eof_is_fatal: bool = False

    # Launch / identity
    binary: Optional[str] = None
    resume: ResumeSpec = ResumeSpec(kind="none")
    supports_conversation_rotation: bool = False
    edit_resend: bool = False  # transcript_fork predicate support
    fork_seed_via_prompt: bool = True

    # Capabilities
    images: ImageTransport = ImageTransport.NONE
    tool_timeline: bool = False
    plan_mode: PlanMode = PlanMode.NONE
    system_instruction_channel: Literal[
        "none", "cli_flag", "rpc_config", "prompt_sentinel", "prompt_prefix"
    ] = "none"
    approval_channels: ApprovalChannels = ApprovalChannels()

    # Models / reasoning
    model_catalog: ModelCatalogSource = ModelCatalogSource.STATIC_CURATED
    model_env_key: Optional[str] = None
    model_configurable_in_managed: bool = False
    reasoning_effort: ReasoningEffortKind = ReasoningEffortKind.NONE
    effort_env_keys: Tuple[str, ...] = ()
    # Env keys a running tab may hot-update without restart (api/tabs.py).
    hot_env_keys: Tuple[str, ...] = ()

    # Goals / quota / auth
    goals: GoalSpec = GoalSpec()
    quota: QuotaSource = QuotaSource.NONE
    auth: AuthSpec = AuthSpec()

    # Solo/unattended execution. ``headless_safe`` is derivable: any value
    # other than "none" suppresses approval-blocking turns under solo.
    solo: Literal["none", "bypass_flag", "sandbox_policy", "always"] = "none"
    headless_safe: bool = False

    # Surfaces this provider may run on; remote Chat is supported by none.
    surfaces: frozenset[Surface] = frozenset()
    remote_terminal: bool = False

    @model_validator(mode="after")
    def _derive_headless_safe(self) -> "ProviderSpec":
        derived = self.solo != "none"
        if self.headless_safe != derived:
            object.__setattr__(self, "headless_safe", derived)
        return self

    def runs_on(self, surface: Surface) -> bool:
        return surface in self.surfaces


_CHAT_SURFACES = frozenset(_ALL_SURFACES)
_CLI_SURFACES = frozenset(
    {Surface.DIRECT_CHAT, Surface.SUBAGENT, Surface.DOT, Surface.TERMINAL_TAB}
)
_TUI_APPROVAL = {
    Surface.DIRECT_CHAT: ApprovalChannel.ONESHOT_STEER,
    Surface.SUBAGENT: ApprovalChannel.ONESHOT_STEER,
    Surface.MANAGED_CHAT: ApprovalChannel.ONESHOT_STEER,
    Surface.DOT: ApprovalChannel.ONESHOT_STEER,
    Surface.MANAGED_TERMINAL: ApprovalChannel.NATIVE_TUI,
    Surface.RESIDENT: ApprovalChannel.NATIVE_TUI,
    Surface.SCHEDULED: ApprovalChannel.NATIVE_TUI,
    Surface.TERMINAL_TAB: ApprovalChannel.NATIVE_TUI,
}
_RPC_APPROVAL = {
    Surface.DIRECT_CHAT: ApprovalChannel.RPC_BLOCKING,
    Surface.SUBAGENT: ApprovalChannel.RPC_BLOCKING,
    Surface.MANAGED_CHAT: ApprovalChannel.RPC_BLOCKING,
    Surface.MANAGED_TERMINAL: ApprovalChannel.NATIVE_TUI,
    Surface.RESIDENT: ApprovalChannel.NATIVE_TUI,
    Surface.SCHEDULED: ApprovalChannel.NATIVE_TUI,
    Surface.DOT: ApprovalChannel.RPC_BLOCKING,
    Surface.TERMINAL_TAB: ApprovalChannel.NATIVE_TUI,
}
_HTTP_APPROVAL = {
    Surface.DIRECT_CHAT: ApprovalChannel.HTTP_CONFIRM,
    Surface.SUBAGENT: ApprovalChannel.HTTP_CONFIRM,
    Surface.MANAGED_CHAT: ApprovalChannel.HTTP_CONFIRM,
    Surface.DOT: ApprovalChannel.HTTP_CONFIRM,
}
_TERMINAL_APPROVAL = {
    Surface.MANAGED_TERMINAL: ApprovalChannel.NATIVE_TUI,
    Surface.RESIDENT: ApprovalChannel.NATIVE_TUI,
    Surface.SCHEDULED: ApprovalChannel.NATIVE_TUI,
    Surface.TERMINAL_TAB: ApprovalChannel.NATIVE_TUI,
}

# Insertion order is the display ordering shared by the Dot backend list and
# managed-executor capability lists (claude, codex, cursor, ...); surfaces
# filter this order rather than re-sorting per site.
REGISTRY: Mapping[AgentType, ProviderSpec] = MappingProxyType(
    {
        AgentType.CLAUDE: ProviderSpec(
            id=AgentType.CLAUDE,
            display_name="Claude Code",
            transport=TransportKind.CLI_ONESHOT,
            stream_source=StreamEventSource.STDOUT_STREAM_JSON,
            binary="claude",
            resume=ResumeSpec(
                kind="verified_flag",
                verified_flag="--resume",
                constructive_flag="--session-id",
            ),
            supports_conversation_rotation=True,
            edit_resend=True,
            images=ImageTransport.SDK_BASE64,
            tool_timeline=True,
            plan_mode=PlanMode.CLI_FLAG_PROBED,
            system_instruction_channel="cli_flag",
            approval_channels=ApprovalChannels.for_surfaces(_TUI_APPROVAL),
            model_catalog=ModelCatalogSource.TAB_ENV,
            model_env_key="ANTHROPIC_MODEL",
            model_configurable_in_managed=True,
            reasoning_effort=ReasoningEffortKind.MODEL_SELECTION,
            hot_env_keys=("ANTHROPIC_MODEL",),
            goals=GoalSpec(
                api=NativeGoals.NONE,
                default_owner="hub_managed",
                usage_quality="exact",
                completion_event="result",
                completion_event_scope="any",
                surfaces=frozenset({Surface.DIRECT_CHAT}),
            ),
            quota=QuotaSource.NONE,
            auth=AuthSpec(
                kind=AuthKind.ENV_OR_SETTINGS,
                endpoint_env_names=("ANTHROPIC_BASE_URL",),
                network_route_selection=True,
            ),
            solo="bypass_flag",
            headless_safe=True,
            surfaces=_CHAT_SURFACES,
            remote_terminal=True,
        ),
        AgentType.CODEX: ProviderSpec(
            id=AgentType.CODEX,
            display_name="Codex",
            transport=TransportKind.CLI_PERSISTENT,
            stream_source=StreamEventSource.RPC_NOTIFICATIONS,
            eof_is_fatal=True,
            binary="codex",
            resume=ResumeSpec(kind="rpc_thread"),
            supports_conversation_rotation=True,
            edit_resend=True,
            images=ImageTransport.RPC_TEMP_FILE,
            tool_timeline=True,
            plan_mode=PlanMode.RPC_COLLABORATION,
            system_instruction_channel="rpc_config",
            approval_channels=ApprovalChannels.for_surfaces(_RPC_APPROVAL),
            model_catalog=ModelCatalogSource.RPC_LIST,
            model_env_key="CODEX_MODEL",
            model_configurable_in_managed=True,
            reasoning_effort=ReasoningEffortKind.ENV_KEY,
            effort_env_keys=("CODEX_REASONING_EFFORT",),
            hot_env_keys=("CODEX_MODEL", "CODEX_REASONING_EFFORT"),
            goals=GoalSpec(
                api=NativeGoals.RPC_THREAD,
                default_owner="hub_managed",
                usage_quality="exact",
                completion_event="turn/completed",
                completion_event_scope="any",
                surfaces=frozenset({Surface.DIRECT_CHAT}),
            ),
            quota=QuotaSource.LOCAL_ROLLOUT,
            auth=AuthSpec(
                kind=AuthKind.LOGIN_STATUS_PROBE,
                login_probe_argv=("codex", "login", "status"),
                network_route_selection=True,
            ),
            solo="sandbox_policy",
            headless_safe=True,
            surfaces=_CHAT_SURFACES,
            remote_terminal=True,
        ),
        AgentType.CURSOR: ProviderSpec(
            id=AgentType.CURSOR,
            display_name="Cursor",
            transport=TransportKind.CLI_ONESHOT,
            stream_source=StreamEventSource.STDOUT_STREAM_JSON,
            binary="agent",
            resume=ResumeSpec(
                kind="constructive_flag",
                constructive_flag="--resume",
                constructive_always=True,
            ),
            supports_conversation_rotation=True,
            edit_resend=True,
            images=ImageTransport.PROMPT_FILE_REF,
            tool_timeline=True,
            plan_mode=PlanMode.CLI_FLAG_PROBED,
            system_instruction_channel="prompt_sentinel",
            approval_channels=ApprovalChannels.for_surfaces(_TUI_APPROVAL),
            model_catalog=ModelCatalogSource.CLI_LIST_MODELS,
            model_env_key="CURSOR_MODEL",
            reasoning_effort=ReasoningEffortKind.MODEL_ID_SUFFIX,
            hot_env_keys=("CURSOR_MODEL",),
            goals=GoalSpec(
                api=NativeGoals.NONE,
                default_owner="hub_managed",
                usage_quality="unavailable",
                completion_event="turn_ended",
                completion_event_scope="any",
                surfaces=frozenset({Surface.DIRECT_CHAT}),
            ),
            quota=QuotaSource.NONE,
            auth=AuthSpec(
                kind=AuthKind.ENV_OR_SETTINGS,
                endpoint_env_names=("CURSOR_API_ENDPOINT",),
                default_endpoint="https://api2.cursor.sh",
                network_route_selection=True,
            ),
            solo="always",
            headless_safe=True,
            surfaces=_CHAT_SURFACES,
            remote_terminal=True,
        ),
        AgentType.TRAEX: ProviderSpec(
            id=AgentType.TRAEX,
            display_name="TraeX",
            transport=TransportKind.CLI_PERSISTENT,
            stream_source=StreamEventSource.RPC_NOTIFICATIONS,
            eof_is_fatal=True,
            binary="traex",
            resume=ResumeSpec(kind="rpc_thread"),
            supports_conversation_rotation=True,
            edit_resend=False,
            images=ImageTransport.RPC_TEMP_FILE,
            tool_timeline=True,
            plan_mode=PlanMode.RPC_COLLABORATION,
            system_instruction_channel="rpc_config",
            approval_channels=ApprovalChannels.for_surfaces(_RPC_APPROVAL),
            model_catalog=ModelCatalogSource.RPC_LIST,
            model_env_key="CODEX_MODEL",
            reasoning_effort=ReasoningEffortKind.ENV_KEY,
            # CODEX_REASONING_EFFORT is read first; TRAEX_REASONING_EFFORT is
            # the legacy fallback (providers/codex.py:646-648).
            effort_env_keys=("CODEX_REASONING_EFFORT", "TRAEX_REASONING_EFFORT"),
            hot_env_keys=(
                "CODEX_MODEL",
                "CODEX_REASONING_EFFORT",
                "TRAEX_REASONING_EFFORT",
            ),
            goals=GoalSpec(
                api=NativeGoals.RPC_THREAD,
                default_owner="hub_managed",
                usage_quality="exact",
                completion_event="turn/completed",
                completion_event_scope="any",
                surfaces=frozenset({Surface.DIRECT_CHAT}),
            ),
            quota=QuotaSource.NONE,
            auth=AuthSpec(
                kind=AuthKind.ENV_OR_SETTINGS,
                endpoint_env_names=("TRAE_API_BASE_URL",),
                network_route_selection=True,
            ),
            solo="sandbox_policy",
            headless_safe=True,
            surfaces=_CLI_SURFACES,
            remote_terminal=True,
        ),
        AgentType.MASO: ProviderSpec(
            id=AgentType.MASO,
            display_name="MASO",
            transport=TransportKind.HTTP_SSE,
            stream_source=StreamEventSource.SSE,
            # A broken SSE stream fails the turn in the transport; EOF itself
            # is not fatal the way a persistent app-server death is.
            eof_is_fatal=False,
            supports_conversation_rotation=True,
            edit_resend=False,
            images=ImageTransport.HTTP_CHUNKED,
            tool_timeline=True,
            plan_mode=PlanMode.NONE,
            system_instruction_channel="prompt_prefix",
            approval_channels=ApprovalChannels.for_surfaces(_HTTP_APPROVAL),
            model_catalog=ModelCatalogSource.HTTP_CATALOG,
            model_env_key="MASO_MODEL",
            reasoning_effort=ReasoningEffortKind.MODEL_VARIANTS,
            hot_env_keys=("MASO_MODEL",),
            goals=GoalSpec(
                api=NativeGoals.NONE,
                default_owner="hub_managed",
                usage_quality="unavailable",
                completion_event="agent_end",
                completion_event_scope="root_only",
                completion_requires_provider_event=True,
                surfaces=frozenset({Surface.DIRECT_CHAT}),
            ),
            quota=QuotaSource.HTTP_ACCOUNTING,
            auth=AuthSpec(
                kind=AuthKind.HTTP_SERVICE,
                endpoint_env_names=("MASO_BASE_URL", "MASO_PORT"),
                default_endpoint="http://127.0.0.1:9796",
                requires_loopback_for_managed=True,
                network_route_selection=False,
            ),
            # MASO sessions resume against the persisted session id fenced by
            # the verified endpoint (maso_http delivery/identity tombstone).
            resume=ResumeSpec(kind="http_session"),
            solo="none",
            headless_safe=False,
            surfaces=frozenset(
                {
                    Surface.DIRECT_CHAT,
                    Surface.SUBAGENT,
                    Surface.MANAGED_CHAT,
                    Surface.DOT,
                }
            ),
            remote_terminal=False,
        ),
        AgentType.TERMINAL: ProviderSpec(
            id=AgentType.TERMINAL,
            display_name="Terminal",
            transport=TransportKind.SHELL,
            stream_source=StreamEventSource.TRANSCRIPT_JSONL,
            fork_seed_via_prompt=False,
            goals=GoalSpec(surfaces=frozenset()),
            auth=AuthSpec(kind=AuthKind.NONE),
            approval_channels=ApprovalChannels.for_surfaces(_TERMINAL_APPROVAL),
            surfaces=frozenset({Surface.RESIDENT, Surface.SCHEDULED, Surface.TERMINAL_TAB}),
            remote_terminal=True,
        ),
    }
)

_FAIL_CLOSED: dict[AgentType, ProviderSpec] = {}


def _fail_closed_spec(agent_type: AgentType) -> ProviderSpec:
    spec = _FAIL_CLOSED.get(agent_type)
    if spec is None:
        spec = ProviderSpec(
            id=agent_type,
            display_name="",
            transport=TransportKind.SHELL,
            stream_source=StreamEventSource.TRANSCRIPT_JSONL,
            fork_seed_via_prompt=False,
            goals=GoalSpec(surfaces=frozenset()),
        )
        _FAIL_CLOSED[agent_type] = spec
    return spec


def get_spec(agent_type: AgentType) -> ProviderSpec:
    """Return the immutable spec for ``agent_type``.

    Unknown types get a permissive-named but capability-empty shell record
    (no binary, no surfaces, all channels NONE), preserving the historical
    fail-closed ``get_adapter(...) -> None -> structured=False`` behavior.
    ``terminal`` is an explicit registry entry, not this fallback.
    """

    spec = REGISTRY.get(agent_type)
    if spec is None:
        return _fail_closed_spec(agent_type)
    return spec


def is_registered_provider(agent_type: AgentType) -> bool:
    """Whether ``agent_type`` has an explicit (non-fallback) spec."""

    return agent_type in REGISTRY


def all_specs() -> Mapping[AgentType, ProviderSpec]:
    """Return the immutable registry in display/insertion order."""

    return REGISTRY


def providers_for_surface(surface: "str | Surface") -> Tuple[AgentType, ...]:
    """Return registered providers that may launch on ``surface``.

    Order follows registry insertion (the shared display ordering).
    """

    key = Surface(surface) if isinstance(surface, str) else surface
    return tuple(agent_type for agent_type, spec in REGISTRY.items() if key in spec.surfaces)
