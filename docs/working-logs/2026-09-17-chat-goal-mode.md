# Chat Goal Mode

## System overview

Chat Goal mode is a persistent, thread-scoped execution contract layered above
the existing structured Chat transport. It is deliberately separate from the
provider collaboration mode (`Agent` / `Plan`): collaboration mode controls one
turn, while a Goal decides whether work continues across turns.

The user keeps one conversational front door. Claude, Cursor, Codex, and TraeX
all expose the same Goal UI. In this version, `hub_managed` is the sole
execution owner: Claude Hub persists the lifecycle and dispatches bounded
continuation turns through the existing native Chat transport. Codex Goal RPCs
are typed for future negotiation, but capabilities deliberately continue to
advertise `hub_managed` until provider-state reconciliation exists. Never run
both schedulers for one Goal; provider names are not used as frontend policy.

## Product contract

- A Chat thread has at most one unfinished Goal.
- The objective is explicit user input and cannot be silently narrowed by the
  model. Starting a Goal is never inferred from an ordinary message.
- A token budget is optional and is only set when the user supplies it. Usage is
  labelled `exact`, `estimated`, or `unavailable`; an estimate is never shown as
  exact. A server-side turn cap remains available even when token usage is not.
- `Stop` continues to mean stop the current response. `Pause Goal` prevents the
  next continuation and safely stops the current Goal turn. `Clear Goal` ends
  the contract without deleting conversation history.
- A normal turn ending does not complete a Goal. Completion, blocking, and
  pausing require an explicit structured Goal signal or an authoritative
  provider Goal notification. Budget exhaustion is its own state.
- Goal activity continues without a browser subscriber and survives tab changes
  and backend restart. Recovery never blindly duplicates an uncertain dispatch.
- Goal mode does not grant new filesystem, network, merge, push, deletion, or
  external-communication authority. Native sub-agents inherit the parent
  conversation's authority.

## State and control plane

The public states mirror Codex where the semantics are portable:

```text
active -> paused -> active
       -> blocked -> active
       -> budget_limited
       -> complete
       -> cancelled
       -> failed
```

Dispatch state is tracked separately from display state so a crash between
persisting intent and provider acceptance is recoverable. Every automatic turn
has a stable run id, step id, and client turn id. The controller persists the
next dispatch intent before calling the provider.

The structured transcript remains the observation plane. Goal snapshots and
their audit events are a separate control plane so edit/resend and transcript
forking cannot rewrite Goal history. UI updates can be mirrored into the stream,
but SSE is never the scheduler.

## Provider mapping

| Capability | Codex | TraeX | Claude | Cursor |
| --- | --- | --- | --- | --- |
| Persistent conversation | app-server thread | app-server thread | verified resume id | resume id |
| Native Goal API | `thread/goal/*` when negotiated | only when verified | no | no |
| Fallback scheduler | Hub-managed | Hub-managed | Hub-managed | Hub-managed |
| Sub-agents | provider-native when available | provider-native when available | provider-native when available | provider-native when available |
| Usage | provider accounting when present | provider accounting when present | result usage when present | result usage when present |

Hub-managed continuations use a provider-neutral control envelope. The agent must
finish each turn with a machine-readable signal (`continue`, `complete`,
`blocked`, or `needs_input`) plus a bounded summary and evidence. Missing or
malformed signals fail closed instead of guessing from prose.

Each Hub-managed turn also proposes a bounded JSON checkpoint containing
verified progress with evidence, decisions, remaining work, an optional
blocker, and the next step. Hub validates and persists the latest valid value
plus at most ten historical versions. Invalid or missing checkpoints keep the
previous valid value and surface a warning; they do not replace the immutable
objective or independently fail the Goal. The next continuation receives both
the authoritative objective and latest checkpoint, while being required to
re-verify repository and runtime facts. This is deliberately working memory,
not a second Task Graph.

The provider receives the full continuation envelope, while the public Chat
timeline records only a neutral `Continue active Goal` user summary. Assistant
checkpoint/status blocks are removed incrementally even when tags cross stream
deltas or end malformed. Raw protocol text is handed only to the in-process Goal
observer and is never stored in or broadcast with transcript events.

Dispatch intent and provider acceptance are separate phases. A generated step id
is persisted as the expected turn id before transport I/O, so synchronous
completion cannot be lost. Terminal mutations persist their state first, wait for
any pending acceptance boundary, then cancel the exact turn. Cancellation failure
retains identity in `uncertain`; idempotent retry or resume reconciliation must
resolve it before another dispatch is allowed.

