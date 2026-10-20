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
  creation timestamp. Supported absence still disables the Hub protocol when
  the last historical Goal was Hub-managed.
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
