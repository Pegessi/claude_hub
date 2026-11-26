# Cursor ACP recovery visibility and runtime renewal

## Evidence and boundary

Baseline: `03926258`, Cursor CLI `2026.11.11-043ef28`. A paged read of all
15 local Cursor Chat streams found 11 error events on November 25–26 UTC:
five backend-runtime interruptions (three at the same second), two inactivity
timeouts, two generic sign-in errors, and two explicit network/stall failures.
This is a snapshot of existing Chats, not a provider-wide failure-rate estimate.
Summary evidence is retained locally at
`/home/tiger/evo_workspace/outputs/cursor-reliability-20261126/session-errors.json`;
it excludes conversation bodies and credentials.

The installed ACP runner enables neither agent retries nor structured connection
reporting by default. Hub already enabled retries through its existing preload,
but also imposed `maxRetries=8`: the current SDK interprets that as a shared
seven-retry cap instead of its native separate server/transport budgets. ACP's
connection callback only writes debug output, invisible to Hub's inactivity
watchdog. ACP also replaces `ActionRequiredError(action=login)` with a fixed
sign-in message, losing the upstream cause. These are verified code paths;
the old generic errors cannot establish which authentication failure occurred.

The local authentication check returned account details successfully. Cursor
caches credentials inside a running process, so a fresh CLI check cannot prove
the failed process used the same current credentials. No expired-token claim is
made. Shared Hub shutdowns remain a separate interruption cause.

## Change

The existing preload uses ACP's supported `extNotification` channel to expose
actual SDK reconnect/connected callbacks and original run error diagnostics.
The run catch also covers exhausted retries, which `onErrorNotRetried` misses
after attempt zero. Retriable errors are held until `processPrompt` finishes its
background-completion handling: a successful followup run clears the pending
error and explicitly resolves the Python inline transient error, even without
new text or tool updates. A `connected` callback cannot clear it. Notifications
are scoped to the actual agent store and
active prompt; cancelled, retired, child-session and old-generation updates
cannot reopen a parent turn. Diagnostic fields are bounded and allowlisted;
error text passes Hub's existing redaction before persistence. Error objects,
headers and causes are never serialized.

No synthetic heartbeat, blanket inference retry or second Hub retry loop is
added. A connected callback shows progress, not turn completion. Existing SDK
checkpoints, native retry budgets and cancellation behavior remain authoritative.
Terminal authentication or exhausted transport errors mark the runtime for
replacement before the next explicit user send. The same persisted conversation
is loaded, and no failed prompt is resubmitted automatically.

## Validation

Initial validation before cross-review: `./scripts/verify.sh all` passed all nine
standard targets: 7,928 backend tests
passed and 10 skipped; 1,572 frontend tests passed. Evidence:
`/home/tiger/evo_workspace/outputs/cursor-reliability-20261126/verify-all.log`.
Focused Cursor recovery tests also passed (10 selected). Tests cover concurrent
session ownership, delayed callbacks, cancellation, diagnostic
failure, redaction boundaries, watchdog activity, duplicate error suppression,
same-conversation restart and successful subsequent turns. A real CLI probe
uses a separate task-owned conversation and isolated Hub runtime. The requested
`gemini-4-argon-high` is absent from the current account model catalog and the CLI
rejects new sessions using it. A separate `composer-2.5` probe passed initial
send, process replacement with remembered context in the same conversation,
cancellation, and a successful send after cancellation. Its summary is retained
at `/home/tiger/evo_workspace/outputs/cursor-reliability-20261126/live-probe-result.json`.
The probe processes were stopped. An earlier probe returned empty text after
resume; the later complete pass does not establish long-duration reliability.
This changes no existing Chat's model and is not a same-model acceptance result.

