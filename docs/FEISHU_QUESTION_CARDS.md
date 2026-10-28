# Feishu question cards (default off)

Hub can mirror one non-secret, single-select Chat question into the **p2p chat
already bound to that tab**. Option buttons and explicit text replies share the
web card's exact question admission. This does not enable permission approvals,
plan approvals, Task acceptance, terminal prompts, or schedule commands.
Multi-question/multi-select cards remain web-only; their questions are never
silently dropped. Ordinary `dot notify` text remains one-way.

## Manual app setup before enabling

Do these steps for each application bot; Hub does not change your Feishu console.

1. Keep robot capability enabled and the intended user in the app availability
   range. Keep **Events configuration** using long connection with
   `im.message.receive_v1`. Do not change the message transport to a webhook.
2. Retain tenant scopes `im:message:send_as_bot` and
   `im:message.p2p_msg:readonly` (existing equivalent broader grants also work).
   `card.action.trigger` itself requires no additional subscription scope.
   Message-card PATCH accepts `im:message:send_as_bot`, `im:message`, or
   `im:message:update`; the send grant already suffices. No CardKit entity scope
   or employee/user-ID scope is needed. Existing unrelated scopes remain intact.
3. In **Events & Callbacks → Callback configuration**, select **send callbacks
   to developer server** and use
   `https://YOUR-HUB/api/feishu/bot/bots/BOT_ID/card-actions`.
   This is separate from Events configuration and the OAuth login callback.
   Subscribe **卡片回传交互 / `card.action.trigger`**, not legacy
   `card.action.trigger_v1` and not both formats.
4. Set the app Encrypt Key, obtain its Verification Token and expected tenant
   key, and provision them into Hub before console URL verification. Use the
   authenticated PUT `/api/feishu/bot/bots/BOT_ID/callbacks` with JSON fields
   `expected_revision`, `callback_enabled`, `verification_token`, `encrypt_key`,
   and `expected_tenant_key`. Read the current revision from the redacted Bot
   pool API. Configuration changes require the expected revision and invalidate
   old callback snapshots; pairings are retained. An enabled setting requires
   all three values. To disable, send `callback_enabled:false` with the current
   revision. **All existing bots default to false.**
5. Provision values from a protected file or request body, never command-line
   values, logs, shell history, card payloads or screenshots. For example,
   `curl --data-binary @/protected/callback-settings.json` sends the file body
   without putting secrets in argv; use the existing authenticated session-cookie
   mechanism. Do not use `--payload-json` with real credentials. Pool read APIs
   expose configured/enabled status and tenant, never verification/encryption keys.
6. Save the URL after its encrypted challenge succeeds. Create/publish the new
   app version and obtain enterprise-admin approval where required. Console
   subscription changes take effect after publication. Test with a sandbox app
   before enabling the production binding.

The endpoint requires HTTPS, encrypted payloads, the documented raw-body SHA256
signature and configured token. A URL challenge may omit signature headers but
must decrypt and match the token; it cannot mutate questions. Actual actions
always require signature headers, app/tenant/operator/chat/message checks and the
current pairing. No plaintext-token production mode is provided.

Hub pins `lark-oapi==1.5.3`, which discards CARD frames. Merely adding a WebSocket
handler cannot enable these cards. This feature uses HTTPS callbacks and leaves
ordinary message WebSocket connections unchanged; no SDK patch is installed.

## Reverse proxy / deployment boundary

Only the exact POST card-actions path needs exemption from browser-cookie login
and CSRF middleware. It authenticates itself cryptographically. Do not exempt
other Hub APIs, redirect callbacks to login, expose extra ports, or open a tunnel.
Configure a public HTTPS route and trusted proxy scheme forwarding through your
normal reviewed deployment workflow; this feature does not deploy or reconfigure
it. The application checks the resulting request scheme, not a caller-supplied
untrusted forwarded header.

Keep edge body limits at 64 KiB or less, bound upstream concurrency and read time,
and preserve raw bytes plus X-Lark signature/timestamp/nonce headers. Hub also
limits bodies to 64 KiB, reads to one second, and requests to 60/minute per bot;
answer admission has a bounded timeout and does not wait for provider I/O.
Response targets are under one second for challenges and under three seconds
for card intake. An intake toast confirms **durable receipt**, not model completion.
Feishu card callbacks have no automatic redelivery guarantee: retry explicitly
if intake fails. Duplicate events return their existing receipt; changed content
under an event identity conflicts.

## Answer and status behavior

- A native option button answers its exact original turn/call; “Run now” is an
  answer to the conversation, not a direct scheduler API operation.
- **Other** displays instructions. Reply directly to the card, or send
  `/answer QUESTION_REF your text`. Hub does not guess that unthreaded prose
  belongs to the most recent question. Plain text still follows existing chat
  behavior; correlated stale answers never fall through as new prompts.
- One durable claim wins across web, button and reply. A native blocking question
  gets its original provider response; Claude/Cursor get a same-conversation
  follow-up turn. That answer can bypass ordinary messages waiting behind the
  question without changing their relative FIFO order.
- The web card displays received/delivering, answered, stale and uncertain states
  from durable receipts, including after reload. Remote token expiry does not
  disable an otherwise live web question.
- Remote choices expire after 24 hours. Explicit remirror revalidates the live
  question and issues new opaque actions; old actions are revoked. Rebinding,
  key/settings rotation, Stop, reset/deletion or runtime replacement invalidate
  old remote actions. Successful Claude/Cursor placeholder-turn completion alone
  does not invalidate its waiting follow-up question.
- No binding or disabled callbacks preserves static notifications/web answering.
  Failed/uncertain outbound delivery is not evidence of a usable card. Unknown
  native write outcomes remain fenced and never replay automatically. Inspect
  the conversation before taking any manual recovery action.

Inspect questions with `claude-hub tab question list TAB_ID`. Answer with
`claude-hub tab question answer TAB_ID --question-ref REF --answers-file FILE
--call-id STABLE_ID`; the file contains one JSON string-array answer. Reuse the
same call ID and body after uncertain HTTP receipt. `dot notify --question-ref
REF` explicitly mirrors an existing live question owned by Dot's current tab;
it does not turn Markdown into a pending question. Task and schedule integrations
should emit their normal provider question; no new schedule kind is introduced.

## Storage and verification

The durable inbox migrates v1 to v2 atomically and retains a protected
`chat_messages.v1-backup.json`. Do not manually edit runtime state. A corrupt
inbox disables durable/remote delivery while the exact web runtime answer path
remains usable. Terminal answer/question content expires after 30 days; compact
identity/fingerprint tombstones remain so old IDs cannot become executable again.
At most 100 open questions per tab are admitted, and answer text is bounded to
64 KiB. Secret questions never enter the mirror store.

The backend test suites `test_feishu_choice_callbacks.py`,
`test_question_answer_inbox.py`, and `test_question_answers_runtime.py` cover the
real encrypted adapter, loopback TLS, races, provider routing and recovery.
`frontend/tests/browser_managed_chat_question.py` exercises claimed/resolved
projection after reload and stale-card rejection against mocked APIs. These are
not evidence that your real app subscription, public route or deployment works;
that final sandbox/live check remains an operator gate.

Official platform sources and architecture decisions are linked in the
[accepted design](working-logs/2026-10-28-feishu-interactive-choice-cards.md).
