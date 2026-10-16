# Dot — the persistent personal agent

Dot is one long-lived Claude Chat tab with a stable role and a file-based
memory. You talk to it casually. It answers small things from memory, hands
real work to Hub Tasks, and keeps track of what matters.

The design goal is a *clean mind*: Dot never carries days of raw history.
Every turn it gets a small, fresh context pack built from durable memory.

## Why (the failure we avoid)

A master agent in one endless conversation drifts. It forgets which task
matters most, re-checks workers, drowns in detail, and writes vague briefs.
Dot fixes this with structure, not a longer prompt:

| Problem | Dot mechanism |
| --- | --- |
| Forgets priorities | `focus.json`: at most 7 active items, shown every turn |
| Micromanages workers | Delegation contract: brief once, then wake only on task events |
| Drowns in detail | Session rotation + per-turn context pack with a size cap |
| Vague briefs | Brief template with a done-when checklist + a brief check before sending |
| Same mistakes again | Review every returned task; a brief miss becomes a lesson rule |
| Lost results | Finished tasks become result cards in memory automatically |
| Memory rot | Nightly tidy turn + deterministic `dot health` |

## Memory home

`<runtime home>/dot/` (override with `CLAUDE_HUB_DOT_HOME`). It is a git
repo; every synced turn commits, so memory has history and can be rolled back.

```
DOT.md              role and rules (user-editable)
focus.json          active focus list (<= 7)
work.json           delegated Hub Tasks Dot tracks
inbox.json          task events not yet shown to Dot
lessons.json        rules for briefs learned from reviews (votes, last seen)
state.json          tab id, session rotation state, sync cursor
memory/<kind>/*.md  cards: user | work | knowledge | concept
journal/YYYY-MM-DD.md   raw talk log (append-only)
files/              stored attachments (images, audio, code, docs)
index.sqlite        derived FTS5 search index (not in git)
```

Markdown cards are the source of truth. The index is rebuilt lazily from
file mtime and size, so Dot may also edit card files directly. A broken index
file (not a database, or a damaged page found on any read) is dropped and
rebuilt. A file changed in the last two
seconds is read again on the next search, since a second quick edit can keep
the same mtime.

### Compression levels

Like human memory, detail fades into concepts:

1. **Raw** — journal entries and stored files.
2. **Cards** — one fact, decision, result or file description with a one-line
   `summary`.
3. **Concepts** — short cards that name an idea and `links` to the cards
   behind it.

Only summaries and concepts enter the context pack; Dot opens a full card or
raw file when it needs detail.

### Card format

```
---
id: "k-paper-notes-1a2b3c"
kind: "knowledge"
title: "Paper notes: FlashAttention 3"
summary: "Key ideas and numbers from FA3."
tags: ["paper", "attention"]
importance: 3
links: []
files: ["files/1a2b3c4d.pdf"]
created: "2026-10-15T10:00:00+00:00"
updated: "2026-10-15T10:00:00+00:00"
archived: false
---
Body text.
```

Values are JSON; a plain unquoted value is read as a string. The folder is
the card's kind, even when a card was moved by hand without editing `kind`.

## Turn flow

1. Hub sends a user turn to the Dot tab.
2. `ClaudeNativeSession` asks the Dot service to prepare the turn:
   - decide rotation (turn count, age, explicit request, `[Dot tidy]`);
   - build the **session pack** (role, user profile, concepts, delegation
     rules from lessons, tool guide) —
     passed via `--append-system-prompt`, stable for the whole session;
   - build the **turn pack** (time, focus, open work, tasks waiting for
     review, new inbox events, cards recalled by the user's words (not on
     Hub wake-ups; common words like "the" and a bare "hi" do not count; a locked
     or broken index only loses the recall), and on the first
     turn the recent talk) — prepended in the hidden
     `<claude_hub_instructions>` block.
     Focus and work have their own caps (the oldest work is cut) so reviews
     and events still fit; reviews come before events so a busy inbox cannot
     push them out; a section that does not fit is skipped, not the ones after
     it. A cut recent talk keeps the latest exchanges.
3. After the turn completes, a post-persist observer appends the exchange to
   the journal and commits the Dot home. Inbox events in the pack are marked
   read when the provider takes the turn; if the turn then fails, they are
   unread again, and a failed first turn makes the next turn open a fresh
   session (so the recent talk is shown again).

