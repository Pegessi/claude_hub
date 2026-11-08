# Feishu: group bindings, topic replies and topic follow-ups (2026-11-08)

Branch `codex/feishu-group-topic`. Follow-up to
`2026-10-13-feishu-topic-mentions.md` (which let a privately paired person use the
Bot in a group topic) and to the MASO Feishu research in
`~/claude_hub_workspace/maso-feishu-src/NOTES.md`.

## Request

> 飞书接入可以尽可能丰富一下功能，比如话题回复，群聊信息回复（可以用这个群聊测试
> oc_96b5583877ba45d59bba8ab75c095358）… 调研一下 maso 的飞书机器人相关的功能和代码，取舍一下

So: research MASO's Feishu bot, take what fits this product, and deliver richer
group/topic behavior; the named group (`包家军`, `chat_mode=topic`, 1 human + 4
bots including this Hub's Bot "Ted") is the live test target.

## What already existed (verified, not assumed)

The 2026-10-13 work is real: group ingress needs an exact `mentions[].id.open_id`
match, the sender must already hold a **private** pairing, replies to events with
`thread_id` already set `reply_in_thread`, coalescing/follow-up state is keyed by
`(bot_id, pairing_id, chat_id, thread_id)`, and turn metadata already carried
`chat_type` + `thread_id`. What was missing is everything about the *group* as an
entity, plus the two topic behaviours below.

## MASO comparison and the trade-offs taken

Recovered MASO's real source (the binary is a shiv zipapp; `extract_shiv.py`
extracts it) and read the whole `maso_cowork/feishu` package for v0.7.3 and v0.8.1.

Ported:

| MASO behaviour | Here |
| --- | --- |
| A group is a conversation shared by its members, not a per-user private channel (`master-feishu-group-{chat_id}`) | **Group binding**: Bot ↔ group `chat_id` ↔ Chat tab. Any member's @mention routes. |
| Topic-mode detection: honour `thread_id` only when the chat is provably a topic group (`chat_mode='topic'` **or** `group_message_type='thread'`, 600 s TTL). MASO's own note: honouring every `thread_id` in an ordinary group caused the v0.6.0 "infinite sessions" bug | Same detection, used only to decide the **reply shape** (this product routes one Chat, so it cannot fork sessions). |
| `reply_to_owned_bot_message` activation: a reply inside a topic the Bot started needs no new @mention (ownership proven from ids the Bot sent) | **Topic follow-ups**: a bounded per-Bot registry (`feishu_sent_messages.py`) of the message ids **and topics** this Bot answered, consulted only for a group message that does not mention the Bot. Tracking the topic matters because in a 话题 group a follow-up carries the human's root id, not ours. |
| Reply always anchors on the inbound `message_id` (never posts fresh into a group) | Unchanged, and kept for every new path. |

Deliberately not ported:

- **Per-topic sessions** (`isolation=per_topic`): a Chat is the unit of work here;
  forking a Chat per thread would duplicate tabs and need new lifecycle rules.
- **`/mode respond|observe|ignore` group policy**: `observe`/`ignore` are meaningful
  with MASO's shared-context channel; here a group either addresses the Bot
  (mention / topic follow-up) or it does not. Adding modes invites a bot that
  answers every message in a room.
- **Thread-history injection** (`list_thread_messages`, watermark): the Chat
  timeline already holds the conversation; re-injecting it would duplicate text.
- **Cards/execution streams, `/group` `/pair` commands, meeting bot, relay
  connector, `ask_user_question`**: MASO-infrastructure-bound or already covered
  by this product's own cards/commands.
- MASO's **fail-open mention check** and **absent outbound `uuid`**: this product
  keeps exact-Bot mention matching and its existing dedupe; both MASO shortcuts
  are fanout/duplication risks.

## Changed

Backend:

- `BotBinding` gains `chat_type` (`p2p`|`group`) and a display-only `chat_name`;
  persisted bindings without the field stay `p2p` (no state-version bump, and an
  unknown value is a hard state error).
- `bind_group` / `routed_binding_for_chat` in the pool store: a group binding is
  addressed by `chat_id`, carries no sender authority, and is never reachable
  through the private-chat or per-sender routes. Occupancy, tab-exclusivity and
  revision CAS are the same rules as pairing.
- `FeishuBotClient.chat_info()` and `list_chats()` over the Bot's own token, plus
  `reply_message`/`reply_text` now returning the sent message id.
- `GET /api/feishu/bot/bots/{id}/chats` lists the groups the Bot can see;
  `POST /api/feishu/bot/bots/{id}/bindings/group` binds one after asking Feishu
  for it (a private chat id is refused with `chat_not_group`, an unreadable one
  with `chat_not_visible`).
- `_route_binding`: p2p keeps the exact sender+chat rule; group prefers the
  group binding and falls back to the sender's private pairing.
- `_group_is_topic_mode` (600 s cache with a 256-entry bound, failures not
  cached) decides the reply shape: a thread id or a topic-mode chat promotes the
  reply into the topic; otherwise the request shape is byte-identical to before.
- `sent_messages` registry (bounded, 500 entries per Bot) records the message id
  and the topic of every reply; `message_event_from_sdk` accepts a group message
  without a mention **only** when it replies to one of those messages or belongs
  to one of those topics. The bucket is released when the Bot is unbound or
  deleted.
- `FeishuChatInfo.is_group` reads both signals the chat-info API really returns
  (`chat_mode` group/topic, and `chat_type` private/public for visibility); a
  partial record (no form information at all) means the Bot is not in the group
  and is reported as `chat_not_visible`.
