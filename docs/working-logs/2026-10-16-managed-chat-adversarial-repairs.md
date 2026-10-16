# Managed Chat adversarial-review repairs

## Scope and delivery boundary

The user requested repairs for the three newly confirmed defects and the inherited
event-log isolation issue found by the adversarial review of
`9c76a553eb1ea0a6e715695c6e828764206760d0` against
`407acd8a6ab5447b04ec780460ba4d32d898f16c`.

This phase changes `feat/managed-workspace-chat` only. It does not update main,
push, deploy or restart the shared service. The preceding merge and its immutable
decision record remain historical evidence; they do not validate these later
repairs. The existing initiator-controlled Task remains in execution epoch 3.

## Required behavior

1. Claude/Cursor question fallback must admit only one answer for an exact
   question turn/call identity, even when concurrent requests wait behind the
   Workspace lock. Accepted answer ownership must survive provider-handoff,
   cancellation and card-resolution failures. Same-call retries preserve payload
   identity and must not consume a newer question.
2. Stop on a Chat reviewer remains authoritative across worker continuation.
   The next review must choose a compatible unpaused reviewer, creating one when
   necessary, while eligible prior reviewers retain same-task context.
3. Direct Chat deletion must strictly retire the stream while the tab owner is
   still discoverable. Failed or timed-out retirement retains a retryable owner;
   no tab lock may be held while acquiring inverse-order stream/send locks.
4. A cancelled asyncio waiter is not proof that its thread-backed file operation
   ended. Stream cleanup and ID reuse must wait for submitted disk writes, without
   silently approving deletion on timeout or dropping retained teardown state.

## Internal design decisions

- A lock-only question fix is insufficient: a durable envelope can survive a
  failed provider handoff while the card remains visually pending. A narrowly
  scoped ManagedSession question-to-call claim is saved with first mailbox
  admission. Existing records default to no claims. Claims share the session
  lifetime of immutable call fingerprints; no new independent persistence store
  or endpoint request contract is introduced.
- The existing first-enqueue persistence-failure policy is retained. Claim and
  pending/fingerprint state must remain in the same model snapshot; this repair
  must not introduce partial compensation that releases only the card claim.
- Event-store operations retain their locks until underlying I/O finishes despite
  waiter cancellation. The existing stop timeout bounds consumer join, not the
  entire flush/provider path. A timed-out consumer remains registered for retry;
  final flush, provider retirement and activity observers stay in the original
  caller so Workspace-lock reentrancy is preserved. This avoids a second teardown
  task and a parallel observer-delivery queue.

## Ownership and reviewer regression

The controls owner changes the question API, narrow mailbox/session fields and
control tests. The lifecycle owner changes event store, tailer, ttyd deletion and
associated tests. The parent changes reviewer selection, orchestration regression,
changelog and this log. Independent reviewers do not edit their reviewed scopes.

Reviewer selection now excludes `chat_paused` on prior-reviewer reuse. Three new
cases cover actual Stop -> Continue -> next review with available/new replacement,
and eligible prior-reviewer continuity. Before the fix, the two stopped-reviewer
cases failed and continuity passed (`/tmp/claude-hub-verify.PiieVU`). After the fix,
all 30 orchestration tests passed (`/tmp/claude-hub-verify.i22uzW`). Independent
scoped review passed five reviewer contracts (`/tmp/claude-hub-verify.5qq4nt`).
A later internal-call assertion was updated for the question helper's explicit
`question_key=None`; final aggregate verification below includes that combination.

## Independent-review follow-up

The first frozen candidate closed the original four counterexamples in its
focused checks. Fresh independent reviewers then exercised adjacent boundaries:

- A failed Chat processing-intent save could leave an uncommitted in-memory
  processing state. An immediate retry took the in-flight shortcut; normal
  receipt recovery could eventually repair it, so this was not permanent message
  loss. The fix restores the pre-intent session and linked Task before releasing
  the lock, retaining the already admitted envelope and question claim.
- Legacy delivery-list entries without fingerprints must not be adopted as a new
  question answer. This is a compatibility guard; ordinary production creation of
  such a legacy Chat state was not demonstrated. Native in-turn answers retain
  their existing exact provider-question semantics, not a new durable client-ID
  deduplication contract.
