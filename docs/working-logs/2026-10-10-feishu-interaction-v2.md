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

### Bounded Feishu message coalescing

Each paired Chat gets one active Feishu dispatch and a bounded queue of later
messages. The first message waits for a one-second quiet window so a short burst
can be submitted as one ordered model request; continuous typing can delay that
request for at most five seconds. A single message keeps its original visible
text, while a batch is labeled and numbered so the model handles it as one
request without losing arrival order.

Once native dispatch starts, later messages do not cancel or preempt the active
turn. They accumulate into one follow-up batch and run after the response. This
keeps the implementation in the existing external-channel adapter instead of
adding a provider-specific runtime broker or hidden interrupt path. An active Web
turn, Goal, approval or other non-Feishu owner is still rejected by the normal
Chat admission guard.

Reservation happens before the first network await. A full queue receives an
explicit not-executed response instead of being silently lost; queue ownership
is released on success, error and cancellation, idle registries are evicted, and
each constituent message retains its own durable deduplication claim. A cancelled
message that has not entered native dispatch remains retryable without cancelling
other members of its would-be batch.

For a valid bound message, the Bot best-effort adds Feishu's `OneSecond` reaction
while the message is being coalesced or queued. The batch leader changes to a
`Typing` reaction when native dispatch starts; all exact reaction records are
deleted in the route cleanup path. This lifecycle runs independently from native
dispatch, so missing scope, a slow API, or a transient reaction failure never
delays the model turn or its reply. Combined replies are anchored to the newest
message in their batch. A rejected batch does not make the next admitted message
look like a follow-up.

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
- The subsequent coalescing refinement adds focused coverage for one-second
  burst grouping, the five-second maximum wait, one non-interrupting follow-up,
  queue capacity, external busy admission, follower cancellation/replay, and
  per-message reaction cleanup.
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
type targets passed. The final no-fail-fast backend run completed all 2,843
collected cases with 2,797 passed, 7 skipped, one failure and 38 setup errors. The
unchanged restart timeout test could not start its subprocess under the macOS
verification sandbox (`Operation not permitted`), but passed when run in
isolation. All 38 setup errors were unchanged Playwright cases whose isolated
HOME had no Chromium executable. A supplemental run using the existing browser
cache started those suites normally: 39 cases passed, while three unchanged
terminal scrollback cases consistently timed out waiting for xterm history both
in the suite and in a separate rerun. No terminal code is touched by this
candidate. The changed Feishu test module passes all 76 cases; the independent
review also reran seven cancellation and retirement cases with asyncio debug
enabled and reported no findings.

Independent review found two candidate defects: a pre-dispatch cancellation
could retain a deduplication claim, and a Markdown truncation boundary could
unbalance a fenced block. Both were fixed and regression-tested. A second review
then found a mixed three/four-backtick truncation case; truncation now recomputes
the exact prefix state monotonically and verifies the complete candidate before
returning.

The coalescing refinement received a separate concurrency review. It found that
simultaneous shutdown cancellation could preserve follower deduplication claims
after the shared turn retired safely, and that cancellation during an error reply
could leave `queue.active` wedged. Followers now shield the shared outcome and
wait for its bounded retirement result, while batch resolution and queue release
run from an outer `finally`. Both races have targeted regression tests; re-review
of the fixes reported no findings.

A final cross-review found that reaction I/O still extended the nominal batching
deadline, successful combined replies were anchored to the oldest constituent,
and a rejected first batch incorrectly advanced follow-up state. Reaction work
now runs off the native-dispatch critical path, success replies use the newest
constituent as their Feishu anchor, and follow-up history advances only after Chat
admission accepts native delivery. Regression tests exercise stalled reaction
operations and both corrected queue semantics. A follow-up cancellation review
found that a second shutdown cancellation during reaction cleanup could orphan the
reaction task and replace retry-safe retirement with a generic cancellation. Route
cleanup now continues collecting the bounded task across repeated cancellations,
with a regression test covering dedup release and task ownership. The review also
identified that a message already acknowledged by Feishu but still waiting only in
memory can be lost if final shutdown exceeds the bounded drain. This is an explicit
operational tradeoff for the current local Hub deployment; durable inbound inbox
work remains out of scope.

## Remaining risks

- Typing reactions and extreme Markdown were not exercised in a real Feishu
  client during this task. Reaction permission remains optional and failures are
  intentionally non-blocking.
- The long-connection adapter relies on private methods in `lark-oapi 1.5.3`; an
  SDK upgrade should include a real connection smoke check.
- Accepted messages waiting only in the in-memory follow-up queue are not
  recoverable if process shutdown exceeds its bounded drain.
- The existing approximately 28.6 GB live log is not deleted or truncated. On
  the first post-upgrade write it becomes `backend.log.1`; only a later rollover
  ages it out under the five-backup policy, so disk space is not recovered at
  initial startup.