## Delegation and wake-up

`claude-hub dot delegate` builds a brief from the template, registers and
starts a Hub Task, and records it in `work.json` (before it starts, so a task
that fails to start is still tracked; retry it with the same
`--request-key`). `claude-hub dot watch <task_id>` (or the Work tab in the
panel) adds a Hub Task you started yourself;
`dot unwatch` stops tracking it (the panel asks first when a review is
waiting, since the review is dropped). Watching a watched task again keeps its
place (unseen reports still raise events). Watching a task Dot delegated keeps it a
delegated task (only `--stall-hours` changes), so its review is not lost. A
background watcher checks tracked tasks every 60 seconds; one task that cannot
be read does not stop the others. When one finishes, fails, needs input (the
worker or the Hub reviewer asks), or looks stuck it:

- writes an inbox event,
- writes a result card from the worker's latest report (not the Hub
  reviewer's; each part cut at 20,000 characters). The Hub's own "please review" reports (a task moved to
  review by hand, a review asked again, the stuck-review reaper) carry no
  result, so they neither fill the card, start a new round, nor count as a
  question. A new return (ready for review, completed, failed) is the result,
  even with progress posted after it; with no new return the card shows the
  worker's newest report since the task was last continued (the person's
  continue message and the Hub's audit lines are not the worker's). A failed card also says why the Hub failed it
  (session died, timeout). A result is announced once: the Hub reviewer
  taking the task back to working and returning it, or a person accepting
  (even again) a result that was not a failure, is not news; nor is the
  result a task had when it was watched (a task watched while working with
  only progress is news when it is done). A task a person sent back that
  returns with no new return is asked for review again, and a re-run that
  fails again (or a move to failed by hand) is a new failure. A reviewer
  question is its own event, even in the poll that announces the result or
  with worker progress after it,
- runs the standing "Dot wake-up" Chat schedule now (queued behind any
  active turn; repeated wakes coalesce). The schedule has a ten-year interval
  so it never fires on its own; events are the only way it runs. A deleted
  schedule is made again; a new Dot tab takes both schedules and turns them on.

An event is written before the task's new state, with a key, so a failed
write is retried without a second copy of the event.

So Dot wakes often when many tasks move and rarely when few do. On a wake it
tells the user only what needs them. When nothing does, it replies exactly
`[quiet]`; that turn is hidden from the chat and kept out of the journal.

## Brief, review, lesson

One loop makes briefs better over time:

```
brief ──▶ worker ──▶ result card ──▶ review ──▶ lesson ──┐
  ▲                                                      │
  └──────── in every session, before the next brief ◀────┘
```

1. **Brief.** `--done-when` is a checklist (repeat the flag, at most 7):
   each item must be something a stranger can check (a file, a command, a
   number, a behaviour). `--output` says what the result is and where it
   goes; `--out-of-scope` says what not to touch. The worker must answer every
   check by number, `1. ✓ <evidence>` or `1. ✗ <why not>`.
2. **Check before sending.** Plain code, no model call. The task is held
   (`held: true`, CLI exit 1) when a check is vague (`done`, `works`, `完成`…),
   there are more than 7 checks, or output or context is missing. Dot fixes
   the brief and sends again, or adds `--confirm`; overridden warnings are kept
   on the work item. A retry with a known `--request-key` is not held again
   and does not overwrite the tracked status, so a result that came back in
   between still raises its event. Lessons never hold a brief: word overlap
   cannot tell whether the brief already follows a rule. They are in the
   session pack, so Dot sees them while writing the brief, not after sending.
3. **Result card.** The done-when list is printed next to the worker's report,
   so each check can be held against its answer.
