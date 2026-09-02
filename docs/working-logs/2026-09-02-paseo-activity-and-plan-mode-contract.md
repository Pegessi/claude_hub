# Paseo activity and Plan-mode contract and validation

## Status

This note records both the upstream source audit and the implemented Claude Hub
contract. The feature worktree passed the central automated checks and the
bounded local runtime checks listed under [Validation](#validation). Items that
were not exercised remain unchecked; passing the broad suites is not treated as
evidence for an unobserved live lifecycle edge.

The upstream baseline is `getpaseo/paseo` `main` at commit
[`78f7796eb44223c7fa252d3c537263e3a0b34875`](https://github.com/getpaseo/paseo/tree/78f7796eb44223c7fa252d3c537263e3a0b34875).
All source paths below refer to that commit unless stated otherwise.

## Scope and terminology

- A **Chat** is Claude Hub's structured, provider-native conversation surface
  (`session_kind=chat`). A **Terminal** is the raw tmux/ttyd surface
  (`session_kind=terminal`). Legacy `session_kind=agent` is accepted only while
  loading old top-level tab state and is normalized to Chat; Workspace managed
  session state always normalizes to Terminal. `agent` is not a public creation
  option.
- Chat creation is available only from the top-level Terminal area. Agent
  Workspace orchestrator/reviewer/worker sessions remain Terminal control-plane
  runners and never mount `StructuredPane`.
- **Plan** means a provider execution mode or feature that restricts the next
  turn to planning. It is unrelated to provider subscription-plan usage.
- **Desired mode** is what the user selected. **Effective mode** is what the
  provider has accepted for a turn. UI must not present a failed mode change as
  effective.

## What Paseo actually does

### Tab activity is an attention state, not connectivity

Paseo derives one state bucket with this priority:

1. `needs_input`: there is a pending permission or the attention reason is
   `permission`.
2. `failed`: lifecycle is `error` or the attention reason is `error`.
3. `running`: the agent lifecycle is running.
4. `attention`: a completed/other attention edge remains unread.
5. `done`: none of the above.

The derivation lives in
[`packages/protocol/src/agent-state-bucket.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/protocol/src/agent-state-bucket.ts).
The agent tab descriptor additionally treats an active turn as `running` even
if the last lifecycle snapshot still says otherwise; see
[`packages/app/src/panels/agent-panel.tsx`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/panels/agent-panel.tsx).

The workspace tab UI renders `running` as an animated ring, `failed` and
`attention` as colored dots, `needs_input` as an alert glyph, and `done` with
no marker. The implementation is in
[`workspace-tab-presentation.tsx`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/screens/workspace/workspace-tab-presentation.tsx)
and
[`status-dot-color.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/utils/status-dot-color.ts).

Paseo sets `finished` attention on the `running -> idle` edge and `error`
attention when entering error. Permission attention is not auto-cleared;
finished/error attention may clear on focus, input, send, or blur, with a guard
against immediately clearing attention that arrived while the tab was already
being viewed. See
[`agent-manager.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/agent-manager.ts),
[`use-agent-attention-clear.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/hooks/use-agent-attention-clear.ts),
and
[`agent-attention.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/utils/agent-attention.ts).

Paseo's resident-agent lifecycle is separate from tab layout. Idle agents stay
resident indefinitely; closing/reloading/archiving or daemon shutdown owns
runtime release. The lifecycle and recovery boundaries are documented in
[`docs/agent-lifecycle.md`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/docs/agent-lifecycle.md).

### Plan is capability-driven and provider-specific

Paseo has two UI representations of Plan:

- a provider mode advertised in the normal mode selector; the selected label
  and icon appear beside the composer, and `Shift+Tab` cycles modes;
- a provider feature toggle named `plan_mode`, presented as a blue-highlighted
  Plan on/off control.

The command center prefers the `plan_mode` feature, then falls back to a mode
whose `colorTier` is `planning`, whose id is `plan`, or whose id ends in
`#plan`. See
[`mode-control.tsx`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/composer/agent-controls/mode-control.tsx),
[`agent-control-contributions.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/command-center/agent-control-contributions.ts),
and
[`policy.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/app/src/agent-controls/policy.ts).

Paseo's provider interface permits `setMode` and `setThinkingOption` to return
a notice such as “applies next turn”; the app renders it as a generic toast.
The current feature interface `setFeature` has no notice return value. This
asymmetry matters for Codex Plan and is visible in
[`docs/providers.md`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/docs/providers.md).

The official changelog introduced Codex Plan with a dedicated review card in
0.1.45 and later added `Shift+Tab` mode cycling. See
[`CHANGELOG.md`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/CHANGELOG.md).

## Provider capability matrix

| Provider | Paseo capability source | When the switch becomes effective | Paseo presentation | Claude Hub contract for this feature | Current status |
| --- | --- | --- | --- | --- | --- |
| Claude | Static `plan` entry in Claude's provider modes | Immediately after the active Claude SDK query accepts `setPermissionMode("plan")` | Plan appears in the mode control. `ExitPlanMode` becomes a Plan permission with Reject / Implement and, when returning to bypass, Implement with Bypass | Expose Default + Plan only when installed `claude --help` confirms the required flag. Selection is persisted as `chat_mode` and applies to the next Claude Chat subprocess/turn; reject switching while a turn is in flight | Implemented. Local capabilities returned Default + Plan; a live Plan turn reached working then idle without changing the workspace. Paseo-style approval cards are not implemented |
| Codex | `collaborationMode/list`; Plan is a `plan_mode` feature only if a plan/read preset is advertised | The selected collaboration mode is attached when the next `turn/start` params are built; it does not mutate an already-running turn | Blue Plan feature toggle. At turn completion, the plan becomes a dedicated permission card with Dismiss / Implement; Implement disables Plan and starts a normal implementation turn | Discover provider presets fail-closed and expose Plan only when the active app-server/thread can supply a valid collaboration mode. Persist `chat_mode`; send the selected preset with the next turn; reject switching while a turn is in flight | Implemented for capability discovery and next-turn payload. Local capabilities returned Default + Plan; provider-mode compatibility is covered by a fixture. Dedicated approval cards are not implemented |
| Cursor | Runtime ACP handshake: `availableModes` / current mode or a mode config option | Only after `setSessionMode` (or the config-option RPC) succeeds; provider `current_mode_update` may later reconcile it | Plan appears as a normal mode when the runtime advertises it. Generic ACP plan updates render in the timeline; Paseo does not synthesize the Claude/Codex dedicated Plan approval flow for Cursor | Expose Default + Plan only when the installed Cursor CLI advertises/supports the required Plan flag. Apply selection to the next Cursor Chat subprocess/turn and reject switching in flight. Do not infer support from provider name alone | Implemented with installed-CLI capability gating. Local capabilities returned Default + Plan; no live Cursor Plan turn or dedicated approval flow was claimed |

### Evidence behind the matrix

- Claude modes, immediate SDK mutation, and Plan approval actions:
  [`packages/server/src/server/agent/providers/claude/agent.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/claude/agent.ts).
- Codex feature declaration, collaboration-mode discovery, next-turn payload,
  and synthetic approval:
  [`codex-feature-definitions.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/codex-feature-definitions.ts)
  and
  [`codex-app-server-agent.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/codex-app-server-agent.ts).
- Cursor is the built-in `cursor-agent acp` provider and inherits generic ACP
  mode negotiation:
  [`provider-registry.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/provider-registry.ts),
  [`cursor-acp-agent.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/cursor-acp-agent.ts),
  and
  [`acp-agent.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/acp-agent.ts).
- Paseo's real optional smoke against `cursor-acp@0.1.0` observed
  `default`/`plan` and successful mode RPCs, but it is not proof that every
  installed `cursor-agent acp` version supports Plan. The same smoke records
  that its wrapper observed no permission calls:
  [`cursor-acp-smoke.test.ts`](https://github.com/getpaseo/paseo/blob/78f7796eb44223c7fa252d3c537263e3a0b34875/packages/server/src/server/agent/providers/cursor-acp-smoke.test.ts).

A local probe of Codex app-server `0.151.0` returned `Plan` with
`mode=plan`, no preset model, and medium effort, plus `Default` with
`mode=default`. That confirms the current machine's shape only. Paseo still
matches Plan by the provider-facing name (`plan` / `read`) and sends the
preset's own `mode`, so Claude Hub needs a compatibility fixture for a future
preset whose display name and protocol mode are not identical. The backend
fixture covers a Plan-facing preset whose provider mode is `read` and verifies
that `turn/start` receives `mode=read`, rather than the UI id `plan`.

## Claude Hub product decision

Claude Hub borrows Paseo's capability-driven mode control but does not copy
Paseo's complete unread-attention state machine in this iteration.

### Chat versus Terminal activity

- Chat tabs use the native structured runtime snapshot:
  `idle | working | attention | offline`.
- Terminal tabs retain the existing tmux/terminal status path.
- A Chat status marker describes native runtime activity. It must not mean
  “WebSocket connected” or “tab mounted”. Before a native session is started,
  an installed provider may report idle/ready-to-start; a missing provider is
  offline.
- The frontend must not reuse one terminal boolean (`is_active`) as the Chat
  status truth. Missing or failed native capability/runtime resolution is
  `offline`, not optimistic idle.
- Paseo's unread `finished` attention and its five-bucket priority remain a
  reference for a future notification layer. They are not claimed as part of
  this delivery.

### Mode selection

- Backend capability discovery owns which modes are visible. The frontend
  renders only `available_modes`; it must not guess by provider name.
- The public modes for this iteration are `default` and `plan`. A provider that
  cannot prove Plan support exposes only `default`.
- Mode is stored per Chat as `chat_mode`. It is not a global provider setting,
  does not change Terminal behavior, and must survive page refresh/runtime
  rehydration.
- A successful switch applies to the **next turn**. A turn already in flight
  rejects the switch rather than creating ambiguous desired/effective state.
- The backend persists the mode only after provider/session validation
  succeeds. A failed `PUT` leaves the previous selection visible and returns a
  user-facing error.
- The current implementation intentionally does not add a general in-flight
  request id/idempotency guard beyond rejecting a mode change while a turn is
  active. Concurrent double-click/race hardening remains a follow-up risk, not
  an implemented guarantee.
- Claude Code Plan mode can write an internal plan artifact under
  `~/.claude/plans`. The validated live turn did not modify the selected
  workspace, so “read-only” here means no workspace edit; it is not a promise
  that the provider writes no state anywhere in the user's home directory.
- Paseo's provider-native Implement / Reject Plan approval cards are not part
  of this implementation. Plan selection constrains the next provider turn but
  does not add a post-plan implementation approval workflow.

### Existing Chat lifecycle baseline

This mode work sits on the existing structured-session lifecycle; it does not
replace it:

- A visible `StructuredPane` hydrates capabilities and durable event history,
  then uses SSE as the low-latency path with ordered long-poll reconciliation.
- Switching away or unmounting the pane closes SSE and aborts its long poll,
  hydration, and pending mode request. Claude Hub therefore does not keep one
  browser connection open for every inactive Chat.
- The backend tailer remains the sole in-process stream owner during a
  five-minute (`IDLE_TTL_S=300`) zero-subscriber grace period. This permits a
  quick switch-back without rebuilding provider state while still bounding
  idle resources.
- Claude and Cursor are one-shot subprocesses per turn. Their verified provider
  conversation id is persisted and passed to the next process. Codex owns one
  app-server process/thread for the active tailer generation and resumes the
  persisted thread after reaping/retry.
- UI completion follows authoritative turn terminalization. A Claude/Cursor
  one-shot process reaching EOF is a transport signal that the tailer must
  consume and reconcile; EOF alone must not clear working state or re-enable
  mode controls before the turn is terminalized.
- The append-only event history is the structured rendering authority. The
  provider conversation id is recovery identity, not a substitute transcript;
  hydration/retry must not append the same provider history again.
- Provider/process death, hard-failed source state, and explicit Retry are
  recovery boundaries. Browser connectivity alone cannot promote a failed
  provider to idle/healthy.

## Recovery and failure boundaries

1. **Capability discovery fails closed.** Timeouts, missing binaries, malformed
   Codex presets, missing active model/thread metadata, or an unsupported
   Cursor/Claude flag must hide Plan and retain Default.
2. **Hydration is authoritative.** Refresh/reconnect reads the persisted Chat
   mode, probes the current provider capability, and publishes a mode as current
   only if it is still advertised. A stale persisted Plan must not make the
   structured pane appear healthy when Plan can no longer be constructed.
3. **No mid-turn ambiguity.** Reject a mode mutation while the provider owns a
   turn. Do not silently queue it or relabel the active turn.
4. **Failure does not commit.** Provider probe or mode-update failure must keep
   the previous persisted and rendered mode. The composer remains usable after
   the error is surfaced.
5. **Tab switch owns cancellation.** A mode request belonging to a previously
   selected Chat must be aborted or ignored; a late response cannot overwrite
   the new Chat's capability state.
6. **Provider process loss is explicit.** EOF/crash during a turn enters the
   tailer's terminalization path; raw one-shot EOF is not itself the UI
   completion boundary. The authoritative completed/failed terminal state then
   moves Chat activity to idle, attention, or offline as appropriate. A
   reconnect must resume through the provider conversation id and durable event
   history; it must not duplicate canonical user/assistant turns.
7. **Plan approval is not inferred.** Claude/Codex provider-native plan approval
   may be mapped only when the adapter emits a verifiable plan boundary. Cursor
   Plan support alone does not authorize a fabricated approval card.
8. **Legacy migration is one-way.** Old top-level-tab `session_kind=agent`
   values may load as Chat; old Workspace managed-session values normalize to
   Terminal. New payloads and persistence emit only `chat | terminal`.

## Validation

Final merge-gate validation completed with:

- backend CI-like no-tmux suite: **1,223 passed, 20 skipped**;
- frontend unit tests: **253 passed**;
- frontend ESLint and production build: passed;
- backend Black, isort, and mypy: passed;
- local capability probes: installed Claude, Codex, and Cursor each returned
  Default + Plan;
- live Claude Plan probe: the first post-submit sample (`t=0`) was working; the
  tab remained working during the initial wait and streamed output, then became
  idle in the same sample that authoritative turn completion arrived. The
  workspace stayed unchanged; Claude wrote an internal plan file under
  `~/.claude/plans`;
- live/API boundary probes: an in-flight mode `PUT` returned HTTP 409 and mode
  survived a cold restart;
- desktop UI probe: Chat and Terminal status indicators exposed the expected
  `data-status`, `aria-label`, and title;
- compact UI probe: 390x844 had no horizontal overflow and mode buttons were
  44px high.

These totals demonstrate the integrated implementation and its regression
surface. The checklist below still distinguishes the exact observed facts from
paths that the central run did not exercise live.

## Acceptance checklist

### Activity

- [ ] A new local Chat with an available native provider reports idle before
  its first turn; a missing provider binary reports offline. The missing-binary
  live branch was not exercised.
- [x] A live Claude Plan turn changes the Chat runtime status to working and
  returns it to idle on completion.
- [x] Desktop Chat/Terminal indicators expose `data-status`, `aria-label`, and
  title text, and frontend tests preserve the separate native-versus-tmux
  status policies.
- [ ] A provider permission/error produces attention in a live Chat. The error
  attention path was not exercised.
- [ ] A live active turn wins over a stale persisted status snapshot.
- [ ] Refresh and Chat-to-Chat tab switches do not flash an incorrect connected
  or idle state while native status is unresolved.
- [ ] Mobile status markers are independently verified to be legible without
  color alone.

### Mode capability and switching

- [x] The installed Claude CLI exposes Default + Plan; a live Plan turn leaves
  the workspace unchanged. It may write an internal `~/.claude/plans` file.
- [ ] A subsequent live Claude Default turn can edit after the Plan turn. This
  transition was not part of the recorded live probe.
- [x] The installed Codex app-server exposes Default + Plan, and the backend
  fixture verifies that the next `turn/start` uses the provider preset's own
  collaboration mode and active model.
- [x] The installed Cursor CLI exposes Default + Plan.
- [ ] Unsupported Claude/Cursor versions expose Default only. Capability gating
  is implemented, but an unsupported installed binary was not tested live.
- [x] Switching during an active turn returns HTTP 409 without relabeling the
  active turn.
- [x] The mode buttons are disabled throughout a working turn and return to
  enabled in the same sample that authoritative completion returns status to
  idle.
- [x] A successful mode switch persists through a cold restart for a top-level
  Chat tab.
- [ ] Two different Chats retain independent modes. No explicit two-Chat probe
  was recorded.
- [ ] Provider/update failure leaves the previous mode selected and the composer
  usable. The failure path was not exercised live.
- [ ] A late response from a switched-away Chat cannot replace the active Chat's
  mode capabilities. The guard has unit coverage in the frontend suite, but no
  explicit live tab-switch probe was recorded.
- [x] At 390x844 the mode selector has 44px buttons and introduces no horizontal
  overflow.
- [ ] Paseo-style Implement / Reject approval cards appear after Plan. They are
  explicitly not implemented in this iteration.

### Recovery and regression

- [ ] Claude, Codex, and Cursor Default-mode text/image turns still stream and
  restore from durable history. The three-provider image matrix was not run.
- [ ] Structured-pane SSE + long-poll transport disconnects when the pane is
  switched away. This lifecycle was not re-probed in central acceptance.
- [ ] Existing Chat history and provider conversation ids are not duplicated by
  capability probing or mode changes. No dedicated live duplication probe was
  recorded for this feature.
- [ ] Legacy top-level `agent` state loads as Chat, managed-session state loads
  as Terminal, and no create/update response emits `session_kind=agent`.
  Existing migration tests passed inside the broad suite, but no new live
  migration probe was recorded.
- [x] Current UI and changelog copy do not claim Claude/Codex-style Plan
  approval for Cursor; the missing approval-card workflow is explicit.
