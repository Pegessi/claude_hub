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
actual SDK reconnect/connected callbacks and the final `agentClient.run` error.
The final catch also covers exhausted retries, which `onErrorNotRetried` misses
after attempt zero. Notifications are scoped to the actual agent store and
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

`./scripts/verify.sh all` passed all nine standard targets: 7,928 backend tests
passed and 10 skipped; 1,572 frontend tests passed. Evidence:
`/home/tiger/evo_workspace/outputs/cursor-reliability-20261126/verify-all.log`.
Focused Cursor recovery tests also passed (10 selected). Tests cover concurrent
session ownership, delayed callbacks, cancellation, diagnostic
failure, redaction boundaries, watchdog activity, duplicate error suppression,
same-conversation restart and successful subsequent turns. A real CLI probe
uses a separate task-owned conversation and isolated Hub runtime. The requested
`gemini-4-argon-high` is absent from the current account model catalog and the CLI
rejects new sessions using it. A separate `composer-2.5` probe succeeded on its
first round; real resume/cancel validation is still being investigated. This
changes no existing Chat's model and is not a same-model acceptance result.

External authentication availability, long-duration upstream reliability and
shared-service restart continuity are not guaranteed by this patch. No shared
Hub service restart or deployment is part of this change.
