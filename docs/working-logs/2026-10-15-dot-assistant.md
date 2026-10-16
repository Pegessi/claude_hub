# Dot persistent personal agent (M1–M3)

Branch `codex/dot-assistant`, checkout `~/claude_hub_worktree/dot-assistant`.
Design: [docs/DOT.md](../DOT.md). Voice (M4) is deferred by the user.

## Goal

A Jarvis-like agent that answers simple things from memory, delegates real work
to Hub Tasks, and keeps its own context small. It must avoid the long-running
master-agent failures seen with MASO: losing priorities, micromanaging workers,
drowning in detail, and imprecise briefs.

## What shipped

- M1: git-backed memory home (`<hub home>/dot`): cards with JSON frontmatter,
  focus (max 7), work, inbox, journal, files and an FTS5 trigram index. A stable
  session pack goes in the system prompt; a small turn pack goes in the hidden
  hub block. Sessions rotate on turn count, age, tidy or request.
- M2: `delegate` starts a Hub Task from a fixed brief template; a 15 s watcher
  turns task transitions into result cards and inbox events and runs the "Dot
  wake-up" Chat schedule. Dot never polls workers.
- M3: nightly `[Dot tidy]` schedule, deterministic health checks, header
  launcher and Dot panel (focus, work, memory, health), REST and CLI.

## Problems found during a real run

On an isolated backend (`CLAUDE_HUB_HOME=~/.claude_hub_dev/dot-assistant`,
tmux socket `dotdev`, port 18731) the first real turn failed with 401.

1. Dot's tab was created without provider env, so it fell back to the built-in
   Claude env, which has no key. Fix: `POST /api/dot/setup` takes `env` /
   `env_preset`; a new tab is created with it, and an existing tab still on
   the empty or built-in fallback env is switched through the normal
   `/api/tabs/{id}/switch-env` path, so a live Chat transport gets the new env
   too. A user-chosen env is kept. The launcher borrows the env of the active
   Claude tab (else the default Claude preset); the CLI uses `--env-preset` or
   the CLI's Claude default preset.
2. The journal recorded the 401 error text as Dot's answer. Fix: failed turns
   are journaled as `hub (turn failed)`.

After the fixes, a real turn on the fallback env failed as expected; setup
seeded the env, and the next turn answered "Your top focus is P1: Ship Dot."
from memory and was journaled. One retry was needed for a provider network
timeout unrelated to Dot.

## Round 3: watch, stuck alarm, name and avatar, phone UI

