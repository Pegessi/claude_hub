# ProviderSpec: a capability registry and symmetric provider-approval model for `agent_stream`

Date: 2026-10-27 · Status: **Proposal (docs only)** · Branch: `codex/rv-provider-spec`
Base: `main` @ `0211661` · Scope: design, no product code changes

This packet proposes one registry of provider facts (`ProviderSpec`) for the
five Chat providers — **claude, codex, traex, cursor, maso** — plus the
pseudo-type **terminal**, and one approval/readiness model shared by every
surface that launches a provider (direct Chat, `agent run` subagents,
Workspace agents/reviewers, Residents, Scheduled Tasks, Dot, Goals).

All claims below cite the code at the base SHA. Section headings match the
requested packet contents.

---

## 1. Problem

### 1.1 Provider differences are real, numerous, and currently encoded ad hoc

The five providers differ on every transport axis Hub cares about. The
differences are expressed today as class attributes in two parallel class
hierarchies, string conditionals at call sites, hand-maintained literal sets,
and per-surface validation functions — with no single record describing a
provider.

**Two parallel capability hierarchies already exist and can disagree.**

- Native Chat transports subclass `SessionCore`, whose capability flags live at
  `backend/claude_hub/services/agent_stream/providers/session.py:32-45`
  (`supports_approval_ui`, `supports_tool_timeline`, `supports_images`,
  `supports_goals`, `goal_execution_owner`, `goal_usage_quality`,
  `supports_native_system_instructions`, `goal_completion_requires_provider_event`)
  and are projected into `StreamCapabilities` at
  `session.py:628-660`.
- Terminal-transcript observation adapters subclass `AgentStreamAdapter`
  (`backend/claude_hub/services/agent_stream/base.py:198-203`) with a
  *separately set* overlapping flag set, registered in
  `backend/claude_hub/services/agent_stream/registry.py:24-62`.
- The flags already diverge for the same provider: Claude's native Chat
  transport declares `supports_approval_ui = False`
  (`providers/claude.py:35-42`, inheriting the base default) while its terminal
  transcript adapter declares `supports_approval_ui = True`
  (`claude_jsonl.py:123-126`). This is correct per surface (a one-shot
  `--print` process cannot show an interactive approval TUI), but nothing in
  the code *says* that; it is two defaults in two files.
- Cursor's structured support is additionally gated imperatively by session
  state: `registry.py:55-62` fails closed unless `cursor_transport` is
  `acp` / `terminal_transcript` / `native`, and the native-vs-tailer transport
  choice is re-derived independently in
  `runtime/manager.py:96-100`, `api/agent_stream.py:336-342`,
  `services/restart_continuation.py:487-494`, and
  `runtime/edit_resend.py:75-76`.

**Provider-name conditionals are pervasive.** 172 non-import `AgentType.*`
references exist across backend non-test code; 29 are explicit
equality/set-membership branches. Representative branches and the capability
they actually encode:

| Branch site | Real capability |
| --- | --- |
| `providers/session.py:246` (`agent_type == CLAUDE` in `prepare_capabilities`) | model-catalog source |
| `native.py:201-218` (`native_provider_binary` map + MASO special case) | transport kind / binary presence |
| `native.py:427` (`if agent_type == "cursor"` in `model_catalog_for`) | runtime catalog probing |
| `native.py:585-597` (`create_native_session` if-chain) | transport factory |
| `providers/session.py:280-289` (`eof_is_fatal`, overridden at `codex.py:81-84`) | one-shot vs persistent lifecycle |
| `terminal_runtime/process/launch.py:47-64, 131-162, 314-338` | TUI launch argv, solo flags, resume argv |
| `terminal_runtime/process/state.py:147` (uuid pin set CLAUDE/CODEX/CURSOR) | conversation-id pinning |
| `terminal_runtime/process/environment.py:56, 260, 286, 304, 319` | settings-file carrier; model flag |
| `terminal_runtime/process/switch.py:36-45, 46, 108` | env hot-switch surface set |
| `terminal_runtime/local_io.py:166-170` | TUI binary name |
| `transcript_fork.py:120-126, 153-175` | edit-resend / fork support (claude, codex, cursor only) |
| `runtime/edit_resend.py:75` | MASO cannot edit past turns |
| `runtime/publication.py:112, 405` and `runtime/retirement.py:110` | MASO confirmation card lifecycle |
| `runtime/manager.py:97` | Cursor transcript-compat fallback |
| `api/terminal.py:274-280, 313-319` | probe-response filtering for four CLIs |
| `api/tabs.py:37-44` (`_DOT_MODEL_HOT_ENV_KEYS`) | per-provider hot-updatable env keys |
| `services/subagents.py:59-68` (`_MODEL_ENV_KEYS`, `_EFFORT_ENV_KEYS`) | model/effort env channels |
| `services/task_session_executor.py:18-33, 48-55, 68-71` | managed-surface admission; model override |
| `services/workspace_manager/sessions/provisioning.py:40-48, 216-218` | traex excluded as worker; cursor transcript provisioning |
| `services/terminal_runtime/manager/create.py:103-134` | managed Chat provider set; MASO admission/preflight/loopback |
| `services/dot/execution.py:18` (`PROVIDERS` tuple) | Dot execution backend set |
| `providers/network_types.py:32-44` | per-provider endpoints/env names |
| `providers/network_selector.py:136-145` | codex-only `codex login status` fallback |
| `services/provider_quota.py:280-479` (codex and maso readers only) | quota observation source |

### 1.2 The provider list is duplicated across backend, frontend, and CLI

Every surface that renders or validates a provider picker restates its own
list, and the lists are deliberately different — but the differences are
invisible (there is no record saying *why* traex is missing in one place and
maso in another):

- `backend/claude_hub/cli/commands/subagents.py:26` —
  `PROVIDERS = ["claude", "codex", "traex", "cursor", "maso"]`.
- `backend/claude_hub/cli/task_commands/creation.py:27, 149` —
  `["claude", "codex", "cursor", "maso", "terminal"]` (no traex).
- `backend/claude_hub/cli/commands/dot.py:21` —
  `AGENT_TYPES = ["codex", "claude", "cursor", "traex"]` (no maso; maso was
  later added to the Dot *backend* tuple, see below, but not to this CLI list).
