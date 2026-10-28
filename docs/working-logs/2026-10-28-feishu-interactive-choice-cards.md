# Design: answer Hub Chat questions from native Feishu cards

Status: design candidate; no product implementation or deployment.
Task: `7bd868c7-ef67-466d-8408-15e8e1778b70`.
Investigated 2026-10-28; base `0211661a1e627349822b3857ecf74f33b81818ef`.
All repository paths below refer to that immutable base unless explicitly marked
**proposed**. This document is also the focused working log for this design.

## Decision and acceptance boundary

Mirror an authoritative, persisted Hub question into the **p2p Feishu chat bound
to its tab**. A single-choice button submits the same `ask_question_response`
that the web card submits. An explicit text reply also becomes that structured
answer, with the original question identity. Never ask the model to infer which
question a reply belongs to. Never execute a schedule/task CLI command taken from
a card value. “Run now” answers Dot's question; Dot then takes whatever action the
existing conversation authorizes, exactly as after a web answer.

Use the existing stream question contract, a shared exact-question admission
service for **both web and Feishu**, and the existing durable inbox coordinator
for answer intent and Claude/Cursor follow-up delivery. Extend its envelope with
a trusted `question_answer` kind rather than treating an answer as an ordinary
queued message. Native blocking answers must bypass the FIFO they are unblocking.
Managed Chat keeps its Workspace mailbox as execution owner; it must not be sent
to the ordinary-tab inbox as a new turn.

Default card transport: a new authenticated HTTPS callback route; retain current
WebSocket message intake. Official docs support card callbacks over long
connections, but Hub pins `lark-oapi==1.5.3`, whose wheel's
`lark_oapi/ws/client.py:262–265` handles EVENT and immediately returns on CARD.
Merely registering a card handler is insufficient. A verified SDK upgrade could
replace the HTTP adapter later without changing question/answer semantics.
This is a design decision within scope, not an SDK patch in this task.

Acceptance means a click or correlated text produces **one answer to one exact
question**, including stale/race/restart handling; it does not mean every provider
uses the same wire operation. Tool permission approvals, plan execution approvals,
Task human acceptance, terminal prompts, and secret questions are not remotely
answerable through this question feature. This does not grant new task authority.

## 1. Observed flows and evidence

### 1.1 Web questions are stream events, not notification Markdown

| Stage | Existing source / behavior |
| --- | --- |
| Claude creation | `services/agent_stream/claude_jsonl.py::_normalize_ask_user_question_args`, `_emit_ask_user_question_approval`: `AskUserQuestion` input becomes `approval_required` with questions `{id,prompt,allow_multiple,options:[{id,label}]}`. Question IDs are index strings; option IDs are labels. |
| Cursor / Codex creation | `cursor_cli_transcript.py` normalizes `AskQuestion`; `providers/questions.py::codex_normalize_questions` and `transcripts/codex_items.py` normalize Codex's `item/tool/requestUserInput` (legacy `tool/requestUserInput`). Native `providers/codex_rpc.py::_handle_server_request` retains the provider JSON-RPC request by request ID and emits a notification carrying its `itemId` as card call identity. |
| Render | `frontend/src/features/chat/timeline/reducer.ts` hides raw question-tool rows and creates approval parts; `components/ChatApprovalCard.vue` renders options and an Other text field. `useQuestionAnswers.ts` keeps selections outside rebuilt timeline objects, requires every question answered, replaces selection with custom text for single select, combines them for multi select. |
| Submit | `controllers/useChatComposer.ts::submitQuestionResponse` calls `formatAskQuestionResponse` in `utils/chatQuestionResponse.ts`: `{"type":"ask_question_response","answers":[{"questionId":"0","selected":["Run now"]}]}`. `useChatDelivery.ts::sendToStream` also sends `question_turn_id`, `question_call_id`, stable `client_turn_id`. |
| Direct tab endpoint | `api/routes/agent_stream/dispatch.py` registers POST `/api/workspaces/tabs/{tab_id}/stream/send`. `dispatch_tab_stream_input` owns Goal admission and dispatches to the native session. **Gap:** the direct-tab path accepts the identity fields in the schema but does not apply the managed path's exact-card checks. Generic `send_message` may match by question IDs alone. Do not copy this weakness into Feishu. |
| Managed endpoint | POST `/api/workspaces/sessions/{managed_session_id}/stream/send`, same module: under `managed_stream_interaction`, uses exact turn/call, `chat_question_answer_calls`, fingerprinted mailbox call ownership; consumes a native question or uses `send_chat_session_message_locked(question_key=…)`. Stale cards receive 409 `stale_question`. |
| Runtime resolution | `services/agent_stream/runtime/input.py::answer_pending_question` validates active native turn, optional expected call, and question coverage, then invokes the transport. `accepts_question_followup` recognizes Claude/Cursor retained cards. `publication.py::_record_approval_card` captures call/turn/run epoch/question IDs; `_emit_approval_resolved` claims before awaiting and persists the original card identity for reload and cross-tab rendering. |

Two provider lifecycles must remain distinct:

* **Codex/TraeX:** blocked provider turn remains in flight. Exact call maps to the
  original server request; `codex_rpc.py::answer_pending_question` pops matching
  requests before awaiting and sends `{answers:{qid:{answers:[labels]}}}` as a
  JSON-RPC **response**, without creating a new model turn. Stop clears requests.
* **Claude/Cursor:** one-shot noninteractive CLI auto-declines the tool using a
  placeholder. `providers/guidance.py::QUESTION_PROTOCOL_GUIDANCE` tells the
  model the question is still pending: stop and await the real follow-up.
  Completion of the original one-shot turn is **not** expiry of this retained
  question. The structured answer is a new turn in the same conversation;
  web chooses steer if the old process is still running. A question-specific
  `approval_resolved` closes the original card, not an unrelated newer card.

Plain composer prose is not automatically a structured answer. Some generic
paths support parsed JSON answers without an exact call. The proposed service
makes the exact identity compulsory for card answers, for web and Feishu alike.
`_pending_approvals` is in memory; persisted historical cards alone do not prove a
live provider request after restart. `is_secret` exists for Codex free-text
questions; those must remain web-only.

### 1.2 Dot notifications, bot binding and inbound Feishu