- Watcher interval 15 s -> 60 s. `dot watch <task_id>` tracks an existing Hub
  Task; `dot unwatch` stops. A queued/working task with no activity for
  `stall_hours` (default 2) gets one `task_stalled` inbox event and wakes Dot;
  activity is the latest of task update, progress report, runtime observation,
  report time and watch time. No daily summary (user's choice).
- `identity.json` stores name, emoji/text avatar and a picture under `files/`
  (raster only, sniffed by magic bytes, max 2 MB). A rename renames the tab and
  rotates the session; the session pack tells Dot its name.
- Phone (<= 768 px): compact launcher in the mobile tab bar, full-screen panel
  with safe-area padding, 16 px inputs (no iOS zoom) and 40-44 px targets.
- Checked at 390x844 and desktop in headless Chromium: rename to "Jarvis" with
  a fox emoji updated the header, launcher and tab; watching an unknown id shows
  "Task not found". Found and fixed: the tab bar kept the old name until reload,
  so the panel now refreshes tabs after a rename.

## Cross review fixes

A high-effort review of the branch found these; each has a regression test
that fails on the old code:

- Inbox events were marked read when the pack was built, even if the budget
  cut them or the send failed. Now only events shown in the pack are acked,
  and only after the provider accepts the turn. Over 12 events, older ones
  wait for a later turn.
- A failed or skipped wake was never retried. The watcher now retries each
  tick while unread events remain.
- Updating an archived card un-archived it. Updates keep `archived`.
- `/api/dot/files` served any stored file inline (HTML/SVG would run on the
  Hub origin) and `files/../` reached the whole Dot home. Now paths must stay
  in `files/`; only PNG/JPEG/GIF/WebP render inline, the rest download with
  `nosniff`.
- Two setups at once could create two Dot tabs. `ensure_tab` is locked, and
  the state cache now keys on inode + mtime_ns + size (same-tick writes were
  read stale).
- A `</claude_hub_instructions>` inside a report or card ended the hidden
  block early and leaked the pack into the visible message. The marker is
  broken inside injected context.
- `prepare_turn` could overwrite a concurrent rotation request; it now runs
  under the store lock. Commits use their own git lock so slow git does not
  block JSON reads and writes.

The user kept solo mode for the Dot tab (permissions skipped) so unattended
wake and tidy turns can run tools.

## Test coverage

- Backend: 47 Dot tests; line coverage of `services/dot`, `api/dot.py` and
  `cli/commands/dot.py` is 92% (88% before). Added schedule creation and
  re-pointing, the plain `done` path, delegate route/CLI errors, journal and
  `dot show`. What is left uncovered is mostly CLI text output and rare
  error branches.
- Browser: `frontend/tests/browser_dot_panel.py` (in `browser-mocks`) runs the
  real SPA with mocked APIs at 390x844, then 1280x900: compact launcher in
  the tab bar, full-width panel, 16px inputs, rename + emoji (PATCH body, tab
  refresh), stalled badge, watch error and success with a 4h alarm, Stop,
  Mark read, then the desktop launcher with the new name. It found two small
  issues, now fixed: the phone launcher buttons were 34px tall (now 38px),
  and the close button label still said "Dot" after a rename.

## Design review against the original goal

A separate reviewer checked the branch against the user's first messages.
Verdict: mostly meets it (fresh packs, focus, delivered-once events, memory
in git). The user decided on the gaps:

- The wake-up schedule had a 09:03 daily cron, which fired a turn every day
  with no events. It now has a ten-year interval, so only watcher events run
  it. `ensure_schedules` moves old homes off the cron. Frequency now follows
  the work: many task events, many wakes; few events, few wakes.
- "Report only when needed": wake and tidy turns answer exactly `[quiet]`
  when nothing needs the user. The chat hides those turns and the journal
  skips them, so they no longer push real talk out of the recent-talk window.
- No size-based rotation. Providers already compact a long context; rotation
  exists to stop drift, not overflow. Added a test for the 18h age rule.
- Brief quality: first a prompt-only self-check plus `Brief lesson` cards.
  Replaced in round 4 (below).
- Still open, by choice: "never poll workers" is prompt-only; nothing pushes
  a non-quiet wake reply to the user outside the Dot tab.

## Round 4: brief, review, lesson loop (2026-10-16)

The user asked for a cleaner answer to "briefs and failure lessons". Reading
on agent feedback loops (Anthropic on agents and context engineering; OpenAI
on agent guides and evals; Reflexion / ExpeL-style lesson memory) gave three
points: the checklist is the contract, a lesson needs a verdict behind it,
and rules must fade or the prompt fills with stale advice.

- `done_when` is a numbered checklist; the brief adds Output and Out of scope;
  the worker answers each check by number with ✓/✗ and evidence. The result
  card puts the checklist next to the report.
- Delegate gate (`lessons.lint_brief` + `match_lessons`, plain code): vague
  checks, more than 7 checks, no output or no context hold the task;
  `--confirm` sends anyway and the warnings stay on the work item. (A matching
  lesson held it too at first; see the review below.)
- `dot review` records ok/redo and the cause (brief/env/worker). Only a brief
  miss writes a lesson (new rule or a vote on an old one), so lessons always
  have a verdict behind them. Pending reviews are listed in every turn pack.
- `lessons.json`: votes with a 60-day half-life. Top 5 (score ≥ 0.5) go into
  the session pack; weaker ones are listed for a matching brief; faded ones
  stay as history. The nightly tidy merges and sharpens them.
- First-pass rate (ok at first verdict, no question asked, last 20 reviews) in
  `dot lessons` and the new Lessons tab, where rules can be added, edited
  and dropped.
- Tests: `test_dot_lessons.py` (lint, match, decay, store, gate, the whole
  review loop with a second round), API/CLI routes, node API cases, and the
  browser run now covers the Lessons tab.

### Review of round 4

Two reviewers (code and design) read the round-4 commit. Fixed, each with a
test that fails on the old code:

- A lesson match held a clean brief on every send, and fixing the brief could
  not clear it, so only `--confirm` worked. Matches are now listed, not held;
  only lint warnings hold. A retry with a known request key is not held.
- A vote for a merged lesson revived it as a duplicate; it now goes to the
  lesson it was merged into. Merging into a dropped lesson is refused.
- One task could vote a lesson twice (second round). Now once per task.
- Votes and merges added raw votes, so an old faded rule could become the
  strongest. Strength is decayed to today first (`weight`).
- `lessons --all` counted dropped lessons for "always", pushing live ones out.
  The API now returns `reach`, so the UI no longer re-derives it from a
  rounded score.
- A full inbox could push "Waiting for your review" out of the turn pack.
  Reviews now come before events, and a section that does not fit is skipped
  instead of ending the pack.
- A question answered before the next poll was missed (only the latest report
  was read); every new report is now scanned. A redo that came straight back
  to `review`, or a closed task that was restarted, never asked for a new
  verdict; both do now.
- Smaller: an unknown lesson id in a review is 404, not 400; the rule limit is
  200 characters everywhere (it was 300 stored, 240 shown).

From the design review, also taken: a `change` cause (the user changed what
they want: no lesson, not in the rate), a new rule that nearly copies a live
lesson votes it instead, and stats show first-round causes and "judged N/M
returned". Left for later: parsing the worker's ✓/✗ answers in code and
logging a reason for each `--confirm`. Not fixed by choice: Japanese, Korean
and Cyrillic checks are not tokenized.

Follow-up: a failed task that was re-run and failed again between two polls
(never seen running) did not ask again. `execution_epoch` only moves on
handoff, but `failed_at` / `completed_at` are reset on every dispatch and set
on every end, so a closed item stores that time as `ended_at`; a different
time reopens the review. Items closed before this change only get the time
filled in, with no event.

### Review of round 5

A second code and design review read the round-4 commits. Fixed, each with a
test that fails on the old code:

- Watching a task again after `unwatch` left it `stopped`: a restart was
  missed and it could never be reviewed. `watch` now clears `stopped`.
- One bad number in a hand-edited `lessons.json` (`weight: "high"`, NaN,
  `tasks: null`) broke every Dot turn. Bad values now count as votes, else 0.
- A vote, direct or through a merged lesson, brought back a lesson the user
  dropped. It is now refused (400).
- A `--request-key` retry overwrote the tracked status; when the task had come
  back since the last poll, its result event and card were lost.
- A second return before any verdict kept the first round's result card.
- Saving a done task as done again moved `completed_at`, which Dot now reads as
  a re-run. The Hub keeps the first completion time.
- The Lessons inputs allowed 300 characters; the API takes 200.

From the design review:

- Lesson matches arrived only after the task was sent, so they could not
  change the brief. The match tier is gone; the top 8 lessons (was 5) are in
  every session pack. `reach` is now always, hidden, dropped or merged.
- An ok that a later round turned into a redo (not `change`) still counted as
  a first pass (`late_redo` now).
- Blaming env, worker or change was free: such a redo now needs `--note`.
- `skip` (with a note) clears a result Dot cannot judge; it counts nowhere.
- Stats show how briefs sent past warnings (`--confirm`) did, to test whether
  the brief check earns its holds.
- The tool guide says how to act on a redo (`task continue` or a new delegate).
- Reviews of watched or stopped tasks are refused.

Not taken: a code-computed ✓/✗ summary on the result card, and an example task
per lesson in the UI.

### Review of round 6

Two more reviewers (code paths and failure modes) read the round-5 commits.
Fixed, each with a test that fails on the old code:

- Watching a task Dot delegated made it "watched", so its pending review could
  never be cleared. Watching now keeps a delegated task delegated (only the
  stall limit changes), and watched tasks are never listed for review.
- The Hub's own READY_FOR_REVIEW reports (manual move to review, "Human
  requested reviewer checks.") fired a second `task_ready` and replaced the
  worker's report on the result card. They are no longer a new round, and the
  card shows what the worker said last.
- One task whose reports could not be read, or one huge report (card body
  over 200,000 characters), stopped the watcher for every task. Each task is
  now checked on its own, and card parts are cut at 20,000 characters.
- A Hub reviewer's question (`review_needs_input`) raised no event. It does
  now; it does not count as a worker question for the first-pass rate.
- The nightly tidy turn consumed inbox events (marked read, no one told).
  Tidy packs carry no events now.
- A bad hand edit of a JSON file read as empty, and the next write erased
  it. The file is now kept as `<name>.corrupt-<time>`. Non-object entries,
  a focus priority like `"high"`, a read event without an id, a huge integer
  weight, `importance: 1e999` and a card that is not UTF-8 no longer crash.
- "Mark read" in the panel acked every unread event, including ones that
  arrived after the list was shown. It sends the shown ids now.
- A NUL byte or an over-long name in a file path was a 500; it is a 404 (or
  400 on a card). A blank focus title is 400, and an edit cannot blank it.

### Review of round 7

One reviewer attacked the round-6 commit, one the less-reviewed parts (turn
flow, index, store, API, panel). Fixed, each with a test that fails on the
old code:

- The stuck-review reaper's report (any copied state, even `needs_input`)
  was taken as a new round and as a worker question. All Hub-written review
  requests are now recognised. A worker return followed by a Hub request in
  the same poll was lost; the newest non-Hub report now decides.
- If a write failed after the inbox event was written, the next tick wrote
  the event again. Events are now written last.
- A JSON file that could not be read (a directory, no permission) read as
  empty, and the next write could replace it. Readers still get the default;
  writers now fail.
- Events of a turn that failed after the provider took it were lost. They
  are unread again, and a failed first turn re-opens a fresh session.
- Two card saves within the mtime resolution left the search index stale.
- U+2028, U+0085, form feed and similar characters split a card's lines and
  changed it on the next save; only `\n` ends a line now.
- Re-activating a closed focus item could make 8 active items.
- A card id or link too long for the filesystem was a 500.
- Panel: an older search answer could replace a newer one, overlapping
  actions cleared the loading state early, and the card count stayed stale
  after archive.

Not changed: the journal sync counts stream lines on each completed turn
(same pattern as the rest of the stream store; sessions rotate).

### Review of round 8

One reviewer attacked the round-7 commit, one the seams with the rest of the
Hub (schedules, delegate, CLI, hooks for other tabs). Fixed, each with a test
that fails on the old code:

- A turn the provider never took ends with its start time, so it was not
  matched and a failed first turn did not re-open the session. A turn is now
  matched within its window (its start to the next start). Only events a
  turn really delivered are given back, so a later turn's read is kept.
- Events written last (round 7) could be lost when the event write failed.
  Events go first again, with a key: the retry finds the same unread event
  instead of adding a second one.
- Deleting the Dot tab turned its schedules off for good; setup moved them
  to the new tab but left them off. Moving them turns them on.
- A deleted wake-up schedule left Dot asleep until a restart; a wake now
  makes it again. A failed setup half-way could make a second wake-up
  schedule. A tidy time the user picked is no longer reset on each start.
- A delegated task that failed to start was a 500 and not tracked, so a
  retry made a second task. It is tracked from creation; the error is a 400
  naming the task.
- `dot identity --name X --avatar-image <bad path>` renamed Dot, then failed.

Not changed: other tabs see only no-op hooks; reviewers found no regression.

### Review of round 9

One reviewer attacked the round-8 commit (with 800 randomized turn
simulations: no invariant broke), one the least-reviewed parts (review,
stats, packs, index, health). Fixed, each with a test that fails on the old
code:

- A schedule turned back on kept its old next-run time, so a new Dot tab
  could get a nightly tidy at once. Its time is now sent again.
- `dot identity` with a file that is not a picture still renamed Dot. The
  picture now goes first.
- A journal file cut off mid-character stopped every new session from
  getting its context; it is read with the bad bytes replaced.
- A garbled `index.sqlite` broke every turn pack until deleted by hand; it
  is now dropped and rebuilt.
- `journal/` or `files/` removed while the server ran stopped the journal
  until a restart; writes make the folder again.
- Common words ("what", "the") recalled nearly every card, and every wake-up
  recalled cards for its fixed text. Both are gone.

### Review of round 10

One reviewer attacked the round-9 commit, one the panel and its API
contract. Fixed, each with a test that fails on the old code:

- An index with a good header but a damaged page opened fine and failed on
  the first read, so every turn lost its whole context pack. Any read that
  finds a damaged index now drops it and rebuilds.
- A double Enter on Add (focus or lesson) added the item twice; the draft is
  now cleared before the request and put back if it fails.
- Times under a minute read "just now ago".
- A 422 (for example a search over 2000 characters) showed only "Dot request
  failed (422)"; the message is shown and the search box is capped.
- Stop on a delegated task waiting for review dropped the review in one tap;
  it now asks first.

Left as is: opening the panel before Dot is set up creates the (empty) Dot
home, which setup would create anyway.

### Review of round 11

One reviewer attacked the round-10 commit and the CLI against the API, one
the Hub hooks (native session, watcher start/stop, setup, shared files).
Non-Dot tabs: no behaviour change found. Fixed, each with a test that fails
on the old code:

- A card linking to a card later deleted (`forget --hard`) could not be
  edited at all; only new links and files are checked now.
- An image-only turn right after a restart ran without Dot's role; it now
  loads the stored session pack.
- Setup with the tab list full gave a 500; it is 429 like a normal new tab.

Left as is: a `state.json` broken by hand makes the tab a plain Chat until
setup (which then makes a new tab); the `[quiet]` filter would also hide a
non-Dot turn whose user text starts with `[Dot ` and whose reply is exactly
`[quiet]`.

### Review of round 12

One reviewer attacked the round-11 commit and the store; one checked the
service against DOT.md claim by claim, with randomized tests for lesson
scores, first-pass stats, pack caps and stall alarms (all held). Fixed, each
with a test that fails on the old code:

- When the Hub reviewer reported in the same poll as the worker, the result
  card showed the reviewer's message, and after a redo the new round was
  missed. Only worker reports count as a result or a return now.
- The Journal view gave a 500 on a day file cut off mid-character.
- A card id sent with spaces around it moved the card to a file no lookup
  could find. The id is trimmed, and a bad id is refused.
- A card file with a byte order mark (some editors add one) vanished.
- A `state.json` nested very deep raised in every Chat turn's Dot check.
- "hi" or "ok" recalled cards that only matched as substrings ("this").

### Review of round 13

One reviewer attacked the round-12 watcher change with a random simulator of
worker, Hub reviewer and person steps (3000 runs); one checked the turn path.
Fixed, each with a test that fails on the old code:

- A Hub reviewer question that came in the same poll as the result was
  hidden (only the result was announced).
- The Hub reviewer's steps (to working and back to review) announced the same
  result again and asked for a second verdict; so did a person accepting it.
- The first turn after a lost index filled it while holding the store lock,
  so every Dot route waited seconds. The index now fills before the lock.

The simulator found 0 anomalies after the fix.

## Risks

- The built-in default Claude preset has no key. A user with no working Claude
  tab must pick an env for the Dot tab once (normal tab env switcher).
- The memory quality depends on the nightly tidy turn following its prompt;
  health checks catch structural drift, not bad summaries.
