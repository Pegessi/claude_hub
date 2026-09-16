# TraeX (`traex`) agent support — terminal TUI + structured Chat

Date: 2026-09-16 · Branch: `feat/traex-agent` (worktree `~/claude_hub_worktree/traex-agent`)

## Why / what

TraeX CLI is an internal **branded Codex fork**. Evidence: its `app-server`
`initialize` response reports `userAgent: "Codex Desktop/0.205.1"`, the binary
emits `codex_core` / `codex_rollout` log targets, and it ships the same
`app-server` / `acp` / `mcp-server` subcommands. Hub already had a first-class
Codex integration, so adding TraeX was mostly a wiring task — verified against
the live binary rather than assumed.

Two independent surfaces were added under one new agent type
`AgentType.TRAEX = "traex"`:

1. **Terminal TUI** — `traex` running inside the existing tmux/ttyd pane.
2. **Structured Chat** — the provider-neutral `StructuredPane`, driven by the
   Codex app-server JSON-RPC engine.

Out of scope this wave: TraeX as an **autonomous workspace worker**
(`task_session_executor` whitelist / goal-packet / transcript contract) and
terminal-side rollout resume/discovery.

## Local config gotcha (machine, not repo)

The installer migrated the legacy Coco config `~/.trae/traecli.yaml`
(`model.name: GLM-5.1`) into `~/.trae/traecli.toml` (`model = "GLM-5.1"`), but
the current `trae` provider catalog has no GLM model, so every TUI/thread start
failed with `-32603 model 'GLM-5.1' is unavailable because its metadata could
not be resolved`. Fixed by setting `model = "Seed-Evolving"` (the
`priority=0` default in `~/.trae/cli/models_cache.json`, also the model
returned by `thread/start`).

## Protocol verification (live, traex 0.205.1)

Hand-probed `traex app-server` over NDJSON, then drove the real Hub transport:

- `initialize`/`initialized` — same params (`clientInfo`,
  `capabilities.experimentalApi`); result adds `codexHome: ~/.trae`.
- `thread/start` → `result.thread.id`, `result.model: "Seed-Evolving"`,
  `modelProvider: "trae"`; emits `thread/started`.
- `collaborationMode/list` → Plan + Default.
- One real turn emitted, in order: `turn/started`, `item/reasoning/textDelta`,
  `item/agentMessage/delta`, `turn/completed` with
  `params.turn.status = "completed"` — identical names to Codex. Extra
  notifications (`hook/*`, `item/started|completed`, `thread/tokenUsage/updated`,
  `skills/changed`, `remoteControl/status/changed`) are silently ignored by the
  existing Codex adapter, as designed.

Two launch differences from upstream Codex:

| | Codex | TraeX |
| --- | --- | --- |
| app-server launch | `codex app-server --stdio` | **`traex app-server`** (rejects `--stdio`; `--listen stdio://` is the default) |
| config / rollout home | `~/.codex` | `~/.trae` (`~/.trae/cli/sessions/.../rollout-*.jsonl`) |

The home difference only affects on-disk rollout discovery/backfill, not live
Chat (which pins the app-server `threadId` and resumes via `thread/resume`).

## Design

Structured Chat reuses the Codex stack almost verbatim:

- `TraexNativeSession(CodexNativeSession)` — overrides only `_build_command()`
  → `["traex", "app-server"]` and `adapter_id = "traex-native"`. Everything
  else (NDJSON framing, three-way response/request/notification dispatch,
  initialize→thread lifecycle, `turn/start|cancel`, model injection via
  `collaborationMode.settings.model`, EOF fatal, image staging, question
  request/response) is inherited.
- Registry maps `TRAEX → CodexJsonlAdapter` (method-driven, protocol-neutral).
- Model override still rides `CODEX_MODEL` (the env key is just the carrier;
  the persistent app-server ignores it as an env var and receives the slug via
  the collaboration-mode channel). Frontend maps `traex → CODEX_MODEL`.
- Static model list keyed `"traex"` (20 slugs from `traex models`).

Terminal TUI mirrors the Codex/Cursor launch shape but is deliberately
**fresh-only** (no resume): a dedicated `_traex_launch_command()` emits
`traex` (+ solo bypass flags), launched through
`$SHELL -c '<cmd>; exec $SHELL'` in both `ensure_tmux_session` and
`_build_ttyd_command`. `agent_session_id` stays `None` for traex.

## File map

Backend:

- `models/schemas.py` — `AgentType.TRAEX`.
- `services/ttyd_manager.py` — `get_agent_command`; `_solo_command`;
  `_traex_launch_command`; `_agent_start_command`; traex branches in
  `ensure_tmux_session` and `_build_ttyd_command`.
