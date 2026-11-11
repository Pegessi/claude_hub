# Feishu serving modes: one Chat per channel (2026-11-09)

Branch `codex/feishu-channels-integration` (built from three parallel branches:
`codex/feishu-channels-model`, `codex/feishu-channels-dispatch`,
`codex/feishu-channels-ui`). Follow-up to
`2026-11-08-feishu-group-topic.md`.

## Request

> 同一会话双渠道，甚至是 N 渠道… 以及可能 BOT 也有很多用户私聊它。
> 要不就弄 P1+P3，后面再说 P2?

So: make the serving mode explicit at bind time, and let one Bot serve many
conversations — each with its own Chat — without doing the P2 "several rooms
sharing one Chat" behaviour by default. P2's isolation guarantees were built in
parallel so the shared mode can be switched on later without rework.

## The model

| Mode | Who gets a Chat | Binding |
| --- | --- | --- |
| `single` (default, unchanged) | this Chat only | private pairing code, or "Bind this group" to a chosen Chat |
| `per_channel` | every conversation: one DM person, or one group, each with its own Chat (max 8) | DMs are provisioned on first message; a group is bound to a new Chat |

- Pool state version 4: `bindings` (list) replaces the single `binding` slot;
  a v3 file migrates to `[binding]` with `mode="single"`. `BotEntry.binding`
  stays as the first-element projection so dispatch, push and older clients keep
  working, and the admin DTO reports `mode`, `bindings` **and** `binding`.
