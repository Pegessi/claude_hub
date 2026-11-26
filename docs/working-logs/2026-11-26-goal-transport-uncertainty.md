# Recoverable Goal completion boundaries

## Evidence and scope

Two read-only incident reconstructions showed the same boundary: successful
native activity, a transport-disconnection error, then a failed completion with
`delivery_uncertain=true` and a quarantined turn identity. Provider logs showed
execution continuing afterward. Earlier rate-limit and malformed-response errors
had recovered inside the provider and did not emit the persisted terminal error.
Root and child execution were both represented; child errors were already scoped
to child status events, not latched into parent failure.

The artifacts do not identify which transport channel failed or why. The HTTP
transport deliberately quarantines ambiguous delivery; reconnecting a volatile
event stream or resending the prompt would not prove safe recovery. This change
does not add a retry loop, modify provider state, or change live service lifecycle.

## Ownership correction

The completion observer forwarded status/text/usage but discarded the transport's
failure metadata. The Goal reducer consequently turned an observation failure
into an irreversible Goal failure. Preserve the existing `retryable` and
`delivery_uncertain` booleans at this boundary, without parsing error text or
inferring recoverability from a provider/model name.

- Explicitly retryable failures pause the same Goal for manual resume.
- Uncertain failures pause and retain the owned turn ID with an uncertain dispatch
  state. Existing cancellation/reconciliation must succeed before explicit resume
  can dispatch again. A failed reconciliation leaves the Goal resumable and fenced.
- Neither path automatically retries or aborts the provider. Checkpoints, objective,
  accounting and completion deduplication remain under their existing owners.
- Definitive failures still fail. Stop still pauses. Successful completions still
  process their Goal protocol. Native provider-owned Goals keep their own lifecycle.

## Verification

The sanitized SSE-to-store-to-Goal regression first reproduced `failed` instead
of `paused` on the unchanged implementation. It covers a root-active disconnect,
a disconnect while children outlive the root, and a failed status monitor. The
matrix checks explicit metadata, malformed booleans, fatal failure, Stop, success,
duplicate accounting, cold recovery and reconciliation failure before resume.
Existing native child-wakeup coverage now includes failed and aborted children.

Focused tests: 186 passed. Backend mypy: 722 files passed. Ruff reports only an
unchanged baseline late-import E402 in the route-registration module; the scoped
check with that existing violation excluded passes. Independent read-only review
found no actionable issues and reran 16 boundary regressions successfully.

The initial `scripts/verify.sh all` passed every target except backend tests:
an unchanged web-session test raced a background listener's authentication
failure. The isolated test passed, and the complete default backend rerun passed
7,804 tests (10 skipped) with combined coverage of 85%. No unrelated test or
production behavior was changed to mask the first failure. Final verification
artifacts are recorded in the Task report. No live-provider recovery experiment
was run; a single cross-layer resume test using the production cancellation bridge
would strengthen the existing separate controller and transport coverage.