`cli/commands/dot.py::dot_notify` POSTs `/api/dot/notify` with text/title/tone.
`api/dot.py` delegates to `services/dot/runtime/session.py::notify`: resolve Dot
current tab, mask secrets, suppress identical title+text for 30 minutes in memory,
then `api/feishu_bot.py::push_to_tab`. The latter resolves an enabled/configured
bound pool entry, obtains tenant token outside its bot gate, rechecks effective
configuration and binding inside the gate, then sends. Without binding it returns
false; CLI receives `sent:false`. Failures and unbound currently share a reason.

`services/feishu_message_format.py::build_feishu_card_payload` builds schema 2.0
header + Markdown body, with done/failed/decision/info colors. It has **no option
buttons**. `FeishuBotClient.send_message` POSTs
`https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id`,
`msg_type=interactive`, `content=json.dumps(card)`, using tenant access token.
Refused cards fall back to a post/text. The method currently returns `None` and
discards Feishu's sent `message_id`, so it cannot yet index or update a question.
Text deduplication is not question identity or durable delivery evidence.

Current inbound is **not an HTTP event webhook**: `services/feishu_bot_websocket.py`
starts one SDK connection per effective bot, registering only
`register_p2_im_message_receive_v1`. `_accept_message` reserves bounded routing
work; `api/feishu_bot.py::_handle_sdk_event` parses and validates app identity,
deduplicates by bot/message ID, and calls `_handle_message_event`.
`services/bot_pool/{state,store,common}.py` owns credentials, binding, generation,
revision and pairing. `api/routes/feishu_bot/pool.py` provides
`/api/feishu/bot/bots/{bot_id}/pair/start`, `/pair/activate`, `/pairing` release:
web owner starts pairing, user sends code in p2p, web confirms received word.
Only activated binding routes traffic. It records owner, sender, chat, tab and
pairing identity; authorized owner and effective generation are checked again
before native dispatch. Group mentions have a separate sender-binding path;
this design does not allow group question answers or automatically export p2p
question content into a group.

`api/routes/feishu_bot/message_dispatch.py` and `queueing.py` own current in-memory
per-tab batching/FIFO, typing reactions and stop-for-newer behavior. The bridge
`dispatch_tab_chat_and_wait` in stream `dispatch.py` sends provenance metadata and
`turn_source.py::format_feishu_provider_text_v2` produces
`Claude Hub verified message source (feishu-v2): …`. This header is server-derived
context, **not** callback authentication. The bridge sets
`allow_question_answer=False`. On **any** `approval_required` it currently retires
the exact turn and raises `ExternalInteractionRequired`; the dispatcher tells
the user to use web Chat. Thus adding buttons alone would still cancel the turn
we intend to answer. The design must change that branch for supported questions.

`cli/feishu_cards_impl/{primitives,interactive,contract}.py` already offers
stateless task/plan card builders and `parse_card_action` with
`hub_token`/`hub_action`, plus suggested CLI strings. These helpers are for an
external bot-owning agent, not the built-in bot's callback execution path. Their
parser is permissive, accepts inner dictionaries without authentication, and is
not an authorization boundary. Reuse safe formatting ideas; never run its
`cli_command` or use arbitrary task-accept actions to answer a Chat question.

### 1.3 Durable inbox / unified pump: already merged in this base

The brief's “rv branches awaiting merge” assumption is stale. Read-only
`git log --all --grep='inbox\|tab.message\|delivery core'` and ancestry show:

| Evidence | Observed revision |
| --- | --- |
| Durable API introduction | `2c7a913f8068261b3440680983170f2b99a92f28` |
| Inbox branch tip `codex/tab-message-inbox-20261020` | `2ba9277` |
| Unified core change | `e36c0426c4c79432ce66ae5e1796a88dc24e97a7` |
| Unified branch `codex/tab-message-unify-20261020` | `2b1eb2d` |
| Merge / `codex/tab-message-unify-main-20261020` | `070dcadead4d85d2a132fa7276638b4fe202d128`, ancestor of base/main |

No existing branch was modified. `docs/CHAT_MESSAGES.md`, the two 2026-10-20 inbox
and unification working logs, and actual `models/chat_message.py`,
`services/chat_message_inbox.py`, `api/routes/agent_stream/messages.py` agree:

* Authenticated POST `/api/workspaces/tabs/{tab_id}/messages` accepts strict
  `{message,call_id,source_tab_id?}`; CLI `tab message`, `message-status`,
  `message-cancel` exposes receipts, not completed model answers.
* Atomic/fsynced `chat_messages.json` under runtime home records fingerprinted
  intent, actor, sequence, tab incarnation, deterministic turn ID and states
  queued/waiting/dispatching/accepted/completed/failed/cancelled/uncertain.
  Same actor+target+call ID with same body returns same receipt; conflicts 409.
* One pump per tab, lifecycle redrain every three seconds, per-tab tasks, cold
  runtime readiness, Goal admission, before-send incarnation check, and durable
  `submission_started` before native I/O. Direct uncertain delivery blocks FIFO;
  never automatically replay after possible submission. Terminal stream evidence
  drives recovery; 30-day body expiry retains idempotency tombstones.
* `messages.py::_submit` waits behind busy Chat and active/blocked Goal and passes
  `allow_question_answer=False`. Its target excludes managed Chat. It does not
  route assistant output back to Feishu. Therefore **ordinary enqueue cannot
  answer a pending request**, and would deadlock behind a blocked native turn.
* `scheduling/chat_runs.py` owns UUID5 schedule-occurrence identities,
  superseding only older undispatched occurrences of the same schedule,
  disable/delete policy and legacy run projection. It feeds the **same inbox**.
  Scheduled uncertainty is terminal history and releases its slot, unlike direct
  uncertainty. Preserve this exception; do not apply it to question answers.

Reuse the coordinator's durable identity, admission/fences, readiness and recovery
machinery for answer intents and follow-up turns. Do not reuse its ordinary FIFO
or scheduler uncertainty policy unchanged. Feishu message batching is still
separate from this inbox and must not coalesce or interrupt answer intents.

### 1.4 Missed-run example: observed boundary, not invented scheduler behavior

Bounded search of scheduler, Dot and UI sources for missed/catch-up/补跑/错过
found no deterministic scheduler-owned “run now / skip” question/card constructor.
`dispatcher.py::_fire_scheduled_task` stamps occurrence/next time before effects;
`chat_runs.py` enqueues/supersedes; `registry.py::_compute_next_run` calculates
next times (a past one-shot fires now; cron advances after now). These are not an interactive catch-up policy. Dot schedule messages
and model context can elicit a normal AskUserQuestion. The screenshot/actual
conversation was not provided, so its exact originating turn is unverified.
Recommended default: treat this example as an ordinary **model-created question**
and mirror its stream event. Do not introduce a second timer or synthesize a
pending provider request from `dot notify` Markdown. Future deterministic catch-up
UX must create a typed Hub question and an explicit owner, separately reviewed.

