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

## UI

Goal is a separate composer control beside the Agent/Plan picker. Creation asks
for an objective and optional token budget. An active Goal renders a compact
status bar between the timeline and composer with objective, state, elapsed
time, usage quality, budget, and the currently valid action. Expanded details
show the complete objective, blocker, turn count, and timestamps.

The active conversation exposes compact Goal state immediately above the
composer. Status is always conveyed by text as well as color. Cross-tab Goal
badges are intentionally deferred until Goal summaries join the tab-list API.

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

## References

- OpenAI Codex Goal protocol and runtime at commit `3c6f32c`: `ThreadGoal`,
  `thread/goal/set|get|clear`, Goal tools, accounting, and continuation runtime.
- `docs/TASK_GRAPH.md` for the existing execution-versus-orchestration boundary.
- `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md` for
  bounded native sub-agent envelopes and the single conversational front door.