- Question cards stay private: mirroring is disabled for a group binding, so a
  group never shows a card nobody there may answer.
- Turn metadata already carried `chat_type`/`thread_id`; nothing new was needed
  there.

Frontend:

- The binding panel offers two explicit paths in the unbound state — "Private
  chat" (the unchanged pairing-code flow) and "Group chat" (pick a group, read
  "Anyone in this group can talk to this Chat.", bind). The bound view shows
  `Target: Private chat` or `Group · <name>` with "Replies stay inside the group
  topic." for topic groups.
- Provenance chips parse `chat_type`/`thread_id` and append `· group` /
  `· group topic`; private messages look exactly as before.

## Review findings applied

A separate review pass found no blocking design problem but did find two real
defects and several weak spots; all are fixed here:

- `bind_group` wrote `chat_id`/`chat_name` and then validated them on read, so an
  over-long group name could persist a pool that fails to load forever. Values are
  validated before the write, and a store test proves the pool still reads.
- The group bind route held the per-Bot gate across a Feishu round-trip; the read
  now happens before the gate and the store re-checks occupancy/revision.
- A group binding could mirror question cards into the group where no member is
  allowed to answer; mirroring is now disabled for group bindings.
- The group path could submit a group chosen for a different Bot after a Bot
  switch; the selection is dropped on Bot change or list change.
- The `chat_not_group` diagnosis was used for a partial chat record, which really
  means "the Bot is not in that group"; that case is now `chat_not_visible`.
- `invalid_bot_request` no longer claims to be a group-bind error on every route.

## Verification

- Backend: `656` Feishu tests pass (122 in `test_feishu_bot_pool.py`, including 18
  new group/topic cases — routing, topic promotion, thread follow-ups, per-Bot
  id scoping, probe failure, DTO and endpoint errors — plus 9 new cases in
  `test_feishu_group_binding_store.py` for persistence, routing separation,
  occupancy/revision, chat-info semantics, the legacy `chat_type` default and an
  over-long-name write that must not corrupt the pool). 14 previously-green tests
  initially failed because the reply funnel started passing `reply_in_thread`
  unconditionally; the flag is now only passed when it is true, restoring the old
  call shape.
- `mypy` clean on the touched modules; `black`/`isort` clean; full backend suite
  run separately (see the commit).
- Frontend: `typecheck`, `lint:check`, `test:unit` (1255 pass, +20 new cases),
  `build` clean.
- Browser mocks (own Vite on 5292, Playwright 1.58.0 on Python 3.11.16, all APIs
  mocked): `browser_feishu_bot_components.py` and `browser_feishu_provenance.py`
  PASS, the latter including the new `· group topic` badge case. Screenshots in
  `~/claude_hub_workspace/ui-review-shots-fg/`.
- Visual check of the new path switch and group picker (uncommitted helper
  `ui-review-shots-fg/group_path_check.py`): `03-group-selected.png` shows
  "Private chat | Group chat", the group select ("包家军 · topic group") and the
  warning.
- Live Feishu (read-only plus one recalled message): the Bot's own token can read
  `GET /im/v1/chats/oc_96b5…` (`chat_mode=topic`, 1 user + 4 bots) and
  `GET /im/v1/chats` (the Bot sees `Ateams` and `包家军`), so the group picker's
  data path is real. A smoke text was posted into the group and recalled
  immediately (`code 0` for both), proving the Bot's send path; group history
  also shows Feishu mints a distinct `thread_id` per top-level message there.
- **Not yet verified live:** a human @mention in the group, and a follow-up in
  the topic the Bot answered. The only human there is the account this session
  cannot post as (`lark-cli` user identity lacks `im:message.send_as_user`), so
  one typed line in the group is the remaining gate. For a *promoted root* topic
  the topic id comes only from Feishu's reply response (`data.thread_id`); if
  Feishu ever omits it, only quote-replies to our message would activate.

## Risks

- Binding a Bot to a group hands that Chat to **everyone** in the group. That is
  the feature, and the UI says so, but it is a real authority change: an
  existing private pairing is unaffected, and the reversed direction (group →
  private) is not offered.
- Group question cards and `/answer` remain p2p-only: a group binding is never
  mirrored a card (it would be unanswerable there), and a `/answer` typed in a
  group is still swallowed rather than answered.
- `push_to_tab` now lands in the bound group chat automatically, but always as a
  plain group message — never inside a topic.
- Group-bound Bots are topic-wide once they answer: any member's message inside
  a topic the Bot already answered activates it without a mention. Routing still
  needs a binding for that chat, but this is wider than a quote-reply rule and is
  the deliberate "the thread is the proof" reading of MASO's behaviour.
- Coalescing still groups by `(bot, chat, thread)` only, so two members writing
  in the same topic within the quiet window become one turn whose single reply
  anchors on the first message. Left open deliberately; widening the key per
  sender is a separate behavioural change.
- The topic hint on the composer falls back to `chat_mode` for responses from
  servers that do not report `is_topic_mode`; the group list carries the
  resolved flag.
- `sent_messages` is per process: after a Hub restart, one follow-up in an old
  topic needs a fresh @mention (the registry starts empty and only learns ids
  from replies it sends itself).
- `/chats` returns group names/counts to any authorized Hub user, while
  `pairing_view` hides another owner's `chat_id`. That matches the pool's shared
  administration model (any authorized user may bind/release) and is accepted
  deliberately rather than overlooked.
- The topic hint needs the server's `is_topic_mode`, which `/chats` now reports;
  the composer-side hint still falls back to `chat_mode` for older responses.
