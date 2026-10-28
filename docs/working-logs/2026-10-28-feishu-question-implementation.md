# Feishu question cards: first release implementation

Task: `32cc7d3b-c477-409a-a9eb-623e5267b814`.
Accepted design baseline: `5654ae24c3b26c4ad920ca632c2d68451a9127a1` on
`codex/rv-feishu-interactive-cards`. Worktree remains
`/home/tiger/claude_hub_worktree/rv-feishu-interactive-cards`.
Fetched origin/main before implementation; it was already an ancestor. Other rv
branches and the running Hub/Feishu configuration were not modified.

## Shipped boundary

Single non-secret question, single choice or correlated free text; per-bot
callbacks default **off**. Raw schema-2 cards use distinct opaque tokens with
only digests at rest. Other instructs the user to reply to the card or use
`/answer QUESTION_REF text`. Unsupported/multi/secret shapes retain exact web
answering and do not export a subset. Tool/plan permissions, Task acceptance,
terminal prompts and schedule commands are excluded.

The accepted design's defaults remain: HTTPS callback transport, existing
WebSocket message transport, no SDK monkeypatch, 24-hour remote actions,
100 open questions/tab, 64-KiB answer text, and explicit remirror. A full opaque
question reference is used in text commands rather than maintaining a second
short-alias index. No managed Feishu pairing eligibility was widened; managed
web answering retains Workspace ownership. Screenshot catch-up prompts use the
normal stream-question path; there is no new timer or run-now callback.

## Ownership and implementation

- `QuestionAnswers` is shared web/API/Feishu admission. It validates the exact
  live session incarnation, turn, call, epoch and complete question. It never
  converts a rejected answer into a new prompt. Unsupported web shapes and an
  unavailable optional inbox retain a strict runtime-only web path.
- `ChatMessageInbox` owns v2 question snapshots, answer envelopes/claims,
  callback fingerprints/nonces and mirror outbox state in its existing atomic
  file. Migration retains a protected v1 backup. A dedicated answer drain can
  bypass an ordinary FIFO waiting on that question. Unknown writes stay fenced;
  provably pre-write restarts retry. Terminal content is removed after 30 days,
  while identities/fingerprints remain as tombstones.
- Native providers receive an exact original question response; pipe failure is
  observable uncertainty. Claude/Cursor follow-ups retain their original card
  until answer or invalidation. The shared runtime rejects old epochs. Direct
  input, ordinary inbox dispatch and managed mailbox dispatch respect answer
  ownership. Managed question envelopes enter ahead of unsent ordinary messages,
  preserving the ordinary messages' relative order and mailbox fingerprints.
- `card_actions.py` implements encrypted URL verification, raw-byte signature
  authentication, expected app/tenant/operator/chat/message/binding checks,
  nonce/event deduplication, bounded intake, mirror send/update fences and reply
  interception before batching. Remirror and binding rotation invalidate old
  actions; validation is repeated at claim and dispatch boundaries.
- Existing stream activity publication records questions/resolution after stream
  append; native live state remains the authority. The existing inbox lifespan
  startup/shutdown in `main.py` owns the new answer/mirror tasks too, so no second
  lifecycle runner was introduced. Supported external questions yield their
  completion waiter without cancelling the provider; unsupported approvals retain
  the existing fail-closed web-required behavior.
- Per-bot protected callback settings require expected revision; read APIs and
  repr omit keys. Existing send callers remain compatible while new methods
  retain message identity, UUID and PATCH support. Parent/root IDs are preserved.
- Web cards poll safe question projections for claimed/resolved/stale/uncertain
  status after reload. `tab question list/answer`, question REST endpoints, and
  `dot notify --question-ref` use existing owners; they cannot fabricate a provider
  question or execute a task/schedule command.

Setup, scopes and reverse-proxy exceptions are in
[FEISHU_QUESTION_CARDS.md](../FEISHU_QUESTION_CARDS.md); storage/API contracts are in
[CHAT_MESSAGES.md](../CHAT_MESSAGES.md). No console/proxy deployment was performed.

## Validation and independent evidence

- Isolated bot/card surface: 164 focused tests plus format/types passed before
  integration (`35909a6`; source subtask commit `c18a24d`). Independent tests
  landed in `d30a184` (source `dadc2a2`); all source copies and test evidence in
  the auxiliary worktree were preserved, not cleaned up.
- New security/recovery tests caught four real defects before integration:
  pre-write restart handling, retained question text, corrupt answer ownership
  references and whitespace call IDs. Those now have passing regressions.
- Focused final native/CLI/bridge checks: 48 passed; managed mailbox/direct inbox
  checks: 52 passed. Focused callback/outbox suite: 40 passed, including a real
  loopback TLS socket with test-only certificate verification, signed/encrypted
  challenge <1s, intake <3s with slow fake provider, click/reply intake, duplicate
  event and rehydration. Broader focused runs are retained in worktree `.log`
  files; they are not a substitute for the strict final suite.
- Browser regression oracle was independently run against unchanged baseline UI:
  it failed on the new claimed-status assertion after reload. Evidence remains
  under auxiliary worktree `feishu-browser-red-evidence/` and
  `feishu-browser-red-run.log`. The new UI passed the same gate in the primary
  feature worktree (`feishu-choice-browser-green2/`). No real backend/provider
  request is part of this mocked browser evidence.