- `services/agent_stream/native.py` — binary map, `"traex"` static models,
  `TraexNativeSession`, factory.
- `services/agent_stream/registry.py` — `TRAEX → CodexJsonlAdapter`.
- `api/terminal.py` — TUI probe-filter whitelist + injected `IS_AGENT_TUI`.
- `cli/commands/rest.py` — interactive tab `--agent-type` choice.
  (`tasks.py` worker choices intentionally left out: workers aren't supported.)
- `tests/test_traex_agent.py` — new.

Frontend:

- `types/index.ts` union; `AgentConfigFields.vue` option + solo hint;
  `AgentAvatar.vue` glyph/color; `TabBar.vue` solo support + label;
  `ScheduledTasksPanel.vue` option; `StructuredPane.vue` `MODEL_ENV_VAR`;
  `terminalSwitchPolicy.ts` + `TerminalView.vue` TUI/clipboard whitelist.

## Pitfalls

- **Three duplicated launch builders.** The terminal launch command is built
  independently in `ensure_tmux_session`, `_build_ttyd_command`, and
  `_tmux_shell_command`; a new agent type must be handled consistently or
  pre-creation (workspace prompt injection) and the ttyd lazy-attach diverge.
- **`--stdio` is rejected by traex** — use the bare `traex app-server`.
- **Running real-tmux tests inside a linked worktree appends an isolated
  socket** (`-L ch-<worktree-slug>`, here `ch-traex-agent`) via
  `runtime_isolation.tmux_socket_args`. That later `-L` overrides the
  per-run socket injected by `test_real_cold_restart_7tab_bijection`'s tmux
  wrapper and makes the bijection test fail (all tabs land on one server). It
  passes in the main checkout (slug → no `-L`) and in the worktree with
  `CLAUDE_HUB_TMUX_SOCKET=` + `CLAUDE_HUB_ALLOW_LIVE_RUNTIME=1`. Environmental,
  not a code regression.
- Local mypy/isort versions differ from the CI-locked toolchain (pre-existing
  test-only pydantic `call-arg` noise and an import-order nit in `native.py`
  present on main too); compare against a main baseline rather than chasing a
  zero count locally.

## Adversarial-review follow-up (same day)

A dedicated review pass (with live repro) found four real gaps; all fixed:

1. **M1 — Chat model switch 400.** `switch_env` rejected non
   claude/codex/cursor *before* its `SessionKind.CHAT` early-return, so the
   traex model picker always failed. Reordered: the CHAT path now admits
   `TRAEX` (env-only, no tmux respawn); the terminal respawn whitelist is
   unchanged (terminal traex stays fresh-only and raises).
2. **M2 — busy TUI read as idle.** The full-frame codex working-marker check in
   the runtime classifier was gated on `agent_type == CODEX`; traex paints the
   same chrome, so a busy traex tab reported idle (which also defeats the
   frontend's stable-screen replay gate). Now gated on `{CODEX, TRAEX}`, and
   `traex` joins the foreground-command idle set. (The workspace auto-continue
   busy check already ran the codex marker set unconditionally.)
3. **M3 — cross-provider transcript + edit-resend.** Reusing
   `CodexJsonlAdapter` verbatim let a *terminal* traex tab discover a `~/.codex`
   rollout and be force-promoted to structured, and let chat edit-resend reach
   `fork_transcript` (which raises for an unknown agent type). Introduced
   `TraexJsonlAdapter(CodexJsonlAdapter)` with
   `supports_transcript_discovery=False` and `discover_source() → None`; added
   that flag to the base adapter and gated the `_tab_capabilities_for` lazy
   promote on it (chat is unaffected — native transport never discovers files);
   hid edit-resend for traex in the UI.
4. **M4 — workspace-worker surface leak.** The shared `AgentConfigFields` is
   reused by the add-agent and resident forms, and the scheduled-task panel
   drives workspace sessions — all gained a Trae option from the shared change.
   Added an `exclude-types` prop (both workspace usages exclude `traex`),
   reverted the scheduled-panel option, and made `ensure_workspace_agent`
   reject `agent_type=traex` with a clear error. Standalone top-level tabs are
   unaffected.

Plus minor nits: `data-kind='traex'` status-chip colors in two panels, the
chat-provider validation message now names TraeX, and the test file grew from
10 to 18 cases (chat-kind never launches a TUI, `ensure_tmux_session` capture,
chat-only `switch_env`, working classifier). Test-module note: monkeypatch the
submodule via `importlib.import_module("claude_hub.services.ttyd_manager")` —
`from claude_hub.services import ttyd_manager` binds the manager *singleton*
exported by the package `__init__`, not the module.
