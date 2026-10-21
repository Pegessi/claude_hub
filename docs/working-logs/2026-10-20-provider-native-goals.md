# 2026-10-20 Provider-native Goal ownership

## User-confirmed behavior

A provider with an implemented native Goal API owns Goal state and continuation.
Claude Hub reads that state and routes user controls to the same native Goal.
Providers without a native API retain the existing Hub Goal controller. Do not
hide protocol output to conceal incorrect state or execution.

## Observed failure

The affected Codex Chat returned null from Hub's current-Goal endpoint while
native `thread/goal/updated` notifications reported an active Goal. Several
cancelled turns were immediately followed by provider-initiated turns. The tab
status still reported the previous cancelled turn even while new tools ran.

Hub's old capabilities always advertised Hub ownership. Stop only interrupted
a turn. Native Goal RPC helpers were present but unused by Goal lifecycle APIs.
Native automatic starts also lacked provider turn-ID tracking and retained the
previous runtime terminal state.

## Implementation boundaries

- Probe with native `thread/goal/get`: a supported API returning `goal: null`
  means there is no current Goal. Only an explicit method-not-found response
  selects the Hub fallback. Timeouts, auth failures, malformed replies and
  changed provider threads remain visible errors, not fallback permission.
- GoalRun remains the UI/API projection. It records execution owner, provider
  thread and creation/version fields. Existing matching Hub state can be adopted;
  unrelated or cleared generations do not authorize mutation of a newer Goal.
  Confirmed absence is recorded separately from a terminal outcome: a later
  replacement receives a new local identity even if Codex reuses a second-level
  creation timestamp. Protocol processing depends only on the current trusted
  Hub continuation metadata, never the existence of a historical Goal.
- Native user controls include explicit `origin: user`. Pause/Stop first pause
  native continuation, then interrupt its Goal-owned turn. Complete and Clear
  also stop Goal-owned execution before reporting confirmation. Failed stop
  remains uncertain; Retry stop never clears a completed Goal implicitly.
- Pending idempotency intents are distinct from accepted receipts. A retry
  reconciles the specific operation's postcondition or stays unconfirmed. It
  cannot return success just because a request ID was recorded before an RPC.
- Ordinary manual turns under a paused or limited Goal remain unrelated to it:
  they neither alter its native limit status on Stop nor increment its turn
  count. Terminal history remains unchanged when no current native Goal exists.
- Native create/resume never sends a Hub checkpoint prompt or runs the Hub
  continuation scheduler. Input steering pauses continuation, submits the user's
  unmodified text/images, then restores native active state.
- Provider events invalidate the cached Goal projection; API reads remain
  authoritative. Native statuses include `usageLimited` and `budgetLimited`,
  shown distinctly without restoring Hub budget controls.
- Native automatic starts reset stale terminal/protocol state and carry the
  consumed provider turn ID. State updates are exposed only after persistence.
- Hub-managed providers retain existing lifecycle, checkpoint and restart logic.
  No shared-service restart or mutation of the user's running Chat is part of
  implementation/testing.

## Ordinary Chat instruction scope

The earlier conversation retained a Hub continuation prompt in provider history;
its later ordinary turn received no fresh Goal prompt. Historical instruction
carryover is the supported explanation for the leaked checkpoint. New
continuation prompts explicitly expire their formatting requirements after that
response. The existing one-shot runtime guidance also
expires historical Hub checkpoint/status instructions unless the current turn
contains fresh Hub continuation instructions. It uses the same input-guidance
frame; no new assistant-output filter is added.

A fresh/resumed transport receives this rule with its first text input. An
image-only first input defers guidance as before. Already-running transports
that received older guidance require their normal restart/rotation before this
rule is sent; the implementation does not modify those live conversations.

## Cross-validation follow-up

The initial `a07826b` candidate passed the standard checks but failed independent
cross-validation. Those earlier green results do not validate this repair.

- A real `asyncio.wait_for(read_line())` boundary let Stop report success before
  the consumer claimed a provider-started turn. `read_line` now claims the guard
  and provider ID together; strict Stop requires completion confirmation and
  cannot report success through the ordinary kill/restart fallback.
- Retired provider IDs must not turn a later explicitly requested Hub turn into
  a native successor. Early notifications before a Hub `turn/start` ACK retain
  ordinary cancellation behavior when the ACK confirms the same turn.
- If the start was observed but the ACK failed, delivery remains uncertain.
  The provider guard, ID, image files and durable Hub turn remain owned until
  completion or confirmed Stop. The error remains visible; no failed completion
  or successful delivery is invented, including for explicit RPC rejection.
- If Goal state completes, disappears or changes while Stop is being admitted,
  the route re-reads it and only targets the originally observed turn. A changed
  Goal or an unknown Hub turn identity requires a fresh action (409), not a 500
  or an untargeted cancellation of newer work. A claimed active native turn
  without a Hub ID is still stopped through the native Goal path when the user
  did not specify an obsolete turn ID.
- Remove the backend's history-based `strip_goal_protocol` option and the
  frontend's unconditional trailing-envelope heuristic. Ordinary and native
  output stays intact in rendered text, copy and sharing. Actual Hub continuation
  turns keep their existing protocol handling, selected by trusted metadata.

Regressions include the original read/consumer counterexample, rejected Stop and
retry, two manual Hub sends with early notifications, accepted-start/ACK failure
with image ownership and durable completion, and completed/cleared/replaced Goal
races. API fixtures do not run the application lifespan: doing so in these
mock-runtime tests leaked its shutdown flag into later stream tests.

## Validation

Installed Codex 0.163.0 schemas and feature descriptions were generated offline
under a task-owned home. Fake-RPC, runtime, coordinator and authenticated API
regressions cover native state, ownership, absent/unsupported/error distinctions,
origins, replay/generation checks, Stop, steering, and non-native fallback. A
real-SPA browser scenario uses mocked APIs to verify native state changes and
confirmed clear. Full verification and independent review results are recorded
in the Task and task-local delivery report.

Native Goal fields do not include an atomic expected-generation mutation token
or a stronger Goal ID than the creation timestamp. A clear/recreate with identical
identity fields cannot be distinguished if absence was never observed. Hub
serializes its own controls and checks the current thread/local generation;
the provider remains authoritative for concurrent changes outside Hub.
