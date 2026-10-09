# Feishu Bot connection and conversation interaction v2

## Scope

This change improves the existing paired, one-to-one Feishu Bot path in three
places: initial long-connection readiness, bursts of inbound messages, and
assistant response rendering. It does not broaden the supported inbound event
types, create an implicit file upload path, or add interactive-card callbacks.

Development uses `~/claude_hub_worktree/feishu-interaction-v2` on
`codex/feishu-interaction-v2`. The primary Hub service and live Bot credentials
are not mutated by implementation or validation.

## Observed incident

The first live Bot was persisted at `2026-10-10 00:27:53.618 +0800`; the first
successful `POST /callback/ws/endpoint` was logged at `00:29:09.311`, about
75.7 seconds later. Credential validation had already returned HTTP 200. There
was no connection-failure/reconcile warning, and the connection subsequently
received messages and sent replies normally.

The long-connection adapter imported `lark_oapi` lazily inside the first Bot's
connection task. A clean process in the production virtual environment spent
about 12.7 seconds importing the SDK; the live process also had an unbounded
28.6 GB backend log and emitted more than 1,100 INFO records in the observed
window, dominated by complete tab/process-list dumps. Existing logs did not
record SDK import, endpoint discovery, handshake completion, or the successful
connected transition separately, so the remaining live delay cannot be
attributed more precisely after the fact.

The settings dialog adopted the create response while its connection state was
still `connecting`, but did not poll that state. A connection could therefore
be ready while the dialog continued to show its old snapshot until a manual or
unrelated refresh.

## Design

### Connection readiness and observability

- Start one process-wide, single-flight SDK import during backend startup. Bot
  connections await the same non-cancellable import future without blocking the
  FastAPI event loop or importing the SDK once per Bot.
- Log bounded durations and outcomes for SDK import, endpoint discovery, and
  WebSocket handshake. Logs include only stage names, duration and exception
  type; credentials and discovered WebSocket URLs are excluded.
- Poll the Bot pool only while the settings dialog has a `connecting` or
  retryable `failed` Bot, cancel the timer when the dialog closes, and stop when
  no transitional connection remains.
- Rotate backend logs and demote full tab-list diagnostics to DEBUG. This bounds
  future growth; it deliberately does not delete or truncate the existing live
  log without operator authorization.

### Bounded Feishu message serialization

Each paired Chat gets one active Feishu dispatch and a bounded FIFO of waiting
messages. Reservation happens before the first network await, preserving arrival
order. A full queue receives an explicit not-executed response instead of being
silently lost. Queue ownership is released on success, error and cancellation,
and idle registries are evicted.

For a valid bound message, the Bot best-effort adds Feishu's `Typing` reaction
while it is waiting or executing, then deletes that exact reaction record in a
`finally` path. Missing reaction scope or a transient reaction API failure never
blocks the model turn or its reply. The existing dispatch admission guard still
rechecks the current binding and target after queueing. An active Web turn, Goal,
approval or other non-Feishu owner is not bypassed.

Operators who want the typing hint should grant the Bot
`im:message.reactions:write_only`; the rest of the integration remains usable
without it.

### Rich response contract

New inbound turns use a versioned `feishu-v2` provider text that says the final
answer is delivered to Feishu and asks for the reliably supported Markdown
subset: headings, lists, links, blockquotes, inline code and fenced code blocks.
Historical `feishu-v1` provider text remains immutable and replayable.

The bridge converts a safe final answer into a Feishu `post` containing an `md`
element. It bounds output, preserves balanced fenced code blocks, and falls back
to plain text when the content cannot be represented safely. Model-authored
Feishu `<at>` tags are neutralized before delivery so free-form text cannot cause
external mention side effects.

Images, files, audio/video, and interactive cards are intentionally not inferred
from Markdown. They require explicit upload, permission, lifecycle and (for
cards) action-callback contracts.

## Verification

The final candidate was checked with the repository-pinned Node, pnpm and uv
versions. Results:

- Focused backend coverage for the Feishu pool, WebSocket adapter, protocol
  bridge, formatter and ttyd logging: 316 passed. This includes FIFO ordering
  and capacity, external busy admission, reaction degradation, cancellation
  cleanup, same-message replay after a queued cancellation, structured replies,
  SDK single-flight import and log rotation.
- A separate formatter boundary probe checked 30,772 balanced combinations of
  backtick/tilde fences, lengths and truncation offsets. Every result stayed at
  or below 20,000 characters and retained balanced fences.
- Backend type check: 233 source files, no issues. Backend format check: 233
  files unchanged; isort skipped only its two configured exclusions.
- Frontend unit tests: 705 passed. ESLint and Vue type checking passed. The
  production Vite build passed with its existing large-chunk advisory.
- Documentation invariants and `git diff --check` passed.
- A task-owned backend on `:18182` and Vite server on `:5278` exercised the real
  settings dialog. It showed only Name, App ID and App Secret, reached the page
  without console errors, and both processes and their private tmux server were
  stopped after the smoke check. The shared service on `:8173` was untouched.

`./scripts/verify.sh all` was attempted before handoff. Its frontend, format and
type targets passed. Repeated fail-fast backend runs passed more than 1,660 tests
before unrelated asynchronous queue-status tests failed nondeterministically. A
no-fail-fast run then finished with 2,779 passed, 7 skipped, one failure and 12
setup errors: the assertion failure was an unchanged restart timeout test whose
subprocess setup returned macOS `Operation not permitted`, while all 12 setup
errors were unchanged Playwright tests whose isolated HOME had no downloaded
Chromium executable. Those environment failures are recorded separately from
the 316 passing tests that exercise every changed backend path.

Independent review found two candidate defects: a pre-dispatch cancellation
could retain a deduplication claim, and a Markdown truncation boundary could
unbalance a fenced block. Both were fixed and regression-tested. A second review
then found a mixed three/four-backtick truncation case; truncation now recomputes
the exact prefix state monotonically and verifies the complete candidate before
returning. Final independent review reported no findings.

## Remaining risks

- Typing reactions and extreme Markdown were not exercised in a real Feishu
  client during this task. Reaction permission remains optional and failures are
  intentionally non-blocking.
- The long-connection adapter relies on private methods in `lark-oapi 1.5.3`; an
  SDK upgrade should include a real connection smoke check.
- The existing approximately 28.6 GB live log is not deleted or truncated. On
  the first post-upgrade write it becomes `backend.log.1`; only a later rollover
  ages it out under the five-backup policy, so disk space is not recovered at
  initial startup.
