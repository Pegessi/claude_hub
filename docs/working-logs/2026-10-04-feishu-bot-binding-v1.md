# Feishu Bot binding and existing Chat bridge v1

## Scope

This change adds the first version for binding one authenticated Claude Hub user
to one existing local direct Chat tab, including the Chat-side connection panel.
It does not register a real Feishu application, provision permissions, start a
tunnel, or create a new agent runtime.

The supported flow is:

1. A user with a real Feishu OAuth session calls
   `POST /api/feishu/bot/bind/start` with a server-known `tab_id` and, when the
   tab belongs to a Workspace, its matching `workspace_id`.
2. Claude Hub returns a ten-minute, single-use code and the canonical event URL.
   Code creation is limited to five requests per user per minute.
3. The same Feishu user sends that code to the configured Bot in a p2p chat.
4. Later p2p text messages from that exact `app_id` + `open_id` + `chat_id`
   binding enter the existing direct Chat provider session.
5. Claude Hub waits for that turn's persisted `turn_completed` event and sends
   its `assistant_text` to the same Feishu chat.
6. The Web user can inspect or remove only the binding keyed by their own
   authenticated `open_id` through `GET/DELETE /api/feishu/bot/binding`.

## Chat connection panel

Every existing local direct Chat mounts one small Feishu connection panel. It
reads the authenticated user's current binding and distinguishes five observable
states: unbound, pending code, bound to this Chat, bound to another Chat, and
error. Generating a code sends only the current `tab_id`; no client-controlled
cwd, shell, provider session, or Workspace target is accepted.

A pending code is polled only while the Chat pane is active and stops when the
KeepAlive pane is deactivated. Moving an existing binding to another Chat still
requires the same Feishu user to send a newly generated code; the old binding
remains active until that code is consumed. Disconnect uses the server's DELETE
operation and therefore removes both the binding and every outstanding code for
the authenticated owner. A 401 response never produces local optimistic state;
the panel shows the existing `/api/auth/login` Feishu login entry instead.

## Identity and target boundary

Binding management deliberately does not use the local-network identity bypass.
`require_real_feishu_user` requires an unexpired stored login cookie and the
normal OAuth whitelist check, even when ordinary local Hub routes allow the
synthetic `local` user.

Claude Hub currently has no per-tab owner field. Therefore v1 treats a real,
whitelisted Web user as an instance administrator who may select any existing
local direct Chat tab. This is an explicit administrator boundary, not a claim
of per-user tab ownership. The server validates the tab and optional Workspace
association; the Feishu sender cannot supply a tab id, cwd, shell, provider
session id, or Workspace id in an event.

The Bot app id must equal the OAuth app id. Feishu `open_id` values are scoped
to an app, so mismatched applications are rejected instead of treating their
identifiers as the same person. The binding stores the OAuth-provided email as
trusted identity data. Current open-id/email allowlists are checked when the
code is consumed, immediately before dispatch, and again before the completed
assistant response is sent. Revocation therefore prevents new execution, while
an unbind or rebind during a running turn suppresses its old response.

## Existing Chat adapter

`dispatch_tab_chat_and_wait()` lives beside the existing agent-stream routes. It
subscribes to the existing tab `TailerManager`, calls the same direct Chat
admission and `_send_to_native` path used by the Web composer, filters on the
actual accepted turn id, and returns the persisted completion text. It does not
construct another provider transport, process, tmux session, cwd, or agent
identity. Active Goal handling therefore remains governed by the same lock and
policy as browser Chat input.

## Callback validation and persistence

`POST /api/feishu/bot/events` supports the official `url_verification`
challenge and `im.message.receive_v1` schema. It accepts only user-authored p2p
text messages. The request stream stops with HTTP 413 as soon as it crosses 256
KiB, including chunked requests without `Content-Length`; decoded text is
limited separately.

When `CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY` is configured, the secure mode:

- verifies `X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`, and
  `X-Lark-Signature` against the exact encrypted request bytes before decryption;
- rejects request timestamps outside five minutes;
- decrypts the official `base64(iv + ciphertext)` AES-256-CBC format with the
  SHA-256-derived key and PKCS7 validation, using `cryptography` rather than a
  local cipher implementation.

Without an Encrypt Key, the official plaintext mode accepts the plaintext
challenge/event and validates its Verification Token, app id, and event
`create_time`. That mode has no request signature and is intentionally described
as unsigned; deployments requiring source authentication should use encrypted
mode.

Both modes enforce an event timestamp window and persistent message-id
deduplication, as required by the Feishu receive-message documentation (event id
alone is not a sufficient deduplication key).

Bindings, hashed pairing codes, bounded code-rate history, and claimed message
ids are atomically stored in `<CLAUDE_HUB_HOME>/feishu_bot.json` with mode
`0600`. Unbinding removes all outstanding codes for that owner. Bindings and
dedup claims survive process restart. A claimed event is handled at most once;
if the process dies after the claim, v1 does not automatically replay that
in-flight turn because doing so could execute the same user instruction twice.

## Configuration

Secrets are read only from explicit environment variables and are never placed
in API responses or logs:

- `CLAUDE_HUB_FEISHU_BOT_APP_ID`
- `CLAUDE_HUB_FEISHU_BOT_APP_SECRET`
- `CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN`
- `CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY` (recommended; omit only to use the
  unsigned plaintext mode)
- optional test/enterprise API override: `CLAUDE_HUB_FEISHU_API_BASE_URL`

External URLs use
`claude_hub.services.public_base_url.get_public_base_url()`. Resolution order is
`CLAUDE_HUB_PUBLIC_BASE_URL`, then `CLAUDE_HUB_PROVIDER_PUBLIC_URL`, then the
configured local host and port. Values must be a credential-free HTTP(S) origin
without a path, query, or fragment. Request `Host` and forwarding headers are
not used.

## References checked

The protocol shape was checked against these official Feishu Open Platform
pages:

- `event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/send-callbacks-to-developers-server`
  (last updated 2025-06-05): URL verification, Verification Token, Encrypt Key,
  encrypted-body shape, and signature/replay purpose.
- `server-docs/im-v1/message/events/receive`: `im.message.receive_v1`, p2p text
  fields, sender type, and the instruction to deduplicate on `message_id`.
- `server-side-sdk/nodejs-sdk/handling-events`: sending a response with
  `receive_id_type=chat_id`.

The existing product behavior was also checked read-only at fixed source
`seed/maso@a7eb9cf4dd436af9d9eb23495374ad8a156adc10`:

- `packages/cowork/src/maso_cowork/server/api/routes/feishu.py`: pair-code and
  binding target validation.
- `packages/cowork/src/maso_cowork/feishu/plugin.py`: local target resolution,
  inbound message routing, sender/client identity, and durable deduplication.

Claude Hub reuses its own agent-stream runtime rather than copying the MASO
runtime or reading MASO configuration and credentials.

## Verification limits

Tests use an isolated Claude Hub runtime, signed synthetic callback payloads,
mocked provider completion, and `httpx.MockTransport` for Feishu APIs. No real
Bot registration, binding, message delivery, webhook, tunnel, or live Hub
instance was used or verified.
