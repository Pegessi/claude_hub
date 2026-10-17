# MASO Hub-managed Goal support

## Scope

Branch: `codex/maso-hub-goal`, based on `b9e074cadcb15f6d88cc95270eca7b48e50b9b7e`.

The requested behavior is direct-Chat Goal mode for the MASO HTTP provider. Hub
remains the sole Goal lifecycle and continuation owner. The change does not call
MASO Auto endpoints, enable its experimental feature, or change Workspace Task
execution. No primary Hub or MASO service was restarted.

## Design

The existing Goal controller already dispatches provider-neutral continuation
turns through the direct Chat transport and observes their persisted completion.
`MasoHttpSession` now advertises that existing `hub_managed` capability and
returns it in `StreamCapabilities`; no MASO-specific Goal state store or
scheduler was added.

MASO has a different provider completion boundary from one-shot Claude/Cursor
processes. Its `turn_end` closes one model request, while only the root
`agent_end` closes the full MASO run. A new provider-session capability therefore
prevents the tailer from synthesizing Goal completion from a trailing control
envelope for multi-stage service transports. MASO enables that guard; existing
one-shot synthesis and persistent app-server behavior remain unchanged.

The Goal prompt, checkpoint/status parser, exact-turn pause/steer/cancel path,
restart behavior and uncertain-dispatch policy remain the shared Hub
implementation. MASO message acceptance that cannot be confirmed still blocks
retries, including automatic Goal continuations.

## Validation

Focused mocked-provider checks currently pass:

- MASO capability advertisement, model variants and lazy session creation;
- Goal protocol redaction and raw observer delivery;
- no completion after MASO `turn_end` or child `agent_end`, even with zero Goal
  terminal grace;
- completion on root `agent_end` without MASO Auto API calls;
- generic provider-owned Goal completion for both persistent app-servers and
  multi-stage service transports;
- existing MASO cancellation, uncertain-delivery, Goal controller and Goal API
  regressions in the focused selection.

The focused selection passed 121 tests with 144 deselected.
`./scripts/verify.sh all` passed the final code candidate: docs, checkout safety,
backend format and types, 4,027 backend tests with 10 skips, frontend
lint/types/tests and the production frontend build. Existing Pydantic
deprecation and test-owned failure-path warnings remain.

An independent read-only review of the base-to-worktree diff found no P0-P3
correctness issues. It checked capability scoping, ordinary MASO message reuse,
root completion ownership, dispatch/cancel/restart races, tests and documentation.
The implementation remains the existing Goal controller plus one provider
completion-boundary capability rather than a MASO-specific state machine.

## Boundaries

The tests use the in-process fake MASO HTTP server and do not invoke a real
model, MASO Auto, or external service. Live semantic compliance with the Goal
control envelope is not claimed by these mocks. Plan mode and edit-resend remain
unsupported for MASO Chat, and managed Workspace Chats retain their Task-owned
controls rather than exposing direct-Chat Goals.