## UI

Goal is a separate composer control beside the Agent/Plan picker. Creation asks
for an objective and optional token budget. An active Goal renders a compact
status bar between the timeline and composer with objective, state, elapsed
time, usage quality, budget, and the currently valid action. Expanded details
show the complete objective, blocker, turn count, and timestamps.

The active conversation exposes compact Goal state immediately above the
composer. Status is always conveyed by text as well as color. Cross-tab Goal
badges are intentionally deferred until Goal summaries join the tab-list API.
While a Goal is active, manual Send/Queue/Steer and attachments are disabled so
the Hub remains the sole turn scheduler; provider approval answers remain usable.
After a transcript completion, the UI performs a bounded, version-aware refresh
because the durable Goal observer commits asynchronously. The disclosure exposes
live status semantics, Escape handling, labelled details, and coarse-pointer touch
targets.

## Historical branch decision

The earlier `feat/auto-mode-team-mvp` branch is retained as design history but
is not cherry-picked. It predates the Task Graph and current native Chat
transport and introduces a second `AgentTeam` topology. Useful ideas (single
front door, bounded subtask envelopes, execute/validate/judge separation, and
partial-failure recovery) are reused as contracts; team ids, team session roles,
and fixed provider assignments are not.

## Validation focus

- Pure lifecycle and budget policy tests, including stale completion after pause.
- Persistence and idempotency tests around dispatch intent and restart recovery.
- Claude/Cursor one-shot and Codex/TraeX persistent transport fixtures.
- UI policy, keyboard/focus, 375 px layout, and Goal/provider-mode independence.
- Existing agent-stream, tab persistence, frontend lint/typecheck/build suites.
- Deterministic interleavings for synchronous completion, pending dispatch versus
  pause, cancel failure/retry, and restart reconciliation.
- Transcript tests for split, malformed, and truncated Goal control tags, including
  proof that raw protocol fields never enter the durable event store.
- The full backend suite runs against a unique runtime home, HTTP port, and tmux
  socket; terminal input interrupts replay buffering and performs a later
  reconciliation instead of dropping live input.

## Follow-up review (2026-09-18)

The review reproduced duplicate sends of one durable step, stale pause retries
cancelling a resumed turn, hidden failed cancellations, resurrection of older
Goals after clear, mutable-budget create replay conflicts, lost accounting after
lowering the budget mid-turn, and rejected answers to active native questions.

The controller now claims each step with a short-lived acceptance future.
Lifecycle mutations serialize separately from the completion state lock, so a
provider can report completion while cancellation waits. Transport failures
retain uncertain identity and pause, allowing explicit reconciliation instead
of losing the task. A cancelled Goal remains current until its dispatch is idle;
the store checks unfinished execution as well as display status before replacement.

Codex/TraeX question answers use a strict current-turn path with no new-turn
fallback. Codex matches pending question IDs instead of dismissing every request.
Claude/Cursor do not have that blocking response channel: an answer to a tracked
card pauses the Goal and uses the existing follow-up path; the user resumes the
Goal when that exchange finishes. Ordinary manual sends remain locked while
execution is active or stopping. New/resumed Goals require Agent/Default mode.

Budget edits apply to the next dispatch decision; an already running turn still
completes and contributes usage. Expanded details expose budget editing, and an
uncertain stop has a visible retry action. A lost mutation response triggers an
authoritative refresh. A failed question submission no longer marks its card
resolved. Every snapshot write refreshes updated_at. Known turn IDs, rather than
unrelated version changes, govern completion reconciliation in the UI.

Validation includes deterministic interleavings, persisted cold-load retries,
provider question fixtures, split-tag parser tests, composable behavior tests,
and opt-in Playwright checks at 1280 px and 375 px. Run the latter with a dedicated
Vite server and `GOAL_UI_REVIEW_URL=http://127.0.0.1:<port> uv run pytest
tests/test_goal_ui_browser.py`; API replies are stubbed and no provider is invoked.
These checks establish lifecycle/UI behavior, not an empirical guarantee of
semantic goal fidelity for arbitrarily long live model conversations.

## References

- OpenAI Codex Goal protocol and runtime at commit `3c6f32c`: `ThreadGoal`,
  `thread/goal/set|get|clear`, Goal tools, accounting, and continuation runtime.
- `docs/TASK_GRAPH.md` for the existing execution-versus-orchestration boundary.
- `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md` for
  bounded native sub-agent envelopes and the single conversational front door.
