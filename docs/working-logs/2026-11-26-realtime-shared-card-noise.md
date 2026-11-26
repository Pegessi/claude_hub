# Realtime shared-card backfill and p2p quiet windows

## Scope and design

Base: 09d6c6b7. One serial writer; read-only test-gap scouting and independent
review cover the full diff, not a closed list of files. No live database edits,
external messages, shared-service restart, frontend edits, or baseline cleanup.

The existing Web push journal and poller queue remain the intake authority. The
existing realtime triage ledger owns the decision and outbound reservation.
There is no additional scheduler or parallel notification pathway.

- Source is a plaintext card element: chat name (chat ID) · sender name (sender ID).
  Missing/duplicate labels have clean fallbacks; display casing is retained while
  matching stays case-insensitive. Name-cache reads use the router lock.
- Beat 1 is a shared card (update_multi=True) with Summary and pending Investigation.
  The ledger adds followup_due, initial_summary, message_id, app_id and the frozen
  event/attachment snapshot (including constituent IDs).
  Old announced investigations migrate into an owed follow-up with no card ID.
- Beat 2 preserves Summary and replaces Investigation/Suggested reply. The same
  Bot client PATCHes the original message and updates the quote-text cache. A
  single-line quote reply supplies the unread signal. Missing ID, changed Bot
  identity or PATCH failure sends a standalone final card. Failed ping retries
  the idempotent update/ping, not another model turn or standalone card.
- Malformed, timed-out or failed investigation yields an honest terminal status,
  with no raw provider error. Owner replies may suppress an unsent Beat 1, never an
  already-owed Beat 2. An explicit global disable still pauses delivery.
- Cleanup cannot replace completed decisions or cancellation. A failed archive is
  logged/annotated; completed decisions are delivered before retained cleanup is
  retried. Cancellation retains the announced investigation for replay.
- Outbound reservation keeps the existing ambiguity tradeoff: a process exit
  between reservation and remote send can lose a notification rather than duplicate
  it. This is not an exactly-once network delivery claim.

## Quiet-window and replay contracts

Normal p2p peer messages coalesce per chat after the latest **unique** arrival.
The queue does not wait on a quiet p2p chat before handling a ready group event.
New messages during an active diagnosis form a later batch; they never cancel the
current investigation. The batch carries ordered text, constituent IDs and image
inputs. The ledger freezes batch evidence before Beat 1; resumed delivery restores
the same source text/images even if only one journal member has arrived. Frozen
batches never absorb newer messages. Terminal constituents are removed from new
coalescing candidates before choosing an identity, so a handled latest message
cannot discard an older unseen one. Accepted/suppressed batches receipt all
constituents before forgetting ownership.

Normal owner participation suppresses new p2p triage for the active window; /ai
does not start that window. The router checks its owner marks, the Web cache and a
bounded history page. Group mentions/watch URLs and owed conclusions are exempt.

Queued messages remain in the existing journal until delivery/suppression. Replay
uses the same queue. Catch-up callbacks may return False for deferred work:
checkpoints do not advance past that gap, while unrelated chats still progress.
Cancellation leaves journal entries recoverable; no task-owned timer outlives the
existing queue worker.

Settings are available in realtime PATCH/API, the settings JSON, or environment:

| Setting | Default | Environment |
| --- | --- | --- |
| p2p_debounce_seconds | 75 | CLAUDE_HUB_FEISHU_REALTIME_P2P_DEBOUNCE_SECONDS |
| p2p_active_seconds | 900 | CLAUDE_HUB_FEISHU_REALTIME_P2P_ACTIVE_SECONDS |

Zero disables the corresponding filter. API values must be finite and between
0 and 86400 seconds. Invalid runtime values fall through to saved/default values.

## Validation contract and limits

- Run new followup/noise plus existing realtime suites in both file orders.
- Real FeishuBotClient plus mocked HTTP proves POST/PATCH/reply ordering, the
  original message identity, shared payload, cache refresh and fallback behavior.
- Controlled-clock tests prove quiet deadlines, chat independence, cancellation,
  owner-active boundaries, /ai/group/followup exemptions and journal recovery.
- Cleanup tests preserve successful single/two-beat decisions and the original
  CancelledError even when archive raises; retry does not rerun completed diagnosis.
- Run repository Black/isort, mypy, focused Ruff correctness checks, full
  scripts/verify.sh all, staged safety, and incremental author/content checks.

Live Feishu rendering/unread behavior and model intent accuracy need human
acceptance; HTTP mocks and prompt-contract tests cannot prove those external facts.
The referenced abbreviated privacy lesson was not retrievable during implementation;
do not equate staged checkout safety with an exhaustive private-string scanner.
Final Task reports record the exact checks, retained evidence and unresolved risks.

## Independent review fixes

Two replay failures were independently reproduced and covered by failing tests
before repair: an announced coalesced batch split after cancellation, and a
terminal latest retransmission hid an older unseen member. Durable batch evidence
and pre-selection member classification close both paths. Queue data/selection
now lives in feishu_realtime_queue.py; the poller still owns lifecycle and journal
settlement, keeping its existing file-size ceiling without a parallel scheduler.

## Final verification evidence

- Realtime suites: **137 passed** in each file order, with all six files included.
- New followup/noise suites: **41 passed**; final condition-based timing adjustment:
  **15 noise tests passed**. Positive async assertions await durable settlement,
  rather than assuming a fixed sleep is enough under full-suite load.
- Full standard entry point, with CLAUDE_HUB_VERIFY_SHARDS=1: **all targets passed**;
  backend **7845 passed, 10 skipped**, frontend **1565 passed**, backend coverage 85%.
  Black/isort, mypy (725 files), docs, checkout safety, frontend lint/types/build
  passed. Full backend evidence directory: /tmp/claude-hub-verify.210blrbu.
- Focused Ruff correctness selection E9,F63,F7,F82, staged safety and diff whitespace
  checks passed. All range authors use the required contributor identity.
- Independent read-only review re-ran both original failure probes successfully,
  then passed its focused suites in both orders with no remaining actionable finding.
- Earlier default 8-shard full run exposed an untouched Dot API test's
  order-dependent 503-vs-429 failure; its complete test file passed alone. No Dot
  code was modified. The final serial run included that test and passed the full
  default test scope, not an exclusion-based subset.
- Incremental added-line scans found zero private-domain, machine-path or private-IP
  matches. This is a bounded scan, not certification against the unavailable
  abbreviated lesson's exact pattern set.