4. **Review.** Every task Dot delegated stays under "Waiting for your review"
   in each turn pack until Dot runs
   `claude-hub dot review <task> --verdict ok|redo|skip [--cause brief|env|worker|change]`.
   A redo needs a cause; `change` means the user changed what they want. A
   `brief` cause needs `--lesson "<rule>"` (new) or `--lesson-id <id>` (the
   same miss again: one more vote). Any other redo needs `--note` on what went
   wrong, so blaming the environment, the worker or the user is never free.
   `skip` (with `--note`) is for a result Dot cannot judge; it clears the
   reminder and counts nowhere. A new rule that says nearly the same as a
   live lesson votes that lesson instead. Watched tasks are not reviewed;
   their brief was not Dot's. A redo is only a record: Dot then sends the fix
   (`claude-hub task continue <task> --message ...` while the task is in
   review or failed, or a new delegate). After a verdict, the next round asks
   again: a new final report (even if the watcher never saw the task working
   again; before a verdict it refreshes the result card) or a closed task
   that was restarted. A closed task keeps the time it ended
   (`failed_at` / `completed_at`); a new time means it ran again and ended the
   same way between two polls, which also asks again. This is one in-memory
   compare per closed task, no report read.
5. **Lessons.** One rule each, "When <situation>, <what to put in the brief>",
   at most 200 characters. Score = strength, halved every 60 days without a
   new vote. A vote or merge first decays the old strength to today, then adds,
   so an old rule with many stale votes cannot jump ahead of fresh ones. One
   task votes a lesson once; a vote for a merged lesson goes to the lesson it
   was merged into, and a vote for a dropped one is refused. The top 8 with
   score ≥ 0.5 are in every session pack ("always", at most 200 characters
   each); the rest are hidden but kept as history. The nightly tidy
   merges duplicates and sharpens wording (`dot lessons merge|edit|drop`). The
   user can edit them in the Lessons tab.

The first-pass rate (ok at the first verdict, no question asked and no later
redo, over the latest 20 reviews) is the one number that says whether briefs
are improving. A question counts even when it was answered before the
watcher looked; an ok that a later round turned into a redo (not `change`)
is not a first pass. Tasks whose first review said `change`, and skipped
ones, are left out. `dot lessons` and the Lessons tab show it with the
first-round causes (so brief misses stand apart from env and worker ones)
and "judged N/M returned": a rate built on few verdicts says little. The CLI
also shows how briefs sent past brief warnings (`--confirm`) did: if they
pass as often as the rest, the check is holding briefs for nothing.

A task looks stuck when it is queued or working with no progress (task update,
progress report, runtime status change or new report) for `--stall-hours`
(default 2). It alarms once per stall; new progress resets it.

Dot does not poll workers. It only reacts to these events. There is no daily
summary; ask Dot when you want one.

A second schedule runs the nightly `[Dot tidy]` turn in a fresh session:
compress yesterday's journal into cards and concepts, merge duplicates,
prune focus. It also replies `[quiet]` unless something needs the user.
Its turn pack has no inbox events: they stay unread for a turn that can act
on them. A tidy turn that fails does not count as a tidy in `dot health`. "Mark read" in the panel marks only the events on screen.

The JSON files are hand-editable. One that is not valid JSON (or not the
expected list or object) is renamed to `<name>.corrupt-<time>` before Dot
starts a fresh one, so the next write cannot erase it; stray values inside a
list and odd numbers are skipped, not fatal.

## Interfaces

- CLI: `claude-hub dot status|recall|remember|forget|show|focus|work|delegate|review|lessons|watch|unwatch|inbox|identity|rotate|health|setup`
- REST: `/api/dot/*`
- UI: Dot button in the header opens the Dot Chat; the Dot panel shows focus,
  work, lessons, memory and health. On phones the button sits in the tab bar and the panel is a
  full-screen sheet with touch-sized controls.

## Name and avatar

`identity.json` in the Dot home holds the name (max 40 chars), an emoji or
short text avatar, and an optional picture (PNG, JPEG, GIF or WebP, max 2 MB;
no SVG). Change them with Edit in the panel or `claude-hub dot identity
--name ... --avatar ... --avatar-image <path>`. A new name renames the Dot tab
and rotates the session, so the next turn is told its new name.

Setup is safe to repeat. It takes the provider env (API endpoint, key, model)
like a normal new Chat: the UI borrows it from your active Claude tab (else the
default Claude preset); `claude-hub dot setup --env-preset <name>` uses a preset.
A Dot tab still on the built-in fallback env gets this env; an env you chose
for the Dot tab is kept. Failed turns are journaled as `(turn failed)`, never
as Dot's words.

Voice (speak / listen) is planned for later.
