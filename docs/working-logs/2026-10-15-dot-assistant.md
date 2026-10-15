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

Not changed (decision for the user): the Dot tab runs in solo mode
(permissions skipped) so unattended wake and tidy turns can run tools.

## Risks

- The built-in default Claude preset has no key. A user with no working Claude
  tab must pick an env for the Dot tab once (normal tab env switcher).
- The memory quality depends on the nightly tidy turn following its prompt;
  health checks catch structural drift, not bad summaries.
