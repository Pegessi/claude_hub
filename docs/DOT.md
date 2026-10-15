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
file mtimes, so Dot may also edit card files directly.

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

Values are JSON; a plain unquoted value is read as a string.

## Turn flow

1. Hub sends a user turn to the Dot tab.
2. `ClaudeNativeSession` asks the Dot service to prepare the turn:
   - decide rotation (turn count, age, explicit request, `[Dot tidy]`);
   - build the **session pack** (role, user profile, concepts, tool guide) —
     passed via `--append-system-prompt`, stable for the whole session;
   - build the **turn pack** (time, focus, open work, new inbox events,
     recalled cards, and on the first turn the recent talk) — prepended in the
     hidden `<claude_hub_instructions>` block.
3. After the turn completes, a post-persist observer appends the exchange to
   the journal and commits the Dot home.

## Delegation and wake-up

`claude-hub dot delegate` builds a brief from the template, registers and
starts a Hub Task, and records it in `work.json`. `claude-hub dot watch
<task_id>` (or the Work tab in the panel) adds a Hub Task you started yourself;
`dot unwatch` stops tracking it. A background watcher checks tracked tasks
every 60 seconds. When one finishes, fails, needs input, or looks stuck it:

- writes an inbox event,
- writes a result card from the latest report,
- runs the standing "Dot wake-up" Chat schedule now (queued behind any
  active turn; repeated wakes coalesce). The schedule has a ten-year interval
  so it never fires on its own; events are the only way it runs.

So Dot wakes often when many tasks move and rarely when few do. On a wake it
tells the user only what needs them. When nothing does, it replies exactly
`[quiet]`; that turn is hidden from the chat and kept out of the journal.

## Brief, review, lesson

One loop makes briefs better over time:

```
brief ──▶ worker ──▶ result card ──▶ review ──▶ lesson ──┐
  ▲                                                      │
  └──────────── shown when the next brief is written ◀───┘
```

1. **Brief.** `--done-when` is a checklist (repeat the flag, at most 7):
   each item must be something a stranger can check (a file, a command, a
   number, a behaviour). `--output` says what the result is and where it
   goes; `--out-of-scope` says what not to touch. The worker must answer every
   check by number, `1. ✓ <evidence>` or `1. ✗ <why not>`.
2. **Check before sending.** Plain code, no model call. The task is held
   (`held: true`, CLI exit 1) when a check is vague (`done`, `works`, `完成`…),
   there are more than 7 checks, output or context is missing, or a past lesson
   shares words with the brief. Dot fixes the brief and sends again, or adds
   `--confirm`; overridden warnings are kept on the work item.
3. **Result card.** The done-when list is printed next to the worker's report,
   so each check can be held against its answer.
4. **Review.** Every task Dot delegated stays under "Waiting for your review"
   in each turn pack until Dot runs
   `claude-hub dot review <task> --verdict ok|redo [--cause brief|env|worker]`.
   A redo needs a cause. A `brief` cause needs `--lesson "<rule>"` (new) or
   `--lesson-id <id>` (the same miss again: one more vote). Watched tasks are
   not reviewed; their brief was not Dot's.
5. **Lessons.** Score = votes, halved every 60 days without a new vote. The
   top 5 with score ≥ 0.5 are in every session pack ("always"); weaker ones
   only hold a matching brief ("on match"); below 0.1 they are faded but kept
   as history. The nightly tidy merges duplicates and sharpens wording
   (`dot lessons merge|edit|drop`). The user can edit them in the Lessons tab.

The first-pass rate (ok at the first verdict and no question asked, over the
latest 20 reviews) is the one number that says whether briefs are improving.
`dot lessons` and the Lessons tab show it with the miss causes.

A task looks stuck when it is queued or working with no progress (task update,
progress report, runtime status change or new report) for `--stall-hours`
(default 2). It alarms once per stall; new progress resets it.

Dot does not poll workers. It only reacts to these events. There is no daily
summary; ask Dot when you want one.

A second schedule runs the nightly `[Dot tidy]` turn in a fresh session:
compress yesterday's journal into cards and concepts, merge duplicates,
prune focus. It also replies `[quiet]` unless something needs the user.

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
