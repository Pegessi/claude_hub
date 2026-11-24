# Feishu /ai busy reaction and reply routing

By default, /ai adds `OnIt` to the command message and posts one answer or
error reply. It then removes `OnIt`. Enable **Send progress reply** in Feishu
settings or use `claude-hub feishu ai-command configure --send-interim-reply`
to restore the intermediate message. Use `--no-send-interim-reply` to disable it.

An existing configuration with no `send_interim_reply` field uses the new
default. An absent `done_emoji` now defaults to empty. Previously saved
`done_emoji: "DONE"` is indistinguishable from an intentional choice, so the
stored value still applies. Clear **Done reaction** in Feishu settings or run
`claude-hub feishu ai-command configure --done-emoji ''` to remove it.

A command that will open a new topic uses a message-scoped entry in
`channel_tabs` until a reply identifies the topic. Independent root commands
can dispatch concurrently. A chat-level lock spans final delivery and
promotion of the entry to the returned topic, so a follow-up arriving while
the receipt is outstanding can reuse the session. Commands in established
topics retain the existing per-topic execution lock.

The pending command retains its tab, turn ID, reply mode and busy-reaction
ID, along with the execution settings used to parse the command. Recovery reuses the
final reply UUID and can obtain a completed answer from durable stream
events. The poller gives recovered topic creation priority over queued
follow-ups in the same chat. The recorded run follows reaction cleanup, so
an exit before cleanup leaves the pending turn available for recovery.

The busy-reaction ID is saved before context fetch. A retry before dispatch
reuses the saved reaction. When interrupted dispatch cannot be safely
replayed, the poller retains a cleanup-only entry and retries deleting that
reaction without dispatching the command again.

Cleanup-only entries allow three retry attempts with a 30-second delay.
They use two worker slots reserved for reaction cleanup and are excluded
from the command intake limit. Exhaustion logs the affected message ID
and removes the cleanup entry.
