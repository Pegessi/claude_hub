# Six UI upgrades, fanned out and re-reviewed (2026-09)

Six features were implemented in parallel worktrees off `main` 2192a50, merged
into `feat/ui-upgrade-batch-20261109`, then reviewed by an agent that did not
write them. The review's findings were fixed by the same implementers in a
second round. This note records what landed, what the review changed, and what
is deliberately still open.

## What landed

- **Inbox (A).** One top-bar `Inbox` control with an amber badge counting only
  items that need a human, replacing the always-on `Status N` / `Agents N`
  pills. Groups: Needs you / Working / Recently done, with an All/Chats/Agents
  filter that also splits rows by owning workspace; peek, inline reply,
  keyboard cursor, phone sheet. Backend gained five optional fields on the tab
  status payload (`pending_question`, `activity`, `activity_since`,
  `last_reply_excerpt`, `last_reply_at`) and the internal status wording
  ("native provider turn is in flight") became "Responding" / "Ready" /
  "Last turn failed (reason)".
- **Decision cards (B).** The approval card is the question with full-width
  options, each carrying the provider's tradeoff line, a "Recommended" marker,
  number-key answers, a multi-question stepper, ≥44px touch targets, and a
  collapsed "You chose: X · receipt" state. Claude's question normalizer now
  keeps option descriptions (it was dropping them), and the shared
  question-protocol guidance tells agents to ask through the question tool with
  2–4 options instead of ending prose with a yes/no.
- **Evidence-first task detail (C).** Detail order is header → Evidence
  (acceptance criteria with per-criterion verdicts, validation, changed files,
  artifacts with thumbnails, risks, review/CI link chips) → Decision → description
  → collapsed Debug info. Board failures stay in Working but sort first with a
  danger tint, cards clamp to two lines, empty columns say what is missing, and
  the agents/reviewers band collapses when all counts are zero.
- **Diff review comments (D).** Diff rows carry file and old/new line numbers in
  a quiet gutter with one header per file; a `+` affordance opens an inline
  editor (hover/focus on desktop, row tap on touch, Shift-click range); a
  per-tab tray jumps back to the line and Send composes one message into the
  existing composer queue (no second transport).
- **Live work log (E).** A running turn shows `● Working · <step> · mm:ss` plus
  `12 edits · 6 commands · 4 reads · 1 failed · 3 sub-agents`; waiting on the
  user or a sub-agent drops the word "Working" and the pulse. Finished turns
  report `Activity · 18 steps · 1 failed · 21m` instead of raw event counts.
  "Ask aside" forks the Chat and posts the question there.
- **Goal plan-first (F).** Additive optional `plan_first`: the first turn may
  only plan (steps, acceptance checks, execution budget: helpers × provider/model,
  max parallelism, duration) and ends on the existing `needs_input` pause; the
  card is identified by the Goal snapshot's `plan_turn_id`, not transcript text.

## What the independent review changed

0 blocking / 16 non-blocking findings. Fixed before merge:

- **Enter on any panel button opened the cursor row instead** (high). Key
  decisions now live in a pure resolver: Enter belongs to buttons and text
  fields, arrows in a text field belong to the caret, Escape closes from
  anywhere.
- **The reply-excerpt scan blocked the event loop** while a turn streamed, and
  **`last_reply_at` was the stream mtime**, so an old answer could read "New
  reply · just now". The scan now runs in `asyncio.to_thread` and is skipped for
  working turns; the timestamp is returned only when the excerpt belongs to the
  newest turn.
- **`plan_approved` was set by any resume or steer**, so a "Revise the plan"
  message claimed approval. The flag is now `plan_answered`, set only after a
  turn carrying the user's own message is actually dispatched; a `<goal-plan>`
  block is accepted only from the planning turn or a parking turn.
- **A failed Ask aside stranded the user on an empty fork**; the pane now
  returns to the source tab before notifying, and the draft stays put.
- **Two browser gates had gone vacuous or stale** after the pills were removed
  (`.status-trigger`, `.tab-bar .agent-status`), and the diff gate never wrote
  the harness `report.json` it was graded on. All three now assert the new
  surface, and two new gates (`browser_agent_inbox.py`, `browser_work_log.py`)
  cover surfaces that had none. Each was proven to fail with the defect
  reinstated.
- Smaller: stale diff-comment anchors are marked instead of silently ignored;
  disabled evidence chips became plain text chips; a compact review bar keeps
  the decision reachable in review state on a phone; the plan card's heading id
  is per turn; a provider-native Chat can no longer un-remember "Plan first".

## Deliberately still open

- A Goal parked on `needs_input` (now also the plan approval point) does not
  raise the Inbox badge. `GoalRunController.current()` deep-copies a run per tab,
  so per-tab reads on a 5 s poll are not cheap; the fix is one bulk
  `waiting_input_tab_ids()` read, tracked as follow-up.
- The Ask-aside failure path has unit coverage only.
- The new status fields have not been rendered from a real backend: the shared
  primary Hub still runs pre-batch code, so real-data screenshots show the old
  wording and the new contract was exercised on mocked payloads.
- `tests/test_ci_health.py` fails on this machine independent of the batch (its
  helper looks for `ERROR:` on stderr while pytest writes stdout), and the
  default backend scope aborts there; the batch ran the remainder file by file.