- Complete `verify.sh browser-mocks`: all 16 scenarios passed; evidence
  `/tmp/claude-hub-browser-mocks.cc16ovlh/run`, including the reload/stale-card
  screenshot and report. Owned Vite exited without forced kill, port closed,
  no scenarios unrun. Manual review server 5297 was also stopped; 5297/5298
  refused connections afterwards.
- `verify.sh terminal-hmr`: 12 passed in 95.33s, evidence
  `/tmp/claude-hub-verify.hvsvoz1n`. Initial HMR prerequisite failure was Corepack
  looking for pnpm under private HOME; using existing
  `COREPACK_HOME=/home/tiger/.cache/node/corepack` supplied the pinned tool without
  changing application configuration. Isolated test runtime was removed.
- An initial strict `all` run found a browser-test formatting issue and an
  existing fake-session bridge incompatibility; both were fixed. That run is
  retained as `feishu-choice-all.log` and is not presented as a pass.
- Bounded independent source review identified and verified fixes for two races:
  stale binding during an awaited runtime probe (bot gate now spans submission),
  and ordinary input overtaking a claim (shared admission locks plus native
  before-send guards). Dedicated tests reproduce the ordering and block both
  races. Goal API tests now exercise exact IDs/durable receipts rather than the
  removed uncorrelated JSON interception; ordinary Goal steering is preserved.
- Final strict all/check-staged/candidate review results follow in the final
  validation update. Existing UI/Feishu sandbox tests do not establish public
  callback reachability or actual app-console configuration. Human acceptance
  and independent candidate review remain required. No lessons needed.

## Final validation record

Product candidate: `1a836009c8580a85bc53c4b9c461a3491ccf1c26`.
Implementation range begins after accepted design `5654ae2`; no merge or push.
The following final commit records validation only and changes no product code.

`CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 ./scripts/verify.sh all` **PASS**, exit 0,
retained as `feishu-choice-all-final-tree.log` in the feature worktree:

- docs, checkout-safety, backend format (612 files), backend types (596 files),
  backend tests, frontend lint, frontend types, frontend tests and production
  build all passed. Backend: **5,528 passed, 10 skipped**, 333.58 seconds.
  The ten browser-marked tests are intentionally skipped in the default scope;
  the separate complete browser-mocks and HMR results above remain applicable.
- Backend test evidence: `/tmp/claude-hub-verify.up359am9` (JUnit, pytest log,
  provenance and coverage); disposable runtime `/tmp/ch-v-udh8ot0r` was removed.
  Final format/type evidence: `/tmp/claude-hub-verify.471oqna1` and
  `/tmp/claude-hub-verify.c3hdfbqu`.
- One dependency deprecation warning came from Uvicorn's WebSocket protocol
  import during the real loopback TLS test. Vite retained its existing large
  chunk advisory. Neither gate failed.
- The earlier `feishu-choice-all-candidate.log` also passed on `712a07d`; the
  final run above additionally includes runtime-teardown invalidation. Other
  failed/intermediate logs remain preserved and are not final pass evidence.
  An exploratory explicit backend scope included terminal browser tests without
  a browser executable and produced setup errors; the default strict scope and
  explicit isolated UI gates are the authoritative results.

Additional regressions cover fair mirror draining past eight workers, persisted
retry backoff, no automatic export of old web-only content on later enablement,
64-KiB answer display truncation without changing provider text, on-disk managed
mailbox identity/fingerprint proof, and runtime teardown. Managed controls retain
all 33 scenarios, now checking durable admission separately from delivery. They
still exercise exact JSON-RPC responses, mailbox ownership, concurrent claims,
publication/deletion ordering, recreated owners, save failures, cancellation,
uncertain delivery and explicit recovery. Combined managed/runtime checks passed
51 tests before the final teardown case; final strict coverage includes it.

The runtime-only corrupt-inbox web fallback also holds the managed owner guard
through native response publication. This preserves web availability without
weakening deletion ownership. Independent bounded source review caught binding,
ordinary-input and mailbox-proof issues; all actionable findings were fixed.
Final independent Workspace candidate review and human acceptance are still
pending. Console/public HTTPS integration was not attempted. Ports 5296/5297/5298
were checked closed; task screenshots/logs and auxiliary snapshots are retained.
No lessons needed.


## Review round 2 corrections

Reviewer F1 confirmed that the external completion bridge treated a supported
question's mirror failure as unsupported interaction and retired the provider.
Eligibility is now decided independently of outbound success. Valid enabled
bindings retain the pending question and release only the external waiter even
when send is uncertain or PATCH raises; unsupported/default-disabled paths retain
the existing web-required retirement policy. Real mirror timeout, persisted
uncertain send, and PATCH failure regressions assert no cancellation/retirement.

Reviewer F2 confirmed that replacing the sole mirror lost old reply correlation.
Remirror now atomically retains content-free old bot/app/chat/message identities
alongside the new mirror. Those tombstones survive inbox reload and body expiry,
are excluded from public projections, and consume stale parent replies before
ordinary batching without answering the new card. Unrelated messages remain
ordinary, while replies to the current card still admit one answer. Old opaque
button digests are revoked by replacement as before.

Focused callback/bridge verification: **81 passed**, including real loopback TLS;
`feishu-choice-review-fixes.log`, evidence `/tmp/claude-hub-verify.dvlu6l5x`.
No live app/proxy/state changes. Updated final strict verification follows.
