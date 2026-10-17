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

## Wake-up reports reach Feishu

The user saw no wake-up report on Feishu: the bot only replied to Feishu
messages, so a wake-up's answer stayed in the web Chat. A first try pushed
every non-`[quiet]` wake-up answer; the user wants Dot to decide instead. Now
Dot has `claude-hub dot notify "<text>"` (`POST /api/dot/notify` →
`DotService.notify` → `feishu_bot.push_to_tab`, same gate and binding checks
as a reply, secrets masked). The tool guide says when to use it (a task the
user asked for is done or failed, a blocking decision, a problem; not every
reply), and the wake message points to it.

## Cross-review fixes

- Events left unread by a wake-up that was stopped by hand, failed, or skipped
  had no wake-up until new events came. The watch loop now wakes while the
  inbox has unread events and no wake-up run is queued or running, after the
  pause (a failed or stopped wake-up starts it too, so a failing provider is
  not retried every minute).
- New events no longer cut the pause after a give-way short.
- An error while giving way is logged; the user's message is still sent.
- The test checks that the queued runs are gone before the stop.
- The web "Busy turn follow-ups are durable" hint follows the composer, so it
  is hidden while a Dot turn gives way.

Second review (of the notify and watch-loop change):

- A wake-up that ran again could send the same notice twice: `notify` skips
  text it sent in the last 30 minutes.
- Wake schedules written with the first short-turn words kept them (no
  `dot notify` hint): those words are retired too.
- A wake-up that kept failing at once (e.g. a disabled schedule) was retried
  every minute: it now waits the pause unless new events came.
- The user wants Dot to know the source, and to be told to use Feishu when a
  bot is bound. Each turn pack now has a `Reach:` line (`reach_note`): Feishu
  (the provider text's verified-source intro), web, or a Hub wake-up/tidy;
  where the answer goes; and whether a bot is bound
  (`feishu_bot.feishu_bound_to_tab`). A Hub turn with a bound bot is asked to
  send what needs the user with `dot notify`.

Third review (all low): a repeat now answers `sent: false` with a reason
instead of `sent: true`; events that come while a wake-up runs go right after
it (not after the retry pause); a bot without a valid config does not count as
bound.

## Not covered

- A Feishu message during the user's own web turn on the Dot tab still gets
  the busy reply.
- A stopped tidy waits for the next night.
