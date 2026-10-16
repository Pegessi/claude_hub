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

## Risks

- The built-in default Claude preset has no key. A user with no working Claude
  tab must pick an env for the Dot tab once (normal tab env switcher).
- The memory quality depends on the nightly tidy turn following its prompt;
  health checks catch structural drift, not bad summaries.
