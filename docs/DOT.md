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
| Vague briefs | Fixed brief template (goal / context / done-when / constraints / report) |
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
starts a Hub Task, and records it in `work.json`. A background watcher checks
tracked tasks. When one finishes, fails, or needs input it:

- writes an inbox event,
- writes a result card from the latest report,
- runs the standing "Dot wake-up" Chat schedule now (queued behind any
  active turn; repeated wakes coalesce).

Dot does not poll workers. It only reacts to these events.

A second schedule runs the nightly `[Dot tidy]` turn in a fresh session:
compress yesterday's journal into cards and concepts, merge duplicates,
prune focus.

## Interfaces

- CLI: `claude-hub dot status|recall|remember|forget|show|focus|work|delegate|inbox|rotate|health|setup`
- REST: `/api/dot/*`
- UI: Dot button in the header opens the Dot Chat; the Dot panel shows focus,
  work and memory.

Voice (speak / listen) is planned for later.