## 2. Proposed question and answer ownership

### 2.1 One exact-answer service, one durable intent owner

Add `services/agent_stream/question_answers.py` as the narrow shared admission
service; move exact-card orchestration from the managed route into it and extend
it to direct tabs. It delegates to existing tailer/provider APIs and the relevant
execution owner. It is not a second provider runner or a second FIFO.

Extend existing inbox persistence to schema version 2, with `questions` and
`answer_intents` alongside existing messages. These records supply cross-channel
claim/recovery that the current volatile pending map and direct inbox lack.
`ChatMessageInbox` remains the atomic storage/dispatch owner for ordinary tabs.
Managed answer intents are recorded here for correlation/deduplication only;
actual follow-up envelope and provider execution remain Workspace-owned.
No standalone Feishu result database or competing question state file.

Proposed question snapshot fields:

```text
question_key = (session_kind, session_id, tab_id, incarnation,
                original_turn_id, run_epoch, call_id)
question_ref = random public 128-bit lookup ID
questions = normalized IDs/prompts/options/multiplicity; snapshot_digest
provider_mode = blocking_native | followup
created_event_sequence; created_at; expires_at
state = open | claimed | resolved | stale | expired | uncertain
answer_intent_id?; owner/session routing metadata
mirror = {bot_id, app_id, tenant_key, pairing_id, binding_generation,
          effective_generation, effective_revision, chat_id, sender_open_id,
          send_uuid, message_id?, send_state, displayed_revision}
action_digest -> {question_ref, operation, question_id, option_id?, expires_at}
```

The tailer remains authoritative for whether that exact provider request can
still be answered. A persisted open snapshot does not resurrect a native RPC.
Only main conversation question events qualify; filter nested `thread_id`,
permission/plan approvals, malformed questions, and `is_secret` before mirroring.
Secret-bearing multi-question cards are entirely web-only, not partially exported.

Extend the publication hook beside the existing observers in
`api/agent_stream.py`. **Current `_notify_post_persist` is completion-oriented**;
adding a callback to its list alone will not observe question creation. Wire
`approval_required` and `approval_resolved` explicitly after stream append and
before fanout. Persist the local snapshot/outbox intent there, without network
I/O; failure leaves web available and mirror disabled, not a fabricated send.
The shared answer service can idempotently initialize a missing snapshot from
that exact persisted event under the same admission lock, closing a race with
web submit. Startup reconciles unanswered persisted events against live pending
state so a crash between stream append and snapshot write loses no new work.
On `approval_required`, upsert by original event identity, resolve the bound bot,
and reserve its outbox send.
On `approval_resolved`, explicit Stop, retirement/reset/delete or invalidating
newer turn, terminalize the question and schedule a card update. Successful
Claude/Cursor completion alone keeps an outstanding follow-up question open;
Codex completion/Stop invalidates its blocking RPC. Restart must reconcile live
exact-card evidence; absent evidence expires it and asks the user to reopen web,
without automatically restarting a provider to recreate the old request.

Question state is a durable projection plus a single answer claim, not authority
to execute a tool. It must be wired to **all** web/card/text answer callers, not
just Feishu, or cross-channel races remain unsolved.

### 2.2 Atomic answer admission and provider dispatch

1. Authenticate callback/message or web owner. Resolve the server-side question
   snapshot; validate full identity, live pending state, binding and expiry.
   Validate complete answers against the snapshot: exact question set, known
   option references or bounded custom text, multiplicity, no attachments.
2. Serialize each tab/session's answers and ordinary input admission with the
   existing Goal/session ownership locks. Lock order: Goal admission → managed
   session interaction lock (when relevant) → short inbox storage lock. Never
   hold the storage lock over provider/network I/O or reacquire a pump lock from
   its own callback. Lifecycle invalidation uses the same admission boundary.
3. Atomically store the answer intent, fingerprint and `open → claimed` owner in
   one inbox file transaction. Assign monotonic ingress sequence and stable
   answer ID. Same ID/body returns the saved receipt; changed body conflicts;
   a competing answer gets `already_answered`/409. Claim reserves the question;
   UI can show “answer queued”, but **resolved** requires the owning execution
   path's delivery evidence. Schema/form validation failures never consume it.
4. For blocking native mode, use a separate answer drain in the **same
   coordinator**, prioritized over ordinary message FIFO. It invokes
   `manager.answer_pending_question(text, expected_turn_id,
   expected_call_id=…)` on the current live request. Never call generic
   `send_message` on a false return. False means stale/rejected, not new text.
   Persist possible submission before the write, record provider-response
   outcome, then publish original `approval_resolved` and update the card.
5. For Claude/Cursor mode, enqueue an internally typed follow-up envelope with
   original turn/call plus answer intent ID in the same atomic transaction.
   It may pass ordinary queued items blocked behind **this question**, but not
   interrupt unrelated active work. Wait for the question's originating process
   to finish; web's explicit steer can still cancel that exact process through
   the shared service. Revalidate just before dispatch. A matching blocked Goal
   answer goes through existing Goal steer/resume policy, not generic inbox Goal
   waiting. Preserve original conversation and authorized Goal ownership.
6. For managed Chat, shared admission validates the live managed incarnation;
   native mode stays in-turn. Follow-up uses deterministic answer call ID with
   `send_chat_session_message_locked(question_key=…)`. Record the inbox claim
   before handoff, retain managed fingerprint/mailbox ownership, then mark handoff
   receipt. On crash between stores, reconcile that exact call against the
   Workspace mailbox; reissue only if definitely absent and not submitted.
   Unknown handoff becomes uncertain; never dispatch a second ordinary turn.

Before native response I/O, extend the transport result contract: today
`_send_jsonrpc_response` swallows broken pipe/reset and `answer_pending_question`
returns true after removing the request. The new service needs proven accepted,
proven not submitted, or uncertain write outcome. Do not report “answered” from
that boolean alone, and do not restore a popped RPC for automatic retry after an
ambiguous write. Stream resolution publication failures require reconciliation,
not replaying a possibly delivered answer.

Ordinary web/Feishu prose input entering after a retained follow-up question must
have explicit behavior: correlated answers use the flow above; unrelated input
is an ordinary turn which atomically invalidates the older follow-up question
before dispatch. Ordinary input cannot start over a blocking native turn. This
prevents an old card from answering a later call reusing question ID `0`.