- Invariants kept: one channel (`chat_id`) at most once per Bot and once
  pool-wide; one tab bound at most once pool-wide (now scanned across every
  Bot's list); `single` allows one binding, `per_channel` up to
  `MAX_CHANNELS_PER_BOT = 8`; generation/revision CAS unchanged; releasing one
  binding is now per `pairing_id`.
- Mode switching is explicit: `PATCH /bots/{id}` takes `mode` + CAS and refuses
  (409 `bot_has_active_bindings`) while any channel is bound.
- Auto-provisioning (`services/feishu_auto_chat.py`): a DM from an unknown person
  in `per_channel` mode creates a Chat tab (`Feishu · DM ou…abcd`) and binds that
  channel; limits are one p2p channel per sender, 8 per Bot, and a 5 s minimum
  interval between creations. Any refusal is answered with one short sentence
  instead of silence.
- Group binding in `per_channel` omits `tab_id`: the server creates a new Chat
  named `Feishu · <group name>` (Feishu's chat lookup still happens before the
  bot gate), truncating an over-long group name instead of failing.
- Those tabs are created inside the per-Bot gate with a 15 s bound, and every
  creation is rolled back (tab deleted, no binding) if the bind then fails.
- An unbound group mention in `per_channel` is answered once with "this group is
  not bound yet", and the sender's private channel is **not** used: the legacy
  group→private fallback applies only in `single` mode.

## P2 groundwork (isolated dispatch per channel)

Several conversations can already reach one Chat today (a group's members,
several topics, and soon several channels). Those now stay separated:

- **Channel identity** = `(bot_id, binding.pairing_id, event.chat_type,
  event.chat_id, event.thread_id, generation, transport_revision)` — channel +
  **authority watermark** — driven by one `_channel_matches` used by both the batch
  builder and the supersede decision. `event.chat_id` is in the key because the
  legacy `single`-mode group fallback resolves a group message to the sender's
  **p2p** binding; `generation` covers control-plane changes and
  `transport_revision` covers credential/callback epochs. Administrative
  `revision` is deliberately absent: a rename or a sibling channel write must not
  split a live conversation, while a credential change must.
- **Batch/coalesce key** adds `event.sender_open_id`: two people in one topic no
  longer merge into a single turn. **Interruption** deliberately omits the sender,
  so a second person in the same topic may supersede, while another room or topic
  can neither stop nor inherit it. `interrupted_text` carryover is bounded and
  keyed by the full channel key, so A1 → B1 → A2 neither leaks nor gets lost.
- **Provider text v3** (`provider_text_format: "feishu-v3"`) adds
  `chat_type`, `thread_id` and `chat_name` (the name only when the binding really
  is the event's chat) to the v2 payload; it describes the **event's** room, so a
  group message is never labelled with the DM chat. v1/v2 output is untouched.

## Frontend

- Bot settings gained a serving-mode control (`This Bot serves one Chat` /
  `Everyone who talks to this Bot gets their own Chat`), disabled with an
  explanation while channels are bound, with the 409 reconciled into readable text.
- The binding panel reads `bindings[]` (falling back to `binding`) and shows
  `Current channels N / 8` with `Group · <name>` / `DM · <id>` rows, each with its
  Chat and its own **Release** action (a per-`pairing_id` release; the bulk
  disconnect would drop every channel, so it is not offered as a per-channel
  control); the group path says it creates a new Chat and explains the cap when
  all 8 are used.

## Verification

- Backend: `692` Feishu tests pass on the integrated branch (665 on the model
  branch, 681 + 2 expected v3-pin failures on the dispatch branch). Full backend
  suite `6224 passed, 9 skipped, 3 failed` — the same three
  environment/timing failures reproduce on unmodified `main`
  (`test_filesystem::test_list_defaults_to_home_when_no_path`,
  `test_native_goal_transport::test_stop_claims_start_at_wait_for_consumer_handoff`
  ×2).
- `mypy` clean over 391 backend modules; `black`/`isort` clean over `claude_hub`
  and `tests`. Note: the repo's own tool venv pins Python 3.11 while this host's
  venv is 3.12, so `mypy --config-file pyproject.toml` alone stops on a numpy stub;
  the same invocation with `--python-version 3.12` is clean.
- Frontend: `typecheck`, `lint:check`, `test:unit` (1275 pass), `build` clean.
- Browser mocks (own Vite on 5293, Playwright 1.58.0 / Python 3.11.16, all APIs
  mocked): `browser_feishu_bot_components.py` and `browser_feishu_provenance.py`
  PASS. Panel screenshots: single mode unchanged, `per_channel` shows
  `Current channels 2 / 8` with a group and a DM row.
- **Not verified live:** a real human message. The group path needs one @mention
  in `包家军`; the DM path needs one direct message from a human account (this
  session cannot post as the user: `lark-cli`'s user identity lacks
  `im:message.send_as_user`). Neither the auto-provisioned tab nor the v3 prompt
  has been observed in production.

## Fix rounds after the merge (2026-11-10/11)

The first merge of this feature was reviewed post-hoc and found unsound in four
rounds of independent probing; the fixes live on `codex/feishu-modes-fixes`. What
each round changed, so a reader can judge the current code rather than the
merged-then-fixed history:

1. `6f1fb6e` — carryover survived a pre-handoff retirement (pop moved after the
   admission guard, idle queue cleanup no longer drops it); write validation
   extended to owner/claim fields before `_save`; the `binding` + `bindings: []`
   legacy record no longer loses its binding; non-first-channel card/Dot lookups
   resolve by `tab_id`/`pairing_id`.
2. `cd9bad4` — undid the "suppress `revision`" shortcut and split the concerns:
   every client-visible write advances the CAS `revision` again, and delivery
   freshness became its own predicate (`is_current_for_delivery`); the empty-batch
   `UnboundLocalError` was fixed.
3. `b5b42e6` — question cards carry an explicit transport snapshot
   (`QuestionMirror.tab_id` + `transport_digest`, bot `transport_revision`) and
   every post-token send/patch gate re-validates it; `DispatchChannelKey` dropped
   the administrative `revision`.
4. `1f824c7` / `5f167fe` / `7eeadf6` — `transport_revision` is validated on read
   (invalid values are state errors, missing values get a conservative baseline),
   legacy mirrors fall back to **strict revision equality** instead of a wildcard
   digest, the queue key is delimited by the authority watermark (a credential
   epoch separates batches, an administrative rename does not), the branch was
   merged onto main's SQLite document store, and finally
   `is_current_for_delivery` compares `transport_revision` so an A→B→A rotation
   cannot deliver a stale answer.

The historical card incident is **a lost card plus a stalled external question
for channels 2..N — not a card transmitted to another person's DM**: the tab guard
ran before any HTTP client was constructed.

## Risks

- `per_channel` creates Chat tabs from inbound messages. The cap (8/Bot), the
  per-sender rule and the 5 s cooldown bound it, but the cooldown lives in process
  memory (a restart clears it, and it is not shared across processes), and there
  is no idle reap yet: a Chat created for a stranger stays until someone archives
  or releases it, and releasing a channel does not delete its tab.
- The auto-created tab is named by the sender `open_id` (`Feishu · DM ou…abcd`,
  where `ou…abcd` is an abbreviated id — the Bot has no display name without
  another scope); the panel row shows the channel's `chat_id` (`oc…`) instead, so
  the two identifiers differ by design.
- Mode switching requires zero bound channels, so moving an existing single
  binding to `per_channel` is: release → switch → re-bind. Deliberate; a silent
  migration would change who can reach which Chat.
- P2's shared-room mode (`shared`: several channels into one Chat) is **not**
  exposed yet. Its dispatch isolation is in place, but the admin model still caps
  `single` at one binding, so nothing can turn it on by accident.
- The v3 room identity intentionally reads the event, not the binding; anything
  that later relies on `binding.chat_id` meaning "the room" must use
  `event.chat_id` instead.