- `backend/claude_hub/services/dot/execution.py:18` —
  `(CLAUDE, CODEX, CURSOR, TRAEX, MASO)` (five; differs from its own CLI).
- `backend/claude_hub/services/task_session_executor.py:161-176`
  (`managed_executor_capabilities`): managed agents advertise
  claude/codex/cursor/maso for both terminal kinds, only claude/codex model
  configurable.
- `backend/claude_hub/models/schema_domains/runtime.py:31-39` — the enum (six
  values) is the only exhaustive shared definition.
- Frontend: `frontend/src/types/taskExecution.ts:3` (union),
  `frontend/src/utils/dotApi.ts:4` (second union),
  `frontend/src/utils/subagentTool.ts:28` (third, 3-value subset union),
  `frontend/src/components/AgentConfigFields.vue:33-68` (option list with
  labels, plus `allowMaso`/`excludeTypes` props),
  `frontend/src/features/scheduled-tasks/ScheduledTasksPanel.vue:382-393`
  (independent four-option list), plus at least eight overlapping provider
  sets: `frontend/src/utils/managedChatSurface.ts:41-43`,
  `frontend/src/features/chat/StructuredPane.vue:94, 184`,
  `AgentConfigFields.vue:180`,
  `frontend/src/utils/terminalSwitchPolicy.ts:8-15`,
  `frontend/src/features/terminal/state/terminalIframeScript.ts:294`,
  `frontend/src/features/chat/timeline/reducer.ts:59`, and the workspace dialog
  exclusions at
  `frontend/src/features/workspace/components/WorkspaceAgentDialog.vue:190`
  and `ResidentAgentDialog.vue:156`.
- Per-provider metadata (display labels) is independently re-implemented in
  `AgentConfigFields.vue:33-68`, `SessionCreator.vue:535-551`,
  `DotExecutionEditor.vue:286-294`, `subagentTool.ts:202-206`,
  `quotaDisplay.ts:135-138`; glyphs and brand colors live in
  `frontend/src/components/AgentAvatar.vue:10-187` and are copied a second
  time into two CSS palettes (`agent-status-floating-panel.css:351-374`,
  `WorkspaceAgentStatus.vue:447-470`, which has no maso chip color).

### 1.3 Approval / readiness is asymmetric across surfaces

There is no single answer to "is this provider usable?". Each surface
implements a different gate against a different signal:

- **Binary presence**: `native_provider_binary_available()`
  (`native.py:212-218`) special-cases MASO (no binary); other providers pass if
  `shutil.which` finds the binary. `SessionCore.capabilities()`
  (`providers/session.py:636-643`) folds binary presence into `structured`.
- **Codex login**: an in-memory, TTL-cached `codex login status` subprocess
  probe (`providers/network_probe.py:17-50`, consumed at
  `providers/network_selector.py:136-175`, 300 s TTL in
  `network_types.py:65`) used only while resolving a network route. It is not
  surfaced as approval state and no other provider has an equivalent.
- **MASO readiness**: a dedicated HTTP preflight (`api/maso.py:30-49`,
  `services/maso_discovery.py`) surfaced to the UI through
  `POST /api/maso/preflight`
  (`frontend/src/composables/useMasoPreflight.ts:27-82`); Chat creation
  actively probes and persists the endpoint
  (`terminal_runtime/manager/create.py:118-134`), and managed MASO Chats
  additionally require loopback (`create.py:127-132`). This is the only
  provider whose Create button is disabled before submission.
- **Env preset/defaults**: CLI config defaults a preset only for claude
  (`cli/config.py:36-53`, `"day1"` fallback; mirrored in
  `useLaunchEnvPresets.ts:240-242`). There is no approval concept here, but
  credential presence is effectively checked only when the provider first
  runs.
- **Quota** is a separate passive panel for only codex and maso
  (`api/quota.py`, `services/provider_quota.py`, states `not_configured` vs
  `unavailable` at `models/schema_domains/quota.py:24-44`); it does not gate
  anything.
- **Surface admission by name**: direct Chat allows five providers;
  `agent run` allows five (`subagents.py:441-442` rejects only terminal, then
  rejects `effort` for all but codex/traex at `subagents.py:451-457`);
  managed executor admission is a name set
  (`task_session_executor.py:18-33`); workspace provisioning rejects traex with
  a prose message (`provisioning.py:40-48`); resident dialogs hide traex and
  maso in the frontend only; scheduled tasks list four; Goals gate not on name
  but on `supports_goals` (`useChatGoal.ts:123-125`) plus a direct-tab-only
  backend rule (`api/goal_runs.py:104-113`).

Concrete consequences of the asymmetry, each visible in code:

- A provider can be selectable in one surface and rejected at runtime in
  another. TraeX is the worked example: it appears in Dot and in `agent run`,
  but workspace admission rejects it server-side and two frontend dialogs hide
  it by `exclude-types` — three representations of the same fact.
- MASO is a valid `agent run --provider maso` choice and a valid direct Chat,
  but only the Chat/Dot UIs preflight it; a headless `agent run` discovers an
  absent service at turn time.
- Approval semantics differ even within Chat: codex/traex permission requests
  are blocking JSON-RPC answers (`providers/traex.py:157-258`); MASO uses HTTP
  confirm cards (`maso_http.py:700-745`); claude/cursor one-shot questions
  cannot block and are answered as a later steer
  (`docs/working-logs/2026-09-05-claude-ask-user-question-approval.md`). A
  headless subagent therefore hard-fails when a turn stops for approval
  (`cli/commands/subagents.py` RUN_HELP, "Approvals" section;
  `docs/working-logs/2026-10-24-agent-run-subagents.md`).

### 1.4 Evidence from recent incidents