The first independent review of `47f3ddfd` found no blockers. Two subsequent
independent cross-reviewers of `86db6471` found one P2 and superseded that
conclusion: ACP may run a successful background-completion followup after its
main run fails, but the initial patch permanently marked the prompt failed.
The cross-review reproduced this using installed SDK control flow and separate
Python provider/normalizer/consumer probes. Evidence is retained under
`/home/tiger/evo_workspace/outputs/cursor-reliability-20261126/cross-review/`.

## Cross-review repair (November 27)

The existing prompt-owned preload context now retains the latest retriable run
error only until the entire prompt finishes. A later successful run clears it;
another failed run replaces it. Cancellation suppresses the deferred diagnostic.
Terminal diagnostics replace the older inline transient message in the Python
provider, avoiding duplicate ERROR events and stale causes. Authentication errors
retain their immediate diagnostic path. No retry loop or prompt replay is added.

The first repair's five regression cases feed actual preload output into the
Python provider, adapter and native consumer: successful followup, failed
followup, connected without recovery, no followup and cancellation. Its focused
Cursor selection passed 15 tests. The installed SDK's actual `processPrompt` and background drain
also pass offline success/failure probes, with resource/model/transport
collaborators substituted; the successful case acks and emits no terminal error,
while the failed case nacks and preserves its final request diagnostic. Evidence:
`cross-review/p2-sdk-drain-recovered.json`, `cross-review/p2-sdk-drain-failed.json`
and `p2-focused.log` under the same output directory. These are deterministic
fault-injection checks, not real upstream network-failure tests. The earlier live
probe explicitly invoked `restart_for_recovery()` and does not establish the
failure-triggered automatic recovery path against a live provider.

Fresh independent review of `72b58e63` found the remaining terminal-only followup
case: the primary run can emit a valid answer and then fail, while a successful
background run emits only `turnEnded`. The installed SDK accepts and acks this,
but its ACP presenter emits no parent update; Python retained the earlier inline
error and marked the turn failed. The original counterexample is retained in
`cross-review/fresh-review-terminal-only-sdk.json` and
`cross-review/fresh-review-terminal-only-consumer.json`.

The preload now sends a private recovery notification only when a real successful
run resolves its pending retriable error. The Python handler resolves only the
transient error, preserving terminal errors, cancellation and ownership fences.
A sixth integrated regression covers terminal-only success after a valid answer;
the existing diagnostics test also checks stale/child recovery rejection and
that a recovery notice cannot clear a terminal failure.

Final code candidate `40ab175175b52851b5491535ca9ed2230270363a` passed all nine
`./scripts/verify.sh all` targets: 7,935 backend tests passed, 9 skipped, and
1,572 frontend tests passed. The expanded focused selection passed 18 tests.
Logs are `p2-silent-verify-all.log` and `p2-silent-focused.log` in the output
directory above; staged checks also passed.

Fresh independent review covered the complete seven-file diff from base
`03926258def403a722bc5c17519698c47d6778cf` and found no actionable P0–P3 issues.
The reviewer independently ran the installed SDK contract and Python consumer
probes for recovery with text, terminal-only recovery, recovery followed by a
new retriable or authentication failure, and cancellation. Both recovery cases
completed without ERROR or restart; later failures retained exactly one final
error and the restart marker; cancellation remained cancelled. Evidence and
limitations are recorded in `cross-review/fresh-review-40ab1751.md` and
`cross-review/fresh-review-40ab1751-consumer.json`.

This closes the cross-review P2 within the code-delivery scope of initiator Task
`ec360b7a-c8d4-471d-912c-da4fd04509bd`. The branch remains unmerged and undeployed;
integration and shared-service operations are outside this delivery.

External authentication availability, long-duration upstream reliability and
shared-service restart continuity are not guaranteed by this patch. No shared
Hub service restart or deployment is part of this change.

The isolated feature worktree, its backend `.venv` and frontend `node_modules`
are retained for the concrete integration follow-up. Small task-owned probe and
verification evidence remains under the local output directory.
No task-owned probe or review server remains running.