- Edit, rollback, retry, fence and forget now share one per-session lifecycle
  barrier, including periods without a registered tailer. A failed stop remains
  retirement-pending and rejects start/input until a safe retry succeeds.
  Rollback refuses replaced owners and retains backups when restoration is unsafe.
- Every owner-removal path now requires strict stop, including archive and
  shutdown. The obsolete optional strictness flag and its reset caller were
  removed together. The real failed-delete -> Archive -> failed-delete -> safe
  retry sequence can no longer hide a failed provider stop. Concurrent HTTP
  deletion during lifespan shutdown was not established; that separate test is
  a helper-level invariant check.

## Final verification

The final source/test freeze is revision 4 of
`/tmp/managed-chat-adversarial-fix-freeze.json` (731 files). The final evidence-only
edit to this log follows that freeze; production/test contents remain pinned.

| Check | Result | Evidence |
| --- | --- | --- |
| Canonical `verify.sh all` on the final source | PASS: backend 3714 passed, 8 skipped; frontend 858 passed; docs, format, types, lint and build passed | `/tmp/managed-chat-adversarial-fix-final-all-v4.log` |
| Independent final question/reviewer recheck | PASS: 64 tests, including unchanged adversarial persistence/legacy probes | `/tmp/claude-hub-verify.K7NU7c/pytest.log` |
| Independent final retirement recheck | PASS: 11 cases, including edit/Retry/Archive failure and safe retry | `/tmp/claude-hub-verify.dqW1oh` |
| Canonical isolated browser contracts | PASS: 11 runners, task-owned Vite port closed after cleanup | `/tmp/managed-chat-adversarial-repair-browser-summary.json` |
| Final-source Terminal HMR | PASS: 12 tests | `/tmp/managed-chat-adversarial-fix-hmr-v4.log`, `/tmp/claude-hub-verify.lh7UhU` |
| Parent original-counterexample recheck | PASS: one accepted answer and one 409, no queued competing answer; direct stream/image/tailer cleared; reused stream contains only the new owner | `/tmp/managed-chat-fix-probes/*-final.log` |
| Forced unrelated-manager fixture recheck | PASS: original FIFO assertions retained, 191 tests | `/tmp/claude-hub-verify.r2lAcp` |

The final independent lifecycle report is
`/tmp/lifecycle-repair-adversary-7833/FINAL-STRICT-RECHECK.md`. It closes the
API-proven edit, Retry and Archive retirement findings at the final production
hashes. The independent question reviewer closed both the processing-intent and
conditional legacy-ID boundaries. Their conclusions are bounded to these repair
paths, not a general bug-free guarantee.

Intermediate full-suite runs exposed three fixture problems, all corrected
without weakening their assertions: a synchronous fixture constructed an
async-loop-bound coalescer; a shutdown fixture bypassed the real manager
constructor and lacked its lifecycle fields; a Terminal-only FIFO fixture did
not isolate stream preflight and thus depended on unrelated managers left by
prior tests. The history/wait ownership test also now stubs provider capability
discovery rather than attempting a real Codex startup. Earlier failed/partial
logs are historical, not substituted for the final full pass.

The original immutable merge decision record still describes only merge
`9668717249fa5f12a6a36518ab3133a91c164e7b`. These repairs are a later ordinary
feature-branch commit with their own source pins and independent review; no new
merge decision record is implied.

## Remaining limit and unverified behavior

If artifact unlink fails after durable direct-tab retirement, existing code still
logs the failure without a durable cleanup retry handle. This separate inherited
limitation remains. The identity-before-artifact ordering was preserved; this
repair does not introduce a new deletion state machine or background cleanup.
Failed edit rollback may intentionally retain `.edit-bak` recovery evidence.

No live provider, shared-service restart, full terminal-replay/performance suite
or resource-savings measurement was performed. Browser contracts use mocked APIs;
retirement probes use simulated providers and real store/lifecycle paths. The
existing deprecation and bundle-size warnings remain non-failing. Main, push and
deployment remain on hold for the user's later decision.