## 3. Native card payload and free-text UX

Use schema 2.0, existing server formatter sanitization/mention neutralization and
bounds, plus a **new pure question builder** in `services/feishu_message_format.py`.
Do not reuse the schema-1 task-card wrapper or scrape Markdown to invent options.
A single-question, single-select card example (all tokens/IDs are synthetic):

```json
{
  "schema": "2.0",
  "config": {"update_multi": true, "enable_forward": false},
  "header": {
    "title": {"tag": "plain_text", "content": "Missed scheduled run"},
    "template": "orange"
  },
  "body": {"elements": [
    {"tag": "markdown", "content": "Run the missed check now, or skip it?"},
    {"tag": "button", "text": {"tag": "plain_text", "content": "Run now"},
     "type": "primary", "behaviors": [{"type": "callback", "value": {
       "v": 1, "hub_action": "question_answer", "hub_token": "opaque_run_token"
     }}]},
    {"tag": "button", "text": {"tag": "plain_text", "content": "Skip"},
     "type": "default", "behaviors": [{"type": "callback", "value": {
       "v": 1, "hub_action": "question_answer", "hub_token": "opaque_skip_token"
     }}]},
    {"tag": "button", "text": {"tag": "plain_text", "content": "Other / type a reply"},
     "type": "default", "behaviors": [{"type": "callback", "value": {
       "v": 1, "hub_action": "question_text_hint", "hub_token": "opaque_other_token"
     }}]},
    {"tag": "markdown", "content": "Other: reply to this card with your answer, or send /answer Q7K2 your text. Question Q7K2 · expires in 24h."}
  ]}
}
```

POST `/open-apis/im/v1/messages?receive_id_type=chat_id` with
`{"receive_id":"oc_bound","msg_type":"interactive","content":"<JSON-serialized card>","uuid":"<stable-send-UUID>"}`.
Persist UUID before sending and sent `data.message_id` afterwards. UUID (at most 50 characters) is Feishu's
one-hour send dedupe aid, not Hub's permanent idempotency key. Retry within that
one-hour window only; an ambiguous send outside that window is not blindly
resent. Duplicate physical cards, if observed, must share one logical claim.
Bound serialized UTF-8 card content conservatively below the 30 KB message limit;
never silently truncate option IDs or drop questions. Oversize cards remain
web-only with an honest fallback notice and Hub link from configured trusted base.

Button values contain a version, fixed allowlisted operation and a **per-action
random 256-bit opaque token**, never user-controlled tab/turn/schedule IDs or
commands. Store token digests at rest with their snapshot/option mapping; compare
safely. Expiry and binding checks are server-side. This chooses opaque tokens over
signed self-contained claims: revocation and single-winner claims already require
state; signing would add key rotation without removing the lookup. The token is
not sufficient authorization without platform authentication and binding match.
The short `Q7K2` reference is a collision-checked display alias scoped to the
binding and active question, not a secret or a global identity.

“Other” returns instructions/toast and does **not** claim an answer or arm an
implicit “next message answers this” global flag. Free text uses
`im.message.receive_v1` with either:

* `message.parent_id` matching the sent card (direct reply), or a known
  question-associated reply chain indexed server-side; or
* explicit `/answer Q7K2 <text>`, alias scoped to this exact bot/chat/binding.

Parse those before normal batching/stop-for-newer. Extend `FeishuMessageEvent`
and SDK parser to retain `parent_id`/`root_id`; current parser's thread ID alone
is insufficient. Require the same authorized p2p sender, current pairing and
valid message time. A plain unthreaded reply, even with one open card, is not
guessed: show how to correlate when blocked; otherwise keep normal chat behavior.
A stale correlated reply is rejected, never silently converted to a fresh prompt.
Free text maps to `selected:[trimmed_text]`, with `questionId` from stored snapshot;
keep provenance in metadata, **outside** the JSON response string so the existing
parser still recognizes it. Never trust a pasted feishu-v2 provenance header.

For multiple questions or multi-select, one click cannot mean complete submit.
Use schema-2 form container, unique field names mapped to snapshot question IDs,
select controls with stable opaque option references, optional input for custom
text, and one explicit submit button (`form_action_type=submit`, callback token
bound to the full snapshot). Validate `action.form_value` as an exact bounded
schema; combine custom+selected only for multi-select as web does. No per-click
provider submission or globally shared draft. For text replies to a multi-question
card require `/answer REF {"qid":["answer"],…}` (document in card); otherwise send
clarification without claiming. If limits/client support prevent the complete
form, provide web fallback instead of rendering a misleading subset. Form layout
and mobile usability require sandbox acceptance before enabling that card shape.

## 4. Callback transport, authentication and app operations

### 4.1 Endpoint and exact protocol

**Proposed**, not present today:
`POST https://<published-hub-host>/api/feishu/bot/bots/{bot_id}/card-actions`.
This route accepts only Feishu URL verification and schema-2
`header.event_type=card.action.trigger`; it is not the existing message receiver
and not the OAuth login callback. Exempt this one route from browser-cookie login
and CSRF, but require platform cryptographic authentication. Do not expose arbitrary
Hub APIs or redirect to login. Keep primary service/reverse-proxy policy unchanged
until an implementation deployment is explicitly authorized.

Encrypted envelope is `{"encrypt":"<base64>"}`. Verification order:

1. Resolve enabled bot by path (never trust body app ID to select keys). Enforce
   HTTPS edge, POST, content type, <=64 KiB raw body, rate/concurrency bounds,
   read timeout and valid header lengths. Reject missing configured Encrypt Key
   or Verification Token. Redact these credentials, bodies and action/update
   tokens from logs and traces.
2. For real callbacks, require all signature headers and verify constant-time
   comparison of `X-Lark-Signature` with
   `SHA256(UTF8(timestamp + nonce + encrypt_key) || raw_body).hexdigest()`;
   timestamp and nonce are `X-Lark-Request-Timestamp` and
   `X-Lark-Request-Nonce`. This is the documented concatenated SHA256, **not
   HMAC** and not a signature over reserialized JSON. Require timestamp within
   ±5 minutes (Hub policy) and replay-cache nonce/body digest under the bot.
   Exact authenticated duplicates return their prior receipt; altered payload
   under an event ID is rejected. Decrypt using official SDK support:
   SHA256(encrypt key) AES-256-CBC key, prefixed IV and validated padding; do not
   hand-roll cryptography in the route.
