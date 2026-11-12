# 2026-11-11 Session status light for a question waiting on the user

## Symptom

A Chat that had asked the user a question showed the amber "working" light in
the session sidebar (and the same state in the TabBar) while the Chat body
showed the interactive card "Waiting for your answer" — the same session read
"Working" and "waiting on you" at once. Reported on `mr 3829`
(`cursor`, native transport).

## Evidence

Live payload from the running Hub while the card was on screen:

```
$ claude-hub tab status           # the tab that was showing the card
{"tab_id":"dfbd892f-…","status":"working","status_text":"Working",
 "detail":"Responding","pending_question":true,"activity":"Responding"}
```

Stream records for the same turn (`terminal-tab-dfbd892f-…`):

- `07:20:54 tool_call_started AskQuestion`
- `07:20:54 approval_required {"kind":"ask_question"}` — the card the body renders
- `07:20:54 tool_call_completed AskQuestion {"status":"failed"}` — Cursor treats
  the ask as an out-of-band protocol, so its own tool call does not block
- `07:21:32… text_delta` — the assistant keeps narrating
- `07:22:30 turn_completed {"status":"cancelled"}`, then a new `turn_started`

So the runtime honestly reported a turn in flight; the ask was a side channel.
The inbox already treated that as a decision (`classifyRow` puts
`pending_question` above the `working` bucket, and `rowState` gives it its own
`needs` dot). `useTabStatus` — shared by the sidebar and the TabBar — did not:
it returned the raw `status`, so the row pulsed amber and read "Working".

## Fix

`frontend/src/composables/useTabStatus.ts` presents a reported
`pending_question` as finished work that is waiting:

- `getTabStatus` returns `idle`, so the row, its `data-runtime-status`
  attribute and the TabBar indicator drop the working treatment and pulse.
- `getTabStatusLabel` returns `Waiting for your answer` (a new
  `isAwaitingAnswer` helper backs it). The sidebar light speaks
  `Session status: Waiting for your answer` like every other row, and the
  TabBar indicator carries the bare sentence.
- The flag demotes the provider's own turn flag (`working`/`idle`) **only** —
  the rule that survived cross-review. It does not override `attention` or
  `offline`: those keep their colour, pulse and label and add
  `— a question is waiting`, so neither the failure nor the card is hidden. The
  reasoning is in the composable's comment and in "Failure wins over the card,
  deliberately" below; the earlier draft in this log (which trusted
  `pending_question` unconditionally, even on an `attention` snapshot) is
  superseded by it.
- An unread row drops the completion badge while a card waits
  (`ChatSidebarRow`'s `awaiting` prop), because answering — not reading — is
  the action, and the light adds `data-awaiting` with a small `?` in the same
  8px slot so a waiting session is not mistaken for a quiet idle one. The TabBar
  indicator carries the same attribute and a ring.

Nothing else changed: the backend keeps reporting the provider's own state
(`working` + `pending_question`), which is why the inbox, liveness and the
answer path are untouched. A pending question still outranks "working" exactly
where it did before; only the presentation stopped contradicting it.

### Failure wins over the card, deliberately

A second review pointed out that `pending_question` is not an unconditional
"you can answer now" gate. Verified in the code rather than in the inbox:

- `_require_startable_incarnation()` (`runtime/session.py:189`) only fails on
  retirement or a replaced/deleted owner, so an `attention` or `offline` snapshot
  keeps the card and reports `pending_question: true` (`tailer.py:547-567`).
- A follow-up answer is refused while the provider still owns the turn
  (`question_answers.py:389-390`, `409 question_busy`), and a follow-up card
  whose turn ended unsuccessfully is marked stale (`question_answers.py:484-521`)
  while `_pending_approvals` survives (no `APPROVAL_RESOLVED` is published for
  those agent types).
- A native (TraeX/Codex) card is only reported while `turn_in_flight`, so it is
  answerable by construction.

So `pending_question` demotes the provider's own turn flag (`working`/`idle`)
only: `attention`/`offline` keep their colour, pulse and label, and the label
gains `— a question is waiting` so neither fact is hidden (`useTabStatus.ts`,
`isQuestionWaitingBehindFailure`). The Needs-you inbox says the same two things
in the same case (`inboxModel.ts`), so the two surfaces cannot contradict each
other again. The inbox panel's dot follows the same rule through `rowDotState`
(moved out of `AgentInboxContent.vue` so `inboxModel.test.mjs` can pin it), which
also keeps a failed provider from wearing the amber needs dot while its row text
names the failure. The marker (`data-awaiting` / `?`) follows the demotion, so a
failed row does not claim to be a finished turn.

Known limit, tracked separately: while a follow-up turn is in flight the body
still renders its card and `Submit` answers `409 question_busy`; the row says
"waiting" there because the card is on screen and is what the user is asked
about. Making the card actionable-only-when-answerable is a body-side change.

## Validation

- `frontend/tests/tabStatusPendingQuestion.test.mjs` (new, real composable):
  blocked-while-working → `idle` + the sentence; cleared question → `working`
  again; attention/offline + card keep their own state and add the suffix;
  attention/offline without a card are unchanged.
- `frontend/tests/browser_sidebar_status.py` (new mocked SPA scenario, registered
  in `scripts/verify_browser_mocks.py`): at 1440 the blocked row is
  `data-status="idle"` with `aria-label`/`title` `Waiting for your answer` and no
  `.working-effect`, the running row keeps `working` with exactly one pulse, and
  an unread finished row keeps `Completed, unread`; the TabBar indicator agrees;
  at 820 the 8px light stays inside the sidebar and off the name; at 390 the
  TabBar carries the same sentence; a failed provider that still carries a live
  card keeps `Needs attention` and only adds `— a question is waiting`
  (`data-awaiting="false"`). The scenario fails with the old mapping
  (assertion reports the light as `working`), so it gates the behaviour.
- Whole frontend unit suite (1482 tests), `vue-tsc`, `eslint`, `black`/`isort`,
  `check_docs.sh` and the full 24-scenario `browser-mocks` gate.

## Known limits

- The label is driven by the poll's `pending_question`, so it follows that
  flag's lifetime. Cursor's out-of-band ask keeps it live while the provider
  finishes the turn, which is the reported case; once a new turn starts the
  mailbox marks the question stale and the row returns to `working` — correct
  for a session that really is running again.
- A card that goes stale is still rendered as an actionable approval in the
  body until its turn's transcript is next re-read. That is a separate surface
  (the body, not the status light) and was not changed here.
- The Workspace Agents board (`useWorkspaceRuntime` → `WorkspaceAgentStatus`,
  dot + `Working` pill) and the Feishu tab-status cards still print the raw
  `working` state for the same session the sidebar calls waiting, and the
  terminal pane's recovery keeps reading the raw status for its own decisions.
  Left as a follow-up: those surfaces also drive counts, filters and
  dispatch/recovery, so they need a separate review rather than the row mapping.
- A long tab name can push the TabBar indicator outside the visible phone strip
  (pre-existing `tab-bar.css` layout limit, not this state); the scenario
  therefore asserts the indicator's own state at 390px rather than claiming it
  is on screen.
