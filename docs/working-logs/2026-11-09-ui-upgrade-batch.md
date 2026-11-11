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

## Third review: a different model, different findings

A cross-model review (Gemini-4 argon over MASO, given the diff and the repo's
own rules, without reading the first review first) found 10 items the first
reviewer missed — including a blocking defect and two high ones. All were fixed
and merged; the counts at HEAD are one blocking, two high, four medium, three
low, all closed except the documented product gaps below.

- **Blocking (E):** the live work log froze on the first event of a streaming
  turn. `ChatTurnWorking.vue` read the newest turn through a `computed` that
  returned the *same* `TimelineTurn` object, and `IncrementalTimelineReducer`
  mutates turns in place while returning a fresh array — so Vue saw an
  unchanged value and never re-ran `step`/`counts`. A 20-minute turn showed
  `Working · Thinking` and an empty counts line for its whole duration.
  `workLog.test.mjs` (pure functions) and the browser harness (static
  pre-populated turns) could not see it. Now the projections read `turns.value`
  directly, and there is a streaming test plus a harness case that appends
  events after mount; both fail if the old computed returns.
- **High (F):** a `plan_first` Goal could start implementation without approval
  — the Hub cleared `plan_pending` and trusted the model to end the planning
  turn with `needs_input`, so a model answering `continue`, or an objective
  edit during the planning turn, dispatched turn 2 with the "the user approved
  this plan" continuation prompt. The pause is now owned by the Hub, not the
  model: any turn that produced an unanswered plan parks unless the Goal is
  complete, and an edited objective re-plans instead of implementing. Approval
  is also refused when no plan was parsed.
- **High (A):** the inbox read `AgentStreamStore("terminal-tabs",
  "terminal-tab-<id>")` for every tab, but a Workspace Chat persists its stream
  under `(workspace_id, managed_session_id)`. Every agent row in the `Agents`
  scope therefore had no excerpt, no reply time, no `activity_since`, and could
  never be `is_unread` — so it could never enter "Needs you" on a finished
  unread reply. The store is now resolved per tab, and the managed snapshot
  carries `activity_since` like the tab one.
- **Medium:** evidence chips inverted their documented label precedence
  (a named MR link rendered as `MR 42`); the approval card built `id` /
  `aria-describedby` tokens out of raw option labels, so any multi-word label
  broke the screen-reader association with the option's tradeoff; Ask aside
  during a running turn forked the *incomplete* turn, leaving spinning tool
  cards in the fork forever; a coarse-pointer tap on the diff `+` opened the
  editor twice and Shift-click ranges could invert (`45-42`) or miss the
  existing note.
- **Low:** the review jump bar's 0.2 target-ratio threshold could never fire
  for a tall Decision section; a mobile inbox open skipped the immediate
  refresh and moved focus off the keyboard surface, a blocked row lost its
  timestamp fallback, and the excerpt's timestamp was naive local time; a
  multi-thread launch failure double-counted, and a trailing `<goal-plan>`
  envelope was not stripped when only trailing tags were inspected.

Cross-model caveat worth recording: this reviewer's *findings* were accurate
(all three top ones were re-verified in code before fixing), but the commit
hashes it cited for the branch history do not exist in this repository, so
treat its bookkeeping as unreliable and its code claims as the evidence.

## Merging into main (merge commit `1dc2c1e`)

`main` had moved on (native sub-agent dock, Feishu bot work, the AI-native
maintenance refactor), so the branch needed a real three-way merge. Five paths
conflicted and eight needed a decision:

| Path | Decision |
| --- | --- |
| `CHANGELOG.md` | Both Released entries kept, main's newer first |
| `scripts/verify_browser_mocks.py` | Union of both new runners |
| `frontend/src/features/chat/StructuredPane.vue` | Union of both import groups; main's native sub-agent dock block kept beside the review provide |
| `backend/claude_hub/services/managed_chat.py` | Union of both import sets |
| `backend/tests/test_managed_chat_runtime.py` | Both appended tests kept, once each |
| `backend/claude_hub/services/agent_stream/store.py` | `import json` restored (main's import block dropped it; our legacy-JSONL reader still calls it) |
| `frontend/tests/browser_agent_inbox.py`, `browser_work_log.py` | Formatted for the repository formatter |

The record that binds these to the merge SHA lives in
`docs/working-logs/2026-11-09-ui-upgrade-merge-decisions.json`
(`./scripts/verify.sh merge-decisions --merge 1dc2c1e` reports complete coverage).

Validation on the merged tree: frontend 1450 tests, backend 6513 passed / 10
skipped, mypy clean over 626 files, frontend lint/types/build clean, browser
scenarios 22 plus main's new one. `tests/test_ci_health.py` fails 6/11 on
pristine `main` as well, so it is excluded from the merged-backend result.