3. For URL verification, decrypt `{type:"url_verification",token,challenge}`,
   verify configured token before returning `{challenge:…}` within one second.
   No state/answer mutation. No fallback that accepts arbitrary plaintext
   challenge/token when encryption is required. Provision keys before console
   URL validation. URL verification is excluded from the official normal-callback
   safety-check procedure: it may omit signature headers. Permit that narrow
   encrypted challenge path using decryption plus constant-time token validation;
   if a signature is supplied, require it to verify. Never extend this exception
   to `card.action.trigger`. Test the actual challenge in sandbox before rollout.
4. For real actions, validate decrypted schema/header/token (constant-time),
   app ID and tenant against registered bot, event ID and microsecond
   `header.create_time`, `event.host=im_message`, operator open ID, and
   `event.context.open_message_id/open_chat_id`. Require exact saved card
   message ID and chat binding, current pairing/generation/revision, authorized
   owner and allowed action token. Reject missing context; do not fall back to
   a token-supplied chat or trust union/user ID as same-app open ID.
5. Under shared answer admission, durably record winner/receipt before success
   response. Return HTTP 200 within three seconds with
   `{"toast":{"type":"info","content":"Answer received; delivering to Hub"}}`.
   This acknowledges durable intake, not successful model completion. Invalid
   auth uses 401/403; valid stale/already-answered cards get HTTP 200 warning
   toast without side effects. Storage overload/failure yields an error, never
   a success toast; user can retry explicitly.

The official callback structure contains two unrelated tokens:
`header.token` is Verification Token; `event.token` is a short-lived card-update
credential. Neither is our `action.value.hub_token`. Do not use update credentials
as authentication or as the provider request identity.

Use a lifecycle-owned worker after ACK for provider I/O and outbound updates;
never wait for a model, tenant-token acquisition, remote API, or a native write
inside the three-second callback budget. Atomic local admission has a bounded
budget (target <1s; reject overload); no ACK-before-durable fire-and-forget task.

### 4.2 Manual app setup (implementation rollout, not performed here)

For **each bound application bot**, an authorized app admin must:

1. Keep robot capability enabled and intended user in availability range.
2. Retain Events configuration → long connection → `im.message.receive_v1`.
   Native p2p text requires tenant scope `im:message.p2p_msg:readonly` (existing
   equivalent legacy grants need not be expanded). Do not subscribe all-group
   messages just to obtain question replies.
3. In **Events & Callbacks → Callback configuration**, choose “send callbacks
   to developer server”; configure the HTTPS route above, not localhost and not
   the message Event configuration. Set Encrypt Key, provision it and existing
   Verification Token and expected tenant key into new per-bot protected Hub callback settings, pass URL
   challenge, then add **卡片回传交互 `card.action.trigger`**. Do not add legacy
   `card.action.trigger_v1`; dual subscription can produce two callbacks.
4. Minimal send scope is tenant `im:message:send_as_bot` (or existing broader
   `im:message`). PATCH of sent cards accepts any one of `im:message:send_as_bot`,
   `im:message`, or `im:message:update`; the minimal send grant already suffices.
   This uses the message API, not a new CardKit card-entity API. No
   `cardkit:card:write` is required for raw JSON message cards. No
   `contact:user.employee_id:readonly` is required when using operator open ID.
   `card.action.trigger` itself lists **no subscription permission**. This does
   not remove pre-existing login/bot-profile/image/reaction permissions.
5. Create/publish a new app version and obtain enterprise-admin approval as
   required. Verify callback push logs and sandbox click/text behavior. The task
   does not inspect real secret values or assert current console grants.
6. Enable the Hub feature per bot only after callback setup succeeds. If the
   host is unavailable publicly, retain web/text notification fallback; no
   automatic tunnel, public exposure or SDK monkeypatch is authorized.

Long-connection alternative has no public callback URL, uses SDK App ID/Secret
connection authentication and `register_p2_card_action_trigger` returning
`P2CardActionTriggerResponse`; builder tokens stay empty as official guidance
requires. HTTP signature/decryption does not apply to SDK-delivered callbacks.
It remains an alternative contingent on a verified SDK change and response-frame
simulation, not a reason to accept unauthenticated JSON at the HTTP route.

### 4.3 Platform evidence checked 2026-10-28

These are official Feishu sources, retrieved using their advertised `.md`
alternate when the normal page was a JavaScript shell. Source titles link to
human-readable pages; append `.md` to reproduce the content fetch. No app
console or live integration was reconfigured.