- **Adding a provider means hunting branches.** The TraeX postmortem
  (`docs/working-logs/2026-09-16-traex.md`, "Adversarial-review follow-up"
  M1–M4) records: a `{claude, codex, cursor}` whitelist in `switch_env`
  rejected traex (M1, compare today's `switch.py:36-69`); a busy classifier
  gated `== CODEX` made traex read idle (M2); reusing `CodexJsonlAdapter` let
  terminal traex discover a `~/.codex` rollout (M3, fixed with
  `supports_transcript_discovery=False`); one shared form leaked traex into
  workspace surfaces (M4, compare today's `AgentConfigFields` exclude props).
  Launch argv was duplicated across three builders that had to change in
  lockstep (compare today's `launch.py:47-64, 131-162, 314-338`).
- **Cursor promotion logic existed at only one of three tab lifecycle
  boundaries** (`CHANGELOG.md:3483-3497`), failing Cursor Chat tabs closed with
  "no structured adapter"; the same fail-closed rule had to be re-applied at
  create, restore, and update sites — the shape of `registry.py:55-62`
  repeated elsewhere.
- **Assumed event shapes**: MASO 0.7.5 puts authoritative text in
  `post_llm_response` while `turn_end` is metadata-only; an adapter that
  assumed one shape dropped whole answers
  (`docs/working-logs/2026-10-27-maso-final-response.md`,
  `CHANGELOG.md:10-17`). TraeX re-emits null-turn records hours late, which
  minted immortal phantom turns
  (`docs/working-logs/2026-09-25-traex-turn-wedge-stop-recovery.md`).
- **Capability negotiation without names already proved necessary**: native
  Goal ownership must be probed — only an explicit method-not-found selects
  Hub fallback; timeout/auth/malformed must not silently downgrade
  (`docs/working-logs/2026-10-20-provider-native-goals.md`; implemented at
  `providers/codex.py:438-445`). Plan mode already follows a
  capability-not-name policy
  (`docs/working-logs/2026-09-02-paseo-activity-and-plan-mode-contract.md`,
  "Provider capability matrix"; frontend policy explicitly avoids provider
  names at `frontend/src/utils/chatModePolicy.ts:23-26`).
- **Preflight strictness itself was a bug**: a 3 s HEAD timeout against a real
  gateway rejected turns; inconclusive probes must not block
  (`docs/working-logs/2026-10-15-provider-preflight-resilience.md`).
- **Dead capability contract fields already exist**:
  `supports_approval_ui` and the capability-level goal fields are declared in
  `frontend/src/types/agentSessions.ts:103-109` but never read in `src/`
  (approval cards render from stream events; the UI reads goal owner from
  `ChatGoal.execution_owner`). An unowned capability vocabulary is itself a
  drift risk.
- **Minor latent duplication found during this research**: the traex
  reasoning-effort key is read in two spellings
  (`providers/codex.py:646-648` reads `CODEX_REASONING_EFFORT` then
  `TRAEX_REASONING_EFFORT`), and both must also appear in the hot-key map at
  `api/tabs.py:37-44`, in `services/subagents.py:67-68` (only the Codex
  spelling), and in `frontend/src/features/chat/presentation/chatModelPresentation.ts:13-17`
  — four sites per env key.

### 1.5 Design goal

Make the **static facts about a provider** a single declarative record; keep
**runtime state** (binary present, logged in, reachable, catalog loaded, quota
remaining) as a separately probed projection; make every **surface declare the
capabilities it needs** instead of naming providers; serve both to the frontend
from one endpoint.

---

## 2. Proposed `ProviderSpec` contract

### 2.1 Design constraints (derived from the code, not invented)

1. The spec holds **facts that do not change at runtime** — transport,
   channels, flags, surface membership. Facts that are probed stay probed
   (binary presence, login, endpoint reachability, `collaborationMode/list`,
   `model/list`, `agent --list-models`, MASO readiness), because the code
   already depends on TTLs and fail-closed discovery
   (`native.py:221-237, 415-437`; `network_types.py:53-65`;
   `maso_http.py:131-185`).
2. The same provider can present different capabilities per **surface**
   (Claude approval UI: terminal yes, one-shot Chat no) and per
   **cursor_transport**. The spec therefore carries per-surface overrides
   rather than one global boolean.
3. `StreamCapabilities` (the per-session, probed projection) stays as the wire
   contract for a *running* session; `ProviderSpec` is the per-provider
   *static* contract. They compose, they do not merge.
4. Unknown providers must get a safe default (today's fail-closed behavior:
   no adapter → `structured=False`, `registry.py:33-40, 52-54`).

### 2.2 Location and shape

One new module, the single registry:

```
backend/claude_hub/services/agent_stream/providers/spec.py
```

Next to the existing `contracts.py`/`session.py` so the transports own the
facts they implement. No frontend or CLI module ever declares a provider list
again (Section 3.4).

Pydantic v2 is used (the project already models wire contracts with pydantic,
e.g. `models/schema_domains/runtime.py:113-139`); frozen value objects keep the
registry immutable at runtime.

```python
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, Tuple
from pydantic import BaseModel, Field, ConfigDict

from ....models import AgentType


class TransportKind(str, Enum):
    CLI_ONESHOT = "cli_oneshot"          # claude --print / agent --print
    CLI_PERSISTENT = "cli_persistent"    # codex/traex app-server JSON-RPC
    HTTP_SSE = "http_sse"                # maso
    SHELL = "shell"                      # terminal pseudo-provider


class ImageTransport(str, Enum):
    NONE = "none"
    SDK_BASE64 = "sdk_base64"            # claude SDKUserMessage blocks
    RPC_TEMP_FILE = "rpc_temp_file"      # codex/traex staged files over RPC
    PROMPT_FILE_REF = "prompt_file_ref"  # cursor sentinel path block + Read
    HTTP_CHUNKED = "http_chunked"        # maso 512 KiB chunks, 8 MiB cap


class ApprovalChannel(str, Enum):
    NONE = "none"
    NATIVE_TUI = "native_tui"                  # in-terminal approvals
    ONESHOT_STEER = "oneshot_steer"           # answered as a later turn
    RPC_BLOCKING = "rpc_blocking"             # server->client JSON-RPC card
    HTTP_CONFIRM = "http_confirm"             # maso confirm endpoint


class PlanMode(str, Enum):
    NONE = "none"
    CLI_FLAG_PROBED = "cli_flag_probed"       # --permission-mode/--mode, help-probed
    RPC_COLLABORATION = "rpc_collaboration"   # collaborationMode/list


class ModelCatalogSource(str, Enum):
    STATIC_CURATED = "static_curated"   # native._STATIC_MODELS
    TAB_ENV = "tab_env"                 # claude: per-tab env wins
    RPC_LIST = "rpc_list"               # codex model/list
    CLI_LIST_MODELS = "cli_list_models" # cursor agent --list-models
    HTTP_CATALOG = "http_catalog"       # maso /api/models


class ReasoningEffortKind(str, Enum):
    NONE = "none"
    ENV_KEY = "env_key"                       # codex/traex
    MODEL_ID_SUFFIX = "model_id_suffix"       # cursor gpt-..-high
    MODEL_VARIANTS = "model_variants"         # maso per-model variant ids
    MODEL_SELECTION = "model_selection"       # claude: pick a model variant


class NativeGoals(str, Enum):
    NONE = "none"            # no provider API; Hub-managed or unsupported
    RPC_THREAD = "rpc_thread"  # codex/traex thread/goal/*
    # MASO has no goal API; its completion boundary differs, modeled separately


class AuthKind(str, Enum):
    NONE = "none"               # local service / ambient
    ENV_OR_SETTINGS = "env_or_settings"  # api key + base url in env/settings
    LOGIN_STATUS_PROBE = "login_status_probe"  # plus `codex login status`
    HTTP_SERVICE = "http_service"  # maso readiness endpoint


class QuotaSource(str, Enum):
    NONE = "none"
    LOCAL_ROLLOUT = "local_rollout"  # codex
    HTTP_ACCOUNTING = "http_accounting"  # maso


class Surface(str, Enum):
    DIRECT_CHAT = "direct_chat"
    SUBAGENT = "subagent"                 # `agent run`
    MANAGED_CHAT = "managed_chat"         # workspace worker/reviewer Chat
    MANAGED_TERMINAL = "managed_terminal" # workspace worker/reviewer TUI
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


class ResumeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    # How the provider conversation is resumed after restart/new turn.
    kind: Literal["none", "verified_flag", "constructive_flag",
                  "rpc_thread", "http_session"]
    verified_flag: Optional[str] = None      # e.g. "--resume"
    constructive_flag: Optional[str] = None  # e.g. "--session-id"
    constructive_always: bool = False        # cursor: --resume creates if absent


class GoalSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    api: NativeGoals = NativeGoals.NONE
    # What Hub advertises until/unless runtime negotiation says otherwise;
    # codex flips this per session (providers/codex.py:438-445).
    default_owner: Literal["provider_native", "hub_managed"] = "hub_managed"
    usage_quality: Literal["exact", "estimated", "unavailable"] = "unavailable"
    # Terminal event that closes one Hub turn on this transport.
    completion_event: Literal["result", "turn_completed",
                              "turn_ended", "root_agent_end"] = "turn_completed"
    completion_requires_provider_event: bool = False
    # Goals are accepted only on direct top-level Chat tabs today
    # (api/goal_runs.py:104-113); keep that a declared fact.
    surfaces: Tuple[Surface, ...] = (Surface.DIRECT_CHAT,)


class AuthSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: AuthKind = AuthKind.NONE
    endpoint_env_names: Tuple[str, ...] = ()
    default_endpoint: Optional[str] = None
    login_probe_argv: Tuple[str, ...] = ()  # ("codex", "login", "status")
    requires_loopback_for_managed: bool = False
    network_route_selection: bool = False   # direct/proxy preflight + select


class ProviderSpec(BaseModel):
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
    edit_resend: bool = False          # transcript_fork.py predicate support
    fork_seed_via_prompt: bool = True  # all four CLI transports today

    # Capabilities
    images: ImageTransport = ImageTransport.NONE
    tool_timeline: bool = False
    plan_mode: PlanMode = PlanMode.NONE
    system_instruction_channel: Literal[
        "none", "cli_flag", "rpc_config", "prompt_sentinel"
    ] = "none"
    # Approval capability per surface (Section 1.1 divergence).
    approval_channels: dict[Surface, ApprovalChannel] = Field(
        default_factory=dict
    )

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

    # Solo/unattended execution
    solo: Literal["none", "bypass_flag", "sandbox_policy", "always"] = "none"
    # Whether an approval-blocking turn can be answered headlessly.
    headless_safe: bool = False

    # Surfaces this provider may run on; remote Chat is supported by none.
    surfaces: frozenset[Surface] = frozenset()
    remote_terminal: bool = False
```

### 2.3 The registry (exact matrix at the base SHA)

`REGISTRY: dict[AgentType, ProviderSpec]` in the same module, populated from
the values verified in Section 1. Terse matrix (full field values in code):

| Dimension | claude | codex | traex | cursor | maso | terminal |
| --- | --- | --- | --- | --- | --- | --- |
| transport | cli_oneshot | cli_persistent | cli_persistent | cli_oneshot | http_sse | shell |
| stream source | stdout stream-json | RPC notifications | RPC notifications | stdout stream-json (+ transcript snapshot for `terminal_transcript`) | SSE | tmux |
| binary | `claude` | `codex` | `traex` | `agent` | — (`native.py:215-216`) | shell |
| eof fatal | no (`session.py:289`) | yes (`codex.py:81-84`) | yes | no | broken SSE fails turn (`maso_http.py:1-8`) | — |
| resume | `--session-id` constructive → `--resume` verified (`claude.py:236-245`) | `thread/resume`, provider id | inherits codex | `--resume` always constructive (`cursor.py:93-100`) | persisted `session_id` + endpoint fence (`maso_http.py:199-236`) | none |
| rotation | yes (`claude.py:233-234`) | yes (`codex.py:319`) | yes | yes (`cursor.py:90-91`) | yes (`maso_http.py:379`) | no |
| edit-resend | yes | yes | **no** | yes | **no** (`runtime/edit_resend.py:75`) | no |
| images | sdk_base64 (`claude.py:247-286`) | rpc_temp_file | inherits codex | prompt_file_ref (`cursor.py:23-37, 161-220`) | http_chunked (`CHANGELOG.md:814-822`) | no |
| tool timeline | yes | yes | yes | yes | yes | no |
| plan mode | cli_flag_probed (`claude.py:228-231`) | rpc_collaboration (`codex.py:504-530`) | rpc (kebab sandbox) | cli_flag_probed (`cursor.py:85-88`) | none (default only, `manager/create.py:123-125`) | no |
| system instructions | cli_flag (`--append-system-prompt`) | rpc_config | rpc_config | prompt_sentinel (`cursor.py:104-109`) | prompt prefix | none |
| approval: terminal tab | native_tui (`claude_jsonl.py:125`) | native_tui | native_tui | native_tui (`cursor_cli_transcript.py:118`) | — | native |
| approval: direct Chat | oneshot_steer (flag False, `claude.py:35-42`) | rpc_blocking (`codex.py:76`) | rpc_blocking (`traex.py:157-211`) | oneshot_steer | http_confirm (`maso_http.py:700-745`) | — |
| goals API | none | rpc_thread, negotiated owner (`codex.py:438-445`) | rpc_thread (inherited) | none | none (hub_managed) | none |
| goal usage quality | `exact` (`claude.py:42`) | `exact` (`codex.py:79`) | `exact` (inherited) | `unavailable` | `unavailable` | unavailable |
| turn terminal event | `result` | `turn/completed` | `turn/completed` | `turn_ended` | root `agent_end` only (`maso_http.py:195-197`) | — |
| model catalog | tab_env + static fallback (`session.py:246-254`) | rpc_list + static fallback (`codex.py:532-560`) | static_curated (`native.py:282-300`) | cli_list_models (`native.py:427-437`) | http_catalog (`maso_http.py:131-160`) | — |
| model env key | `ANTHROPIC_MODEL` | `CODEX_MODEL` | `CODEX_MODEL` | `CURSOR_MODEL` | `MASO_MODEL` (`subagents.py:59-65`) | — |
| reasoning effort | model_selection | env_key `CODEX_REASONING_EFFORT` | env_key (+ legacy `TRAEX_REASONING_EFFORT`, `codex.py:646-648`) | model_id_suffix (`providers/model_options.py:61-166`) | model_variants (`maso_http.py:82-124`) | — |
| solo | bypass_flag (`claude.py:212-215`) | sandbox_policy (`codex.py:447-497`) | sandbox_policy | always (`agent --yolo`, no-op toggle) | none | — |
| auth | env_or_settings, Bedrock/Vertex/Foundry rejected (`network_types.py:25-29`) | login_status_probe + endpoint inference (`network_selector.py:136-145`) | env (`TRAE_API_BASE_URL`) | env default endpoint `api2.cursor.sh` (`network_types.py:32-44`) | http_service, managed requires loopback | — |
| network route selection | yes | yes | yes | yes | no (`trust_env=False`, `maso_http.py:182-184`) | — |
| quota | none | local_rollout | none | none | http_accounting | — |
| direct Chat | yes | yes | yes | yes | chat-only | — |
| subagent (`agent run`) | yes | yes | yes | yes | yes (CLI list); effort rejected for all but codex/traex (`subagents.py:451-457`) | no |
| managed Chat | yes | yes | **no** | yes | yes, loopback only | no |
| managed terminal | yes | yes | **no** (`provisioning.py:40-48`) | yes | no (`task_session_executor.py:20-21`) | no (also rejected `:18`) |
| resident | yes | yes | **no** (hidden UI) | yes | no (hidden UI) | yes |
| scheduled | yes | yes | no (absent list) | yes | no | yes |
| dot | yes | yes | yes | yes | yes + preflight (`dot/execution.py:18`) | no |
| terminal tab (TUI) | yes | yes | yes | yes | no | yes |
| remote target | TUI only | TUI only | TUI only | TUI only | no | yes |

`get_spec(agent_type)` returns the record or a permissive-named but
**capability-empty** record for unknown types: `transport=SHELL`, empty surface
set, all features `none` — identical in effect to today's
`get_adapter(...) → None → structured=False` fail-closed path
(`registry.py:33-40`). `terminal` is an explicit spec, not a fallback.

### 2.4 Representative consumer rewrites

**A. Managed-surface admission** — `services/task_session_executor.py:15-33`

Before:

```python
def validate_config_admission(config: ManagedExecutorConfig) -> None:
    if config.agent_type in {AgentType.TERMINAL, AgentType.TRAEX}:
        raise ValueError(f"Unsupported managed session agent_type: ...")
    if config.agent_type == AgentType.MASO and config.session_kind != SessionKind.CHAT:
        raise ValueError("MASO is an HTTP Chat provider; select a Chat session")
    if config.session_kind == SessionKind.CHAT:
        if config.agent_type not in {
            AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR, AgentType.MASO,
        }:
            raise ValueError("Managed Chat supports only local Claude, Codex, Cursor, or MASO agents")
        if config.target == ExecutionTarget.REMOTE:
            raise ValueError(CHAT_REMOTE_UNSUPPORTED)
```

After:

```python
def validate_config_admission(config: ManagedExecutorConfig) -> None:
    spec = get_spec(config.agent_type)
    surface = (Surface.MANAGED_CHAT if config.session_kind == SessionKind.CHAT
               else Surface.MANAGED_TERMINAL)
    if surface not in spec.surfaces:
        raise ProviderSurfaceError(config.agent_type, surface)  # one message table
    if surface is Surface.MANAGED_CHAT and config.target == ExecutionTarget.REMOTE:
        raise ValueError(CHAT_REMOTE_UNSUPPORTED)
```

**B. Binary/factory** — `native.py:201-218, 585-597`

Before: a dict literal for binaries, a MASO special case for availability, and
a five-branch factory. After:

```python
def native_provider_binary(agent_type: AgentType) -> Optional[str]:
    return get_spec(agent_type).binary

def native_provider_binary_available(agent_type: AgentType) -> bool:
    spec = get_spec(agent_type)
    if spec.transport in (TransportKind.HTTP_SSE, TransportKind.SHELL):
        return True  # readiness is proven by the auth/readiness probe
    return spec.binary is not None and shutil.which(spec.binary) is not None

# Factory becomes registry-declared transport -> implementation class, with
# MASO no longer imported lazily inside an if-chain; unknown transport raises.
```

**C. Frontend surface gate** — `frontend/src/utils/managedChatSurface.ts:35-44`

Before:

```ts
if (sessionKind === 'chat') {
  return target === 'local' && ['claude', 'codex', 'cursor', 'maso'].includes(agentType)
}
return agentType !== 'maso'
```

After (consuming the Section 4 endpoint; types generated/served from backend):

```ts
const spec = providerSpecs.value.get(agentType)
const surface = sessionKind === 'chat' ? 'managed_chat' : 'managed_terminal'
return spec?.surfaces.includes(surface) ?? false
// 'local' is itself declared (no provider offers remote managed Chat).
```

The same replacement removes the lists at `StructuredPane.vue:94, 184`,
`AgentConfigFields.vue:180`, `terminalSwitchPolicy.ts:8-15`,
`terminalIframeScript.ts:294`, `reducer.ts:59`, and the scheduled-tasks
dropdown, replacing each with a capability test
(`capabilities.edit_resend`, `spec.solo !== 'none'`,
`surface in spec.surfaces`, `spec.transport === 'cli_persistent'`).

---

## 3. Provider-approval symmetry

### 3.1 One state machine, one vocabulary

A provider's usability is the combination of **static** spec facts and
**live** readiness. Introduce one per-provider status (read-only, derived —
nothing is "approved" by writing a flag):

```
unknown
  └─(spec loaded)→ not_configured        # binary absent / no endpoint / quota says not_configured
                    │ probing (bounded, cached)
                    ├─ ready             # every requirement for the intended surface passes
                    ├─ needs_auth        # binary present; login probe fails (codex) / 401-403
                    ├─ unreachable       # endpoint probe failed (non-timeout)
                    └─ inconclusive      # timeout: never blocks (preflight-resilience rule)
```

Rules, all already established by incident history:

- Status is computed **per (provider, env, surface)** — the same provider may
  be `ready` for direct Chat (env preset works) but `not_configured` as a
  managed worker (loopback requirement). Cache keys must include env/source
  identity (`docs/working-logs/2026-10-20-chat-capabilities-cache.md`; current
  selector key at `network_config.py` fingerprint functions).
- **Inconclusive never blocks** and inherits the caller environment
  (`docs/working-logs/2026-10-15-provider-preflight-resilience.md`; current TTL
  split in `network_types.py:53-56`).
- **Downgrade requires explicit evidence**: native-goal negotiation stays the
  model — only explicit method-not-found flips ownership
  (`docs/working-logs/2026-10-20-provider-native-goals.md`).
- Probes are bounded and side-effect free: existing constraints — HEAD probes
  with timeouts (`network_probe.py:64-100`), read-only MASO discovery with no
  port scanning (`services/maso_discovery.py:1-10`), login probes never echo
  account data (`network_probe.py:43-50`) — become the spec-declared probe
  contract.
- Runtime capability discovery (modes, models, goal owner) stays a
  *session-level* projection (`StreamCapabilities`) and is distinct from
  surface-level readiness.

### 3.2 Requirements per surface, not names

Each surface declares a `SurfaceRequirements` record; admission =
`spec.satisfies(surface)` (static) **and**
`readiness(provider, env).allows(surface)` (live):

| Surface | Static requirements | Live gate |
| --- | --- | --- |
| direct Chat | `DIRECT_CHAT` in surfaces; transport != shell | binary or HTTP readiness; mode supported (`session.py:640-643`) |
| subagent | `SUBAGENT` in surfaces; `headless_safe` for solo runs (else hard-fail rule documented up front, as today) | same as Chat |
| managed Chat | `MANAGED_CHAT`; local target; loopback when `auth.requires_loopback_for_managed` | full readiness at creation (today's MASO probe, `create.py:118-134`) |
| managed terminal | `MANAGED_TERMINAL`; `remote_terminal` when remote | binary present |
| resident | `RESIDENT` | binary present |
| scheduled | `SCHEDULED` | at run time |
| dot | `DOT` | maso preflight when maso (today's editor gate) |
| Goals | `goals.surfaces` contains surface; non-plan; direct tab (rules unchanged) | provider-native goal negotiation or hub-managed path |

This replaces the five+ name lists in Section 1.2 with one predicate and makes
"why is traex missing here" answerable: its spec omits `MANAGED_CHAT`,
`MANAGED_TERMINAL`, `RESIDENT`, and `SCHEDULED` (the postmortem M4 boundary,
`provisioning.py:40-48`), while including `DIRECT_CHAT`, `SUBAGENT`, `DOT`, and
`TERMINAL_TAB`.

### 3.3 Migration of existing flags and config

- Class attributes on `SessionCore`/`AgentStreamAdapter`
  (`supports_images`, `supports_tool_timeline`, …) become *runtime projections
  sourced from the spec*: transports keep their override hook, but the default
  comes from `get_spec(session.agent_type)`, so the two hierarchies can no
  longer silently disagree; a surface-keyed accessor
  (`spec.approval_channels[surface]`) replaces the divergent
  `supports_approval_ui` defaults.
- `managed_executor_capabilities()` (`task_session_executor.py:153-177`) and
  dot `PROVIDERS` (`dot/execution.py:18`) are generated by filtering the
  registry, not hand-listed.
- Env-key maps (`subagents.py:59-68`, `api/tabs.py:37-44`,
  `chatModelPresentation.ts:5-17`) come from `spec.model_env_key`,
  `spec.effort_env_keys`, `spec.hot_env_keys`.
- Config/env vars are unchanged (Section 4).

### 3.4 UI/CLI impacts (listed, not implemented in this packet)

- New `GET /api/providers` returning, per provider: static spec + current
  readiness for the requesting surface + display metadata
  (name/glyph hint). Optionally `POST /api/providers/{id}/preflight`
  (bounded) generalizing `/api/maso/preflight`; the maso route is retained as
  an alias.
- `AgentConfigFields` renders options from the endpoint filtered by a
  `surface` prop; `excludeTypes`/`allowMaso` are deleted. Same for scheduled
  tasks, resident/workspace dialogs, and the Dot editor (which already renders
  a backend list — `DotExecutionEditor.vue:54-60`).
- Labels/glyphs/colors move to backend-served metadata consumed by
  `AgentAvatar.vue` (with a built-in fallback glyph so offline UI still
  renders).
- CLI click `Choice(...)` lists move to a validated fetch with the current
  lists as offline fallback; the server stays the authority (it already
  rejects bad names — `subagents.py:441-442`).
- Per-surface readiness drives enable/disable + reason strings uniformly,
  replacing the MASO-only preflight panel
  (`useMasoPreflight.ts`) with a shared composable.
- Approval-blocked turns in headless subagents report the spec-declared
  reason (`headless_safe=False`) instead of provider-specific prose.

---

## 4. Compatibility

- **Wire enum unchanged**: `AgentType` values (`runtime.py:31-39`) and all env
  var names (`ANTHROPIC_MODEL`, `CODEX_MODEL`, `CODEX_REASONING_EFFORT`,
  `CURSOR_MODEL`, `MASO_MODEL`, `TRAE_API_BASE_URL`, `ANTHROPIC_BASE_URL`,
  `CURSOR_API_ENDPOINT`, `MASO_BASE_URL`, `MASO_PORT`,
  `HUB_CHAT_ALLOW_NETWORK`, `CLAUDE_HUB_PROVIDER_NETWORK_MODE`, …) keep their
  exact semantics and precedence (`maso_http.py:37-79`,
  `cli/config.py:36-53`).
- **`StreamCapabilities` response unchanged** initially; the registry is a
  backend-internal source for its defaults. New fields are additive and only
  arrive later with `/api/providers`.
- **Unknown providers fail closed exactly as today**: empty capability record,
  no structured adapter (`registry.py:52-54`).
- **MasoS**: its HTTP/SSE differences are first-class dimensions
  (`HTTP_SSE`, `HTTP_CHUNKED`, `HTTP_CONFIRM`, `HTTP_CATALOG`,
  `http_service` auth, `root_agent_end` completion, `trust_env=False`); its
  delivery-tombstone and endpoint-fence behavior
  (`maso_http.py:199-236, 774-779`) stays in the transport and is additionally
  declared as "endpoint identity immutable per session" so switch-env UI need
  not special-case it (`switch.py:46-55`).
- **Cursor transports** (`terminal` / `acp` / `terminal_transcript` /
  `native`; `registry.py:55-62`, `runtime.py:45-55`) modeled as a
  session-scoped transport variant over one spec, not as providers.
- **Frontend rollout strategy**: backend-served capability endpoint is the
  single source (Section 3.4); duplicated constants are deleted per surface,
  never all at once. Until a surface migrates, both sources coexist and a
  contract test asserts they agree. `AgentType` remains a TS string union
  (wire-stable); subset unions (`dotApi.ts`, `subagentTool.ts`) derive via
  `Pick`/capability filters.
- **Quota endpoint** (`/api/quota`) unchanged; its `not_configured` /
  `unavailable` vocabulary feeds the readiness model rather than being
  replaced.
- **No persisted "approved" flag** is introduced in the initial slices (see
  open decision 7.2); readiness remains derived, matching today's behavior.

---

## 5. Incremental slice plan

Each slice is independently shippable, behind existing behavior, with rollback
by revert. Ordering matters; later slices depend on earlier ones but nothing
within a slice requires the next.

**Slice 1 — Registry introduced read-only (no behavior change).**
Add `providers/spec.py` with the Section 2 matrix and `get_spec`. No production
call site reads it. Add the golden-matrix test (Section 6) and *equivalence
assertions* that derive, in tests, existing literal sets/maps from the spec and
compare them to the current constants byte-for-byte (surface sets for each
surface, binary map, env-key maps, resume flags, hot-key map). Rollback: delete
the module.

**Slice 2 — Non-admission backend consumers migrate.**
Replace the low-risk, pure-data sites one PR at a time:
`native_provider_binary`/availability (Section 2.4 B), the subagent env-key
maps (`subagents.py:59-68`), `_DOT_MODEL_HOT_ENV_KEYS` (`api/tabs.py:37-44`),
`managed_executor_capabilities`/dot `PROVIDERS`, then admission predicates
(`task_session_executor.py:15-33`, `provisioning.py:40-48`,
`create.py:103-134`) behind a helper returning the same errors. Existing
behavioral tests pin outputs; rollback per PR.

**Slice 3 — Transport/launch consumers migrate.**
`create_native_session`, `eof_is_fatal`, launch builders
(`launch.py:47-64, 131-162, 314-338`), resume argv
(`state.py:147`, `claude.py:236-245`, `cursor.py:93-100`), fork predicate set
(`transcript_fork.py:120-126`), terminal probe-filter set
(`api/terminal.py:274-280`), cursor-transport gating
(`registry.py:55-62` + the three re-derivations). The two capability
hierarchies take defaults from the spec while retaining overrides; a test
asserts advertised `StreamCapabilities` are unchanged per provider/surface
(record before/after snapshots).

**Slice 4 — `/api/providers` and readiness unification backend-side.**
Add the endpoint (static spec + derived readiness from existing probes:
binary/which, codex login TTL cache, network probes, maso preflight, quota
states). Generalize readiness behind one service; keep `/api/maso/preflight`
and `/api/quota` as-is. Slice ships with no frontend consumers; contract test
locks the response.

**Slice 5 — Frontend migrates surface by surface.**
Order: launch dialogs (`AgentConfigFields` + workspace/resident exclusions) →
composer gates (solo, attachments already capability-driven, goals already
capability-driven) → StructuredPane sets → scheduled tasks → Dot →
labels/avatars → CLI lists with fallback. Each change deletes one local list
and is covered by a component test using a mocked `/api/providers`.

**Slice 6 — Approval symmetry cleanup.**
Per-surface requirement records gate creation uniformly; the MASO-only
preflight panel becomes generic; headless-subagent approval failure reasons
come from `headless_safe`; dead capability fields
(`supports_approval_ui` in the frontend type) are either wired to the
surface-keyed spec value or removed deliberately.

What stays throughout: pydantic wire models, env precedence, transport
implementations themselves, ttyd/tmux, and every probe's bounds and TTLs.

---

## 6. Validation per slice

- **Slice 1 — proof of no behavior change**:
  - Golden matrix test: parameterized over all six `AgentType`s × every
    `ProviderSpec` field, asserting the exact Section 2.3 table (a fixture
    file is the reviewable artifact).
  - Equivalence tests: for each existing constant
    (`subagents._MODEL_ENV_KEYS`, `api/tabs._DOT_MODEL_HOT_ENV_KEYS`,
    `native.native_provider_binary` map, dot `PROVIDERS`,
    `managed_executor_capabilities` lists, launch solo sets,
    terminal probe set, fork predicate set), assert
    `{derived from spec} == {literal in code}`. Any future edit that changes a
    list without editing the spec (or vice versa) fails CI.
  - Full existing suite (`./scripts/verify.sh all`) must pass unchanged.
- **Slice 2/3 — consumer equivalence**:
  - Per-consumer before/after characterization tests using recorded sessions
    (existing fakes/fixtures under `backend/tests/`, e.g.
    `test_managed_chat_admission.py`, `test_cli_subagents.py`,
    `test_traex_agent.py`, `test_agent_stream_native.py`,
    `test_cursor_cli_transcript_agent_stream.py`, `test_maso_http.py`).
  - `StreamCapabilities` snapshot test: for each provider × surface
    (direct Chat, managed Chat, terminal tab) with probes stubbed, caps JSON
    before/after is identical — including the claude approval flag split.
  - Property test: for every provider/surface pair,
    `spec.satisfies(surface)` equals today's admission outcome (rejected or
    accepted), deriving "today" from the migrated call sites pre-change.
- **Slice 4 — contract tests**: `/api/providers` schema test; readiness state
  transitions with stubbed probes incl. timeout→`inconclusive` (never blocks),
  401/403→`needs_auth`, maso-down→`unavailable`, managed non-loopback→
  requirement failed; auth required on the route like other APIs.
- **Slice 5 — frontend tests**: each migrated surface gets a test with mocked
  provider payloads asserting visible options/gabels equal the old constants
  (a shared fixture served by a TS copy of the golden matrix generated from
  backend to prevent re-drift); `frontend-tests` + `frontend-types`; no
  browser gate needed beyond existing Chat flows because behavior is
  option-visibility only, with browser gates on the launch dialogs per
  `docs/UI_ACCEPTANCE.md`.
- **Every slice**: `./scripts/verify.sh check-staged` and the target subset
  (`docs`, `backend-types`, `backend-tests`, then `frontend-*`); revert-based
  rollback demonstrated by pure-additive first slice.

---

## 7. Risks and open decisions for the user

1. **Strict vs permissive default for unknown providers.** Proposal:
   capability-empty/fail-closed (matches `registry.py` today). Alternative: a
   shell-only default. This determines whether a future seventh provider
   appears nowhere until registered (strict) or runs as raw terminal
   immediately (permissive). **Recommendation: strict.**
2. **Static spec vs runtime-probed capabilities.** The proposal draws a hard
   line (transport/image/channels static; modes/models/goal-owner/readiness
   probed). Decision needed: should endpoint-dependent facts that *can* vary by
   deployment (e.g. a gateway exposing different models) ever be cached as
   static? Current code says no for claude models (`claude.py` per-tab env
   catalog) — proposal keeps that.
3. **Capability granularity.** A wide flat spec (proposed, ~30 fields) vs
   nested grouped records vs generated-from-code introspection. Flat is
   greppable and testable; nested risks dead sub-fields like the already-dead
   frontend `supports_approval_ui`. Open to trimming fields with only one
   consumer.
4. **Readiness storage.** Derived/ephemeral with TTLs (proposed; matches
   today's caches) vs a persisted per-provider approval record. Persisting
   would allow "approved provider" allowlisting across restarts but introduces
   stale-credential risk and a new state owner.
5. **Scope of `/api/providers`**: static + on-demand readiness (proposed) vs
   also streaming readiness changes via SSE. SSE adds a second push channel;
   quota already polls.
6. **CLI behavior when the Hub is unreachable**: keep hardcoded fallback
   choices (proposed; server still validates) vs hide unknown providers.
7. **Feature-flag need.** Slice 1–3 are pure refactors behind identical
   behavior and need none; Slice 4–5 alter UI data sources and may warrant a
   settings flag for one release given the number of launch surfaces.
8. **`supports_approval_ui` semantics**: consolidate into per-surface
   `approval_channels` (proposed; requires callers to name the surface) vs keep
   the boolean on `StreamCapabilities` and derive it per session.
9. **TraeX's `TRAEX_REASONING_EFFORT` legacy key**: fold into
   `effort_env_keys` ordering with documented precedence, or deprecate on a
   schedule (not established by this research; left as-is in the proposal).

---

## 8. Non-goals

- Changing any provider's wire protocol, launch argv, env precedence, resume
  semantics, transcript format, or probe timeouts/TTLs.
- Moving provider implementations (claude/codex/traex/cursor/maso) onto a
  shared transport base class; the registry describes them, it does not
  merge them.
- New provider onboarding or adding/seventh providers (network_probe is a
  probe utility, not a Chat provider, and is intentionally not given a spec).
- Persisted credential storage, secret management, or a replacement for env
  presets; quota enforcement/gating; billing.
- Rewriting the Goal state machine — only its provider-capability inputs are
  regularized.
- Changing ttyd/tmux terminal hosting or remote-SSH mechanics.
- Removing `/api/maso/preflight`, `/api/quota`, or any existing response field.
- Frontend visual redesign of avatars/pickers — only their data source moves.

---

## Appendix: key evidence index

- Capability flags: `providers/session.py:32-45, 628-660`;
  `agent_stream/base.py:198-203`; `providers/claude.py:35-42`;
  `providers/codex.py:73-84, 438-445`; `providers/traex.py:12-24`;
  `providers/cursor.py:30-37`; `maso_http.py:188-197, 306-324`.
- Transports: `providers/process.py`; `providers/codex_rpc.py`;
  `agent_stream/native.py:201-218, 563-597`;
  `registry.py:24-62`; `maso_http.py:1-8`.
- Lists/maps: `cli/commands/subagents.py:26`;
  `cli/task_commands/creation.py:27, 149`; `cli/commands/dot.py:21`;
  `services/dot/execution.py:18`;
  `task_session_executor.py:15-33, 153-177`;
  `services/subagents.py:59-68, 441-457`; `api/tabs.py:37-44`;
  frontend sites in Sections 1.2/3.4.
- Readiness: `providers/network_probe.py:17-50, 64-100`;
  `providers/network_selector.py:136-175`;
  `providers/network_types.py:32-65`; `api/maso.py:30-49`;
  `services/maso_discovery.py`; `terminal_runtime/manager/create.py:118-134`;
  `api/quota.py`; `models/schema_domains/quota.py:24-51`.
- Incident history:
  `docs/working-logs/2026-09-16-traex-agent.md` (M1–M4);
  `2026-09-25-traex-turn-wedge-stop-recovery.md`;
  `2026-10-15-provider-preflight-resilience.md`;
  `2026-10-20-provider-native-goals.md`;
  `2026-10-24-agent-run-subagents.md`;
  `2026-10-27-maso-final-response.md`;
  `2026-09-02-paseo-activity-and-plan-mode-contract.md`;
  `CHANGELOG.md:10-17, 19-49, 3483-3497`.
