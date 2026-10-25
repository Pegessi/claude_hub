# Dot as a secretary on Feishu

Branch `codex/dot-secretary`, checkout `~/claude_hub_worktree/dot-secretary`.
Design: [docs/DOT.md](../DOT.md) ("Delegation and wake-up").

## Problems the user reported

1. Feishu replies were long plain prose, not using Feishu styles.
2. Dot thought and worked at length, then answered with a wall of text; the
   user wants a fast secretary between them and their agents.
3. After Dot sent work out, nothing woke it to report back on Feishu, where
   the user mostly talks to Dot.
4. Agent results were relayed in raw form.

## What was found

- The Feishu reply was the turn's whole assistant text: every progress note
  between tool calls ("I'll read the report first…") came before the answer.
- Wakes worked for tasks Dot started with `dot delegate` (the journal shows
  notices sent on Feishu). The audits in the user's screenshot were sent to
  model-pinned codex sessions Dot built itself, not as tracked tasks, so their
  end woke nothing. `dot delegate` could not choose a session or model.

## What changed

- Feishu replies use `final_text` (the text after the turn's last tool call)
  from the native Claude consumer; the web keeps the whole turn.
- `dot notify --title --tone` sends a card (JSON 2.0, coloured header, Markdown
  body); a refused card is resent as a post.
- `dot delegate --session <id>` runs the task in that workspace agent session,
  as its agent. Tasks whose source is the Dot tab are watched automatically.
- Hub-kept turn rules: work like a secretary, start agent work with
  `dot delegate`, put the whole answer in the last message, conclusion first,
  relay results in plain words. The wake message and `Reach:` line say that a
  task the user asked for that is done, failed or needs them goes to Feishu as
  a card. The previous wake text is retired so schedules get the new one.

## Cross-review fixes

- `--session` checked only the workspace: a non-orchestrator or a Chat-kind
  agent failed at start and left a `todo` task. Now the session must be a
  workspace orchestrator, and the task takes its session kind.
- Only the Dot Chat gets the last answer on Feishu; another bound Chat that
  ends with "Done." after writing a report would have lost the report.
- The Claude adapter never marked a Task sub-agent's records, so their text
  joined the parent's answer (the journal showed English sub-agent notes after
  `[quiet]`). Records with `parent_tool_use_id` now carry `subagent_thread`.
- Not fixed: a task started from the Dot Chat that ends between two watcher
  passes (under a minute) is not watched.

## Follow-up: a newer Feishu message interrupts

The user saw a Feishu message become a follow-up instead of interrupting. Only
Dot's own wake/tidy turns gave way; a message during Dot's answer to an earlier
Feishu message waited in the Feishu queue. Now, on the Dot Chat, a newer
message in the same chat marks the active batch superseded and stops its turn
(`manager.cancel_turn` with its turn id); the stopped batch ends as
`superseded` without a failure reply, and the next batch's text carries the
previous message and the new ones. If the stop fails (the answer ended first),
the flags are undone and the message follows as before. Not covered: a Feishu
message during a web turn on the Dot tab, and web messages (the web composer
still queues behind a user turn).

Review fixes: the reply is skipped only when the stop is confirmed (the
batch awaits the stop task), so a turn that failed on its own still gets the
failure reply; an answer that finished before the stop took is sent and the
next batch is a plain follow-up (no "stopped" claim); the carried text is set
by the stop task only when it stopped, so a late revert cannot wipe a newer
interrupt; the carried text includes the omitted-image note. Not covered: if
the newer message is withdrawn before it is sent (e.g. the binding changes),
the stopped message gets no answer.

Second review fixes: every exit of a batch (web-interaction reply, rejection,
shutdown) settles a pending stop and drops its carried text, so a stop that
lands late cannot make the next batch claim the earlier answer was stopped;
the carried text is the user's own words (no nested intros) plus a note when
the stopped message had images, which are not re-sent.

## Activation

The live Dot keeps its session pack until the session rotates (`claude-hub dot
rotate`, or the normal turn/age limits). The user's `DOT.md` is not rewritten;
the secretary wording lives in the Hub-kept turn rules. A backend restart is
needed for the code changes.

## Not covered

- Other providers (Codex app server, Cursor) still return their whole text to
  Feishu.
- Replies to Feishu messages stay posts; only Dot's notices are cards.