| Source | Design fact supported |
| --- | --- |
| [Card callback structure](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication) | Schema 2.0, `card.action.trigger`, no subscription scope, operator/context/value/form fields, distinction between verification and update token. |
| [Handle card callbacks](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks) | Three-second response; 30-day interaction / 14-day update boundary; update token 30 minutes / two uses; respond before deferred update. |
| [Receive callbacks](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/receive-and-handle-callbacks) | HTTP signature/decryption rules, synchronous callback with no redelivery; WebSocket authentication handled by SDK. |
| [HTTP subscription setup](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/send-callbacks-to-developers-server) | Public request URL and encrypted challenge returned within one second. |
| [Long-connection callbacks](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/configure-callback-request-address) | Native SDK callback registration, custom-app restriction, one cluster-selected recipient rather than broadcast. |
| [Add callback and publish](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/add-callback) | Callback configuration is separate from Events; publish/admin approval activates it. |
| [Send message](https://open.feishu.cn/document/server-docs/im-v1/message/create) | Tenant send grant, interactive JSON string, send UUID and returned message ID. |
| [Receive message](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive) | `im.message.receive_v1`, p2p grant, parent/root IDs and message-ID deduplication. |
| [Schema-2 button](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/interactive-components/button) | `behaviors.callback.value`, disabled buttons and form submit contract. |
| [Update sent message card](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch) | PATCH `/open-apis/im/v1/messages/{message_id}`, same sender, shared `update_multi=true` card. |

Expected tenant identity is provisioned by an authenticated app administrator;
never learn the trusted tenant from the first untrusted callback.

SDK incompatibility is independently reproducible from the published pinned wheel:
GET `https://pypi.org/pypi/lark-oapi/1.5.3/json`, fetch its wheel URL, inspect
`lark_oapi/ws/client.py` with `zipfile` in memory. This checks Hub's exact pin,
not an assumption that current latest SDK fixes it. No package install or lock
change was made for this evidence.

## 5. Delivery, races, expiry and recovery

| Situation | Required behavior |
| --- | --- |
| Callback duplicate | `(bot_id,event_id)` maps to immutable fingerprint/receipt. Same duplicate returns saved outcome. Different event IDs pressing the same or different option still compete on **one question claim**. |
| Text retry | Continue existing bot/message-ID dedup, then deterministic `answer:bot:message` intent. Binding and payload fingerprint must match; do not coalesce answers with nearby prose. |
| Browser retry | Client answer ID survives timeout and reload via receipt lookup; same exact body reuses it. All three channels share the same question claim; equal text from a different ID does not create a second execution. |
| Click versus correlated text versus web | First **durable admission under the shared lock** wins, not Feishu create_time or network arrival. Loser receives already-answered; no second RPC/follow-up. Option clicks in multi-select forms do not claim until Submit. |
| Ordinary text racing answer | Same admission boundary checks whether it is an explicit answer. Blocking native request prohibits unrelated new turn. For follow-up mode, ordinary input admitted first invalidates old card; answer admitted first reserves the follow-up before ordinary FIFO. No generic stop-for-newer may cancel the answer. |
| Queued ordinary/scheduled messages | Answer bypasses entries waiting on this question. Coordinator must not hold the ordinary tab pump lock while awaiting a blocked provider; answer drain has independent bounded ownership. Preserve ordinary FIFO relative to other ordinary entries. No global across-channel FIFO is claimed. |
| Feishu-created turn asks a question | Supported question emits mirror and a typed `question_pending` bridge result; release its external batching waiter without retiring the provider. Native turn remains waiting; follow-up mode retains card after successful one-shot completion. Unsupported approvals retain existing web-required policy. Do not let queued Feishu prose stop the waiting question. |
| Callback ACK lost after durable claim | No platform automatic retry is assumed. User re-click gets the same winning question receipt. Callback worker resumes only proven not-submitted intent; no duplicate native answer. |
| Crash after claim before I/O | Reconcile exact live question and fence, retry same intent only if definitely not submitted. Retained native request lost on backend restart is stale, not recoverable by recreating process. |
| Crash/write ambiguity | Persist `submission_started` before RPC write or follow-up; unknown outcome becomes uncertain, blocks any new answer. Reconcile stream resolution, provider/managed receipt and exact turn proof. No force retry or automatic restore of popped RPC. |
| No bound bot / disabled callback | Web question still works. Record mirror `unbound`/`callback_unavailable`, optionally ordinary link notification when configured. Never send clickable controls claiming they work. A later binding does not receive old questions automatically; explicit remirror requires revalidation and new tokens. |
| Rebind/config rotation/revoked owner | Check pairing ID, generation, effective revision, owner authorization and card message/chat/operator at intake **and** before execution/send. Mark old mirror stale; no token transfer to new chat. Deny group/forwarded card actions even if they carry a valid token. |
| Turn ends / cancels / changes | Codex/TraeX completion, explicit Stop, reset, deletion, incarnation change or newer unrelated turn invalidates matching question. Claude/Cursor success completion alone is the expected follow-up wait. Never expire merely because UI labels the original turn completed. |

Default expiry: min(24 hours after question creation, owner's earlier explicit
expiry). This is a proposed Hub product limit, not Feishu's limit and not a
change to web's no-timeout protocol. On mirror expiry, disable remote actions;
web can still answer a live question. A deliberate remirror can issue fresh
short-lived tokens only after exact live validation. Claims have no automatic
lease expiry that would allow a second answer during uncertain delivery.

Patch sent cards with `update_multi=true`, using the same sending bot identity.
On claim show “Received / delivering”; on proven resolution show the chosen
answer, attribution (web/Feishu), timestamp and disabled buttons (or remove
controls). On stale/expiry show the reason and a safe web link. Escape custom
text and do not display secrets. Updates are a retryable **projection**, not the
answer transaction: latest durable revision wins; serialize updates and discard
outdated revisions so a late “open” patch cannot overwrite “resolved”. Failed
patches cannot reopen a claim. Outside Feishu's update window a stale display
may remain, but backend still refuses its token; log safe status and notify
without duplicating execution. Prefer message-ID PATCH for web answers and
expiry; don't depend on a user-click update token. If deferred token updates
are used, start only after callback response and respect token time/use bounds.

Extend outbound client with a message-returning send method and PATCH method;
keep existing `send_message` callers compatible. Store outbox transitions before
API I/O. On timeout preserve `send_uncertain`; an early callback before the send
response is indexed is rejected as “delivery still reconciling” until that exact
message ID is proven, rather than authorizing the first arbitrary message ID.
A known-success UUID retry within Feishu's dedupe window can recover the message
ID. Never allow a button to be accepted solely because its token exists.

Feishu completion text is relayed only from the exact answer-owned turn (or
original blocking turn), with captured binding rechecked at send. A durable
answer receipt tracks this relay separately from execution; reply failure is not
provider failure and cannot rerun the answer. Claude/Cursor follow-up completion
must not be attached to the original completed question turn. Native questions
can recur in the same turn; each new call gets a separate mirror/claim.

## 6. File/module change plan (future implementation only)

| Owner / file | Planned change |
| --- | --- |
| `services/agent_stream/question_answers.py` (new) | Shared strict question lookup/validation, lock and ownership orchestration; provider-mode dispatch, Goal integration and no-fallback semantics. Actual runtime I/O stays in existing manager/transport. |
| `models/chat_message.py`, `services/chat_message_inbox.py` | Schema-2 question snapshots, action digests, claims, answer receipts and mirror outbox; internal typed answer envelope; answer drain, durable fences, reconciliation/retention. Preserve ordinary and scheduled contracts. |
| `api/routes/agent_stream/dispatch.py` | Route web exact answers through shared service for direct and managed sessions; require exact card identity; replace supported external-question retirement with typed pending outcome. Keep unsupported approvals fail closed. |
| `runtime/{input,publication,retirement}.py`, `providers/{codex_rpc,traex}.py`, `providers/session.py` | Expose exact live question snapshot, invalidation and unambiguous answer outcome; persist provider fences; distinguish placeholder turn completion from cancellation. No write-success boolean after swallowed transport error. |
| `api/agent_stream.py`, `main.py` | Register post-persist question/mirror observers; start/quiesce/drain coordinator and outbox workers once. Reconcile on startup; no uncontrolled detached tasks per callback. |
| `services/feishu_message_format.py` | Add pure schema-2 question/disabled-state/form builder using existing sanitization and payload bounds. Keep static notify builder unchanged. |
| `services/feishu_bot.py` | Extend client with returned message identity, stable UUID send and PATCH; extend typed message event with parent/root IDs; normalized authenticated card-action model distinct from message model. |
| `api/routes/feishu_bot/card_actions.py` (new), `api/feishu_bot.py` | Narrow HTTPS platform-auth adapter and URL verification, strict schema, correlation and immediate receipt; share binding guards and admission, no command execution. |
| `services/bot_pool/{common,state,store}.py`, `api/routes/feishu_bot/pool.py` | Add protected per-bot verification token/encrypt key, admin-configured expected tenant key and callback-enabled status; redact read APIs, require expected revision on updates, rotate/revoke existing action digests. No global credentials for multiple apps. |
| `api/routes/feishu_bot/{message_dispatch,queueing}.py`, `services/feishu_bot_websocket.py` | Correlated reply interception before batching; preserve parent/root IDs; supported question pending outcome; suppress stop-for-newer against waiting/claimed questions. Message transport remains WebSocket. |
| `api/routes/agent_stream/messages.py`, managed `workspace_manager` Chat mailbox owner | Trusted typed-answer dispatch adapter; original turn/call/Goal checks; managed deterministic handoff reconciliation without bypassing session ownership or mailbox dedupe. |
| `frontend/.../useChatDelivery.ts`, `useChatComposer.ts`, inbox/approval presentation | Web uses shared answer receipt/identity; display claimed/resolved/expired projection; stable retry ID and stale errors. Selection remains local until explicit submit. |
| `api/dot.py`, `cli/commands/dot.py` | Optional `dot notify --question-ref REF` / request `question_ref` mirrors an **existing** live question; return question/mirror/answer-status URLs and distinct unbound/unavailable outcomes. Text-only notify stays one-way. No Markdown-to-question guessing. |
| `api/routes/agent_stream/…`, `cli/commands/rest.py` | Proposed authenticated GET `/tabs/{tab_id}/questions` and POST `/tabs/{tab_id}/questions/{question_ref}/answers` with `{call_id,answers}`; GET answer receipt. CLI `tab question list`, `tab question answer --question-ref … --answers-file … --call-id …`. Managed equivalent resolves existing session owner; does not masquerade as ordinary tab message. |
| `scheduling/chat_runs.py`, Dot task/schedule prompting | Reuse stream-created questions and optional task/schedule metadata for display. No new schedule kind, callback run-now call, task accept, or catch-up timer. Document how emitting agent uses question tool, then optional notify-by-ref; automatic mirror avoids duplicate sends. |
| Existing tests / `docs/CHAT_MESSAGES.md`, bot setup guide | Add cross-channel behavioral coverage and update current contracts after implementation. Keep stateless CLI card helper contract independent. |

Question-list/answer API must apply the same authenticated tab/session visibility
policy as stream input; public callers cannot supply trusted bot/operator,
incarnation or scheduler fields. Do not widen local-network shared auth into a
claim of per-agent security. `dot notify --question-ref` checks the question is
owned by Dot's current tab and already persisted; if an executing agent races
its tool event publication it must retry lookup, not fabricate an ID.

Tasks and schedules do not need a second question-creation API for this goal.
Model-emitted question events are the single creation source. If a deterministic
system-owned catch-up question is required later, define its typed owner/resolver
and render it through the same question projection; it cannot be falsely tagged
as a provider RPC. This extension is explicitly deferred rather than hidden in
notification formatting.

### Config, migration and limits

Default feature off until per-bot HTTP keys and setup are complete. Settings live
with existing protected bot-pool secrets; no credentials in CLI argv/output,
question JSON or Markdown. Callback path bot ID is public, not a credential.
No plaintext-token-only production mode. Inbound route needs a narrowly scoped
reverse-proxy/auth exemption and body/rate limits; document this at implementation
release. This task does not change the running proxy, port or app.

Inbox version-1 → version-2 migration adds empty question/answer/outbox sections,
preserves every existing message, sequence, incarnation and tombstone; atomic
write, version validation and backup before rollout. On corruption disable this
feature without breaking web Chat. Downgrade requires draining/quiescing answers
and a compatible reader/export; never discard claimed/uncertain records to make
an older binary start. Bot-pool migration adds optional disabled settings; old
bots retain current notification behavior. Historical cards are not automatically
made actionable. Bound records (100 open questions/tab; existing text limits;
answer text <=64 KiB total); overload remains a visible rejection.

Keep full terminal answer/mirror records for 30 days, then remove custom text and
action secrets; retain compact consumed-question and answer-call tombstones so
retries cannot become executable again. Expired mirrors are not permission to
close the underlying provider question. Observability: safe question-ref,
receipt ID, transport/auth rejection reason, state transitions, callback latency,
stale clicks, outbox attempts; never raw credentials/tokens/content.

## 7. Alternatives and unresolved deployment questions

| Alternative | Why rejected / condition |
| --- | --- |
| Let the model read “Run now” and infer pending question | Wrong-turn risk and provider blocking deadlock; cannot enforce exact request or races. |
| Put answer JSON in ordinary `tab message` | Busy/Goal FIFO prevents in-turn RPC; `allow_question_answer=False` is intentional. Use trusted answer kind plus shared admission. |
| Callback runs `schedule run-now` or suggested `task accept` CLI | Bypasses the waiting Chat, authorizes different semantics, and turns untrusted value into a command surface. |
| Only extend `dot notify` with buttons | Notification has no provider call identity, message ID retention or callback receiver; external bridge currently cancels questions. |
| New Feishu-only queue/database | Duplicates existing durable delivery and leaves web/Feishu races unsolved. Extend existing intent owner and explicit managed handoff instead. |
| WebSocket callback registration only | Pinned SDK drops CARD; future verified upgrade plus frame tests may remove HTTPS requirement. No monkeypatch in design delivery. |
| Self-contained signed values | Still need server state for claims, revocation and binding; opaque per-action tokens simplify rotation. |
| “Next message is the answer” after Other | Interleaved questions and old cards can redirect prose. Require reply-to-card or scoped explicit alias. |
| Broad permission or legacy callback subscription | Unneeded access/duplicate formats. Native schema-2 action plus p2p message scope suffices. |

Open questions with recommended defaults (not blockers for this doc):

1. **Public callback reachability:** current reverse-proxy reachability and app
   console settings were not probed. Default HTTPS adapter if deployment owner
   can provide that narrow public path; otherwise keep feature disabled until a
   separate SDK upgrade is verified. No tunnel or exposure inferred from this task.
2. **Screenshot origin:** original schedule/Chat event unavailable. Default
   generic stream question integration; obtain actual event identity during
   implementation acceptance, without inventing a scheduler-owned catch-up flow.
3. **Managed external binding eligibility:** ordinary built-in Feishu bridge
   currently targets direct Chat. Default mirror only already-valid bound targets;
   preserve existing managed-tab restrictions. Shared exact-answer service should
   support managed web semantics, but widening Feishu pairing to managed sessions
   requires its own authorization review; no backdoor via ordinary messages.
4. **Product expiry:** recommend remote token TTL 24h with explicit remirror,
   while live web questions retain current no-timeout behavior.
5. **Multi-question forms:** recommend one atomic form submit, no mutable shared
   selection state. Require sandbox mobile verification before enabling this shape;
   safe web fallback until it passes. Single-question choice/text is the first
   release slice; the general contract must not silently drop other questions.

## 8. Verification and reviewer gates

This document specifies future tests; **no callbacks/product feature were
implemented or live-tested by this task**. Candidate-doc checks and actual
results are recorded below, separately from the following implementation plan.

1. Pure payload tests in `tests/test_feishu_message_format.py`: native schema-2
   callbacks, distinct tokens, no raw routing IDs/commands, disabled terminal card,
   sanitization, UTF-8 bounds, multi-question completeness, secret exclusion.
2. Extend `test_chat_message_inbox.py` / `test_chat_message_routes.py`: atomic
   winner/fingerprint/receipt, single provider response or follow-up, ordinary
   FIFO preserved, answer can bypass blocked FIFO, Goal answers, independent tabs,
   corruption/backpressure, expiry/tombstones, restart at every durable boundary.
3. Extend provider/native/managed tests: exact turn+call with reused question IDs,
   JSON-RPC request matching, popped request write failures, Stop/reincarnation,
   Claude placeholder completion versus cancellation, managed handoff crash and
   mailbox fingerprint reconciliation. No answer text fallback on stale native.
4. Feishu handler tests near `test_feishu_bot*.py`: valid encrypted/signed event,
   known callback response <3s with slow fake provider, challenge <1s, wrong
   signature/token/app/tenant/operator/chat/message/binding, forwarded/group click,
   revoked owner, rotation before dispatch, oversize/malformed body, replay and
   same-event changed payload. Challenge path never mutates question state.
5. Race test simultaneously submit web, click, correlated text and ordinary
   message with controlled admission barriers: exactly one structured answer;
   ordinary input either waits or invalidates by the stated rule. Test duplicate
   event IDs and different event IDs pressing the same button separately.
6. Extend external bridge tests: supported question does not trigger retirement;
   blocked provider remains answerable, batching waiter releases, answers never
   coalesce/stop-for-newer; unsupported permission approval still requires web.
   Confirm assistant relay watches the right answer turn and never re-executes
   after reply failure.
7. Browser mocked gate: same question seen in web, simulated Feishu answer updates
   original card after stream replay/reload; stale click cannot answer new card.
   Cover Other, single/multi select, keyboard/mobile and secret-only fallback.
8. Sandbox application gate (manual permission/public-path setup): send real card,
   click option, reply to card and use explicit alias, answer from web first,
   disconnect around callback ACK/native write, rebind, expire and observe card
   disabled. Record exact app version/scopes and safe event/message IDs, no
   secret body. Unit/simulation pass is not this live integration gate.

Local callback simulation recipe for implementation reviewers:

```text
Create a test-owned runtime under the feature worktree; set CLAUDE_HUB_HOME,
CLAUDE_HUB_STATE_ROOT and unique CLAUDE_HUB_TMUX_SOCKET before backend imports.
Use FastAPI TestClient/ASGITransport (no primary port) and a fake bot-pool binding,
stream store, controllable native provider and mocked outbound Feishu client.
Seed question (turn-A, call-A, qid=0), sent card om-test and per-action digests.
Build schema-2 event with header, operator, action.value and context; encrypt it
using test-only Encrypt Key and sign exact serialized envelope bytes per §4.1.
POST the proposed /api/feishu/bot/bots/bot-test/card-actions route.
Assert 200 toast only after durable claim and before fake native write unblocks.
Drain owned worker; assert one exact answer, original approval_resolved and PATCH.
Replay same event, then a new event with a conflicting option: neither sends again.
Race a web POST and im.message.receive_v1 reply with parent_id=om-test against it.
Restart at claim/prewrite/postwrite/handoff boundaries and assert uncertainty fences.
Tear down only test-owned workers/runtime; no live bot, default tmux or 8173.
```

The SDK alternative additionally requires a real protobuf CARD-frame input to
the selected SDK version and an assertion that its returned response frame carries
the toast, rather than calling its registered handler directly. The pinned
1.5.3 frame currently fails that criterion, as the source inspection predicts.

Review acceptance mapping: §1 addresses current-flow evidence; §§2–5 define
payload/callback/security/resolution; §§6–8 supply file plan, operations,
alternatives and simulations. A final independent reviewer must check these
against the pinned sources and approved Goal Packet. Human acceptance remains
required; branch delivery does not authorize integration or rollout.

### Candidate documentation validation

- Worktree: `/home/tiger/claude_hub_worktree/rv-feishu-interactive-cards`;
  branch `codex/rv-feishu-interactive-cards`, based on fetched/synced clean main
  `0211661a1e627349822b3857ecf74f33b81818ef`. Existing rv branches untouched.
- `./scripts/verify.sh all`: exit 1. `docs` and `checkout-safety` passed;
  backend-format/types/tests could not run because worktree `backend/.venv`
  is absent; frontend-lint/types/tests/build could not run because locked
  frontend dependencies are absent. No full-suite pass is claimed. This
  documentation-only task did not install product dependency environments.
- Backend verification evidence retained by the script at
  `/tmp/claude-hub-verify.cr6qshdt`, `/tmp/claude-hub-verify.eqtk0hid`,
  `/tmp/claude-hub-verify.d36q5_6w`; each owned disposable runtime was removed.
- `./scripts/verify.sh docs`: passed independently after draft review.
- `git diff --check`: passed; JSON parsing passed for the native card example;
  all 14 checked key code/document evidence paths exist.
- `./scripts/verify.sh check-staged`: passed for the explicitly staged design
  document and changelog. Browser/live Feishu and callback simulation are
  implementation gates, not executed feature tests.
- No task-owned server was started. No live config/credentials inspected or
  changed. No lessons needed.
