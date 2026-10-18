# Dot final-review wake notification

## Incident and scope

On 2026-10-18, a tracked task's independent reviewer passed its final result at
19:23:43 CST. Dot was woken at 19:24:24 but received a worker progress summary
saying the artifact was still in review. It replied `[quiet]`, which the existing
frontend hides. The final reviewer verdict and human-acceptance request were not
included in its context. A later user message prompted the visible report.

This repair adds final-review notification to the existing Dot watcher. It does
not change restart continuation, schedule admission, the inbox delivery ACK,
watched/delegated ownership, or human acceptance. No live Dot message, Feishu
notification, task acceptance, service restart, merge, or push was performed.

## Behavior

- A current sealed review in `review` with a completion timestamp and an outstanding
  human-acceptance request produces `task_review_passed`. The report must be a
  passing verdict from the current reviewer and cycle within the applied review
  window. Plan approvals, unsealed/stale reports, skipped reviews and already
  accepted tasks do not produce this event.
- The event includes the final verdict. Its result card retains the worker return
  from before the review request and adds the final reviewer conclusion, validation
  and risks. Late same-cycle worker returns, progress and questions cannot replace
  those reviewed artifacts or reopen an acceptance question.
- `announced_review_cycle` deduplicates final notifications independently of the
  ordinary worker-result cursor. Reopening into a new cycle permits a new notice.
  A new watch treats a currently completed review as history, like existing watch
  behavior. An already tracked task waiting for acceptance without the new marker
  can be notified once on upgrade.
- Inbox writes precede the work marker. If the marker write fails, retry can recover
  the card/marker from either the unread or already-read event without notifying
  again. This uses the existing bounded inbox history (200 read events); repeated
  storage failure followed by eviction of that history is not a transactional
  exactly-once guarantee. Normal successful markers survive inbox compaction.
- The fresh turn context explicitly asks Dot to report the final verdict, request
  human acceptance, avoid `[quiet]`, and use the existing `dot notify` path if a
  Feishu bot is bound. No automatic acceptance or deterministic outbound message
  delivery was added. Actual model compliance and live Feishu delivery remain
  separate end-to-end validation.

## Validation

The new tests use real Task/Report models, the canonical reviewer task-update
policy, Dot storage/watcher/context, and mocked task/report query and wake dispatch.
They cover watched and delegated tasks, consumed worker events, same-poll result
and verdict, new cycles, duplicate polls and service reconstruction, late reports,
plan approvals, rejected/unfinished/stale review evidence, unwatch, and inbox
write failure before and after event delivery. Existing Dot behavior tests remain.

- Focused Dot scope: 133 passed in 7.49s.
- Independent review found and verified fixes for three cases: delivery between
  inbox and failed-marker writes, late unreviewed worker artifacts, and late
  reviewer/worker attention echoes. The independent probes pass after correction.
- Initial full verification found one type-check failure caused by reusing an
  index variable name for a datetime in a comprehension. The name was corrected.
- Final strict `verify.sh all` exited 0: backend 4332 passed / 10 skipped in
  219.46s; frontend 1029 passed / 0 skipped; Black/isort, full backend mypy
  (534 source files), frontend lint/types/build, docs and checkout safety passed.
  One backend skip requires a clean committed delivery tree; it passed separately
  after implementation commit `44c26d7` (1 passed in 0.05s;
  `/tmp/claude-hub-verify.gfsaef`). The remaining skips are explicit browser/zsh
  prerequisites. Independent review approved the final code and documentation.
  Evidence: `/tmp/ch-dot-final-review-checks/final-all.log` and
  `/tmp/claude-hub-verify.ts1yhJ/pytest.log`.
- Not run: live provider/Feishu/API integration, browser-mocks, terminal-hmr,
  terminal-replay. No frontend source changed. A provider can still fail or ignore
  instructions; unit tests do not prove real notification delivery.
