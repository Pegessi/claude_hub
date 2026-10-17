# Dot: the user comes first

Branch `codex/dot-user-first`, checkout `~/claude_hub_worktree/dot-assistant`.
Design: [docs/DOT.md](../DOT.md) ("The user comes first").

## Problem

Tried on another machine: a Dot wake-up turn ran many tool calls (checking a
returned task by itself). The Dot Chat is also bound to a Feishu bot, so for
that whole time a Feishu message got "busy, not run" and a web message was
queued in the browser. The user wants the main session to stay free so their
messages are answered at once.

## Options weighed

- Run wake-ups somewhere else (a Hub Task or a second Chat). Rejected: Dot's
  memory and judgement live in the one Dot session; a second worker would need
  its own context, and its findings would still have to come back to Dot.
- Native background sub-agents in the Dot turn. Not possible: the Claude Chat
  runs one process per turn, and output after `result` is dropped.
- Chosen: Dot's own turns give way to the user, and they are told to stay
  short.

## What changed

- Backend: a user message on the Dot tab (web or Feishu, both through
  `_dispatch_tab_stream_input`) stops a running wake-up or tidy turn (matched
  by its scheduled run's turn id) and is sent at once. `DotService.yield_turn`
  gives the wake-up's events back (once: the turn's record forgets them, so its
  later end cannot give them back after the next wake-up read them) and asks
  for a new wake-up after a three-minute pause. A stopped turn counts like a
  failed one (events unread, no tidy stamp) and stays out of the journal.
  Before the stop, Dot's queued wake-up/tidy runs are cancelled
  (`drop_pending_scheduled_task_runs`, no drain): the stopped turn's end drains
  the scheduler, and a queued run would otherwise take the Chat before the
  user's message (409 busy).
- Only wake-ups mark events read. A user turn shows waiting events and
  reviews under "waiting for your next wake-up", so it does not take them over.
- Prompts: a Hub-kept "Stay free for the user" section in the session pack
  (the role file is the user's, so new rules cannot go there for existing
  homes); the wake-up message asks for a short turn and to judge from the
  result card instead of rerunning checks. A wake-up schedule with the earlier
  Hub words gets the new ones.
- Frontend: the turn start of a Dot system turn has `yields_to_user`; the web
  composer then sends instead of queueing (`isComposerHeldByTurn`). The mode
  stays locked until that turn ends.

## Not covered

- A Feishu message during the user's own web turn on the Dot tab still gets
  the busy reply.
- A stopped tidy waits for the next night.
