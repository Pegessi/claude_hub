# Feishu topic mention routing

## Scope

This change lets a user who already paired a Feishu Bot in a private
conversation invoke that same Bot from a group topic by mentioning it. It does
not introduce group-based pairing, implicit group authorization, standalone
file events or interactive-card callbacks.

Development uses `~/claude_hub_worktree/feishu-topic-mention` on
`codex/feishu-topic-mention`. Investigation of the deployed `mac_mini` service
was read-only; the task did not restart, reconfigure or deploy that service.

## Observed incident and root cause

At `2026-10-13 11:38:45 +0800`, the deployed backend logged the same group
event reaching both configured Bot WebSocket connections. Each route then
reported `Only Feishu p2p messages are supported`. Feishu delivery was healthy;
the local normalizer rejected every `chat_type=group` event before mention
selection, binding resolution or Chat dispatch.

## Design

- The authenticated Bot profile now retains the official `open_id` alongside
  its name and avatar. New and rotated credentials persist it immediately. An
  older entry that has presentation metadata but no `open_id` refreshes the
  profile before its first group event. Environment Bot identity is cleared
  together when its App ID changes.
- Group ingress fails closed unless `mentions[].id.open_id` exactly matches the
  current Bot. This prevents every Bot connected to the same group event from
  responding. Plain input removes the matching mention placeholder; rich `post`
  input removes only the current Bot's `at` node and preserves mentions of
  other people.
- Authorization still comes from the Bot's existing private-chat binding. A
  group event routes only when its sender matches that binding, and group text
  that resembles a pairing code is ignored. The group `chat_id` is never
  persisted as pairing authority.
- The normalized event carries `chat_type` and `thread_id`. Replies to topic
  events set Feishu's `reply_in_thread` flag, while existing private replies keep
  their previous request shape. Coalescing additionally requires the same
  `chat_id` and `thread_id`, and follow-up state uses that same context identity,
  so two topics targeting one Chat become separate ordered turns without being
  described as continuations of each other. Text mention placeholders are
  matched longest-first so keys such as `@_user_1` cannot corrupt
  `@_user_10`.

## Verification

- Focused group-topic, rich-post and reply-protocol regressions passed. They
  cover exact Bot selection, mention stripping, private-binding routing, pairing
  rejection, legacy identity refresh, rejection of an unpaired group sender,
  provider-visible text, reply-in-thread payloads, mention-key prefix collisions
  and separation of different topics.
- Independent review found and the implementation fixes two P2 edge cases:
  prefix-colliding plain-text mention keys and tab-wide follow-up state leaking
  across topics. Final test counts and repository verification are recorded in
  the task handoff.

## Remaining limits

- Only existing supported `text` and rich `post` messages enter the group path.
  Standalone files, audio/video and cards remain unsupported.
- A sender must first pair the Bot from its private conversation. Other group
  participants are ignored until they establish their own authorized binding.
- The deployed `mac_mini` instance is unchanged, so the fix is not live until a
  later authorized merge and deployment/restart.
