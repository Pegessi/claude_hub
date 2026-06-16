# 2026-06-16 — Feishu interactive cards over the `claude-hub` CLI

## System Overview

This change lets an **external agent** ask a **human** for a decision over
Feishu and collect the answer back into the shell — "Scenario A":

```
agent → claude-hub feishu send-card --wait → [Feishu card to human]
                                                     │ human clicks / types
        human's decision  ◄── backend result store ◄─┘ (your bot relays callback)
```

The agent runs one blocking CLI command; the CLI pushes an interactive card to
a chat, then long-polls a small backend result store until the human acts.

The relay back is handled by **your own Feishu bot** — Hub ships no
long-connection bot. Your bot already receives Feishu events; in its
`card.action.trigger` handler it calls a lark-independent helper Hub exposes,
`feishu_cards.parse_card_action(payload)`, to pull `{token, action, form,
operator_id, chat_id}` out of the raw callback, then POSTs that to
`POST /api/feishu/cards/result`, which unblocks the waiting CLI.

This builds on the earlier CLI (`2026-06-15-claude-hub-cli.md`). It adds **one**
new backend surface: the card result store + its `/api/feishu/cards/*`
endpoints. Everything else is CLI-side helpers.

Scenario B (a human typing `/hub …` commands directly to the bot) is **out of
scope**, but Hub still exposes `hub_commands.run_hub_chat_command(client, text)`
as a reusable, lark-independent helper your bot can call from its message
handler to turn a `/hub …` chat line into a backend action + reply.

## Module Design

Backend (`backend/claude_hub/`):

- `services/feishu_card_results.py` — `CardResultStore`, an in-memory,
  TTL-pruned, first-write-wins store keyed by an opaque token. `register()`
  marks a token pending (idempotent — never clobbers a resolved decision),
  `submit()` records the human's `action`/`form`/`operator_id` exactly once,
  `get()` returns `pending` / `resolved` / `unknown`. A module-level singleton
  `card_result_store` backs the API. Time is injectable (`now=`) for tests.
- `api/feishu.py` — three endpoints under `/api/feishu/cards`:
  - `POST /register` — `{token, chat_id?, kind?}` → pending (token `min_length=8`).
  - `POST /result` — `{token, action, form?, operator_id?}` → resolved; `409`
    on unknown or already-resolved token (double-click / lost race).
  - `GET  /result/{token}` — current decision (`unknown` for never-seen tokens).
  These inherit the backend's loopback auth bypass; the token is the capability.

CLI (`backend/claude_hub/cli/`):

- `feishu_cards.py` — pure card builders + the correlation contract **and its
  inverse**, `parse_card_action()`. Every interactive control's `value` carries
  reserved keys `hub_token` (`TOKEN_KEY`) and `hub_action` (`ACTION_KEY`).
  `INTERACTIVE_KINDS` (`approval`, `needs_input`, `plan_confirm`) embed a token;
  `DISPLAY_KINDS` (`status`, `task`) render live backend data and carry no
  token. `needs_input` uses a `form` container with a named `input` so the
  callback's `form_value` maps field-name → entered text. `parse_card_action()`
  is the single integration point for an external bot: it accepts the **raw
  callback dict** Feishu delivers (or a lark-oapi attribute object, or the inner
  `event`), returns `{token, action, form, operator_id, chat_id}`, and yields
  `None` for foreign cards / malformed payloads. It is lark-independent and
  never raises.
- `feishu_sender.py` — `send_card(app_id, app_secret, chat_id, card) -> str`.
  Imports `lark_oapi` lazily so importing the CLI never hard-depends on the SDK;
  normalizes SDK/transport failures to `FeishuSendError`. (This is the only
  lark-touching code left, and only on the outbound send path.)
- `feishu_store.py` — friendly chat-id aliases persisted at
  `$CLAUDE_HUB_CONFIG_DIR/feishu_bindings.json`. `resolve_target()` returns a
  binding if known, else passes a raw `oc_…` id straight through.
- `hub_commands.py` — `run_hub_chat_command(client, text) -> reply`, a reusable
  lark-independent dispatcher for `/hub …` chat commands. Not used by Hub itself
  (Scenario B is out of scope); kept as a helper a user's own bot can call.
- `commands/feishu.py` — the `feishu` group: `bind`/`bindings`/`unbind`,
  `send-card`, and `result`. `send-card --wait` registers the token, sends the
  card, then long-polls (`_poll_result`, default 300 s / 2 s cadence).

## Usage

```bash
# One-time: alias a chat id so agents don't paste oc_… everywhere.
claude-hub feishu bind ops --chat-id oc_abc123
claude-hub feishu bindings
claude-hub feishu unbind ops

# Inspect a card without sending (no creds needed).
claude-hub feishu send-card --kind approval --title "Deploy?" --body "ship v2" --dry-run

# Ask a human and BLOCK until they click (the agent's main use):
claude-hub --json feishu send-card --kind approval --to ops \
    --title "Deploy v2?" --body "All checks pass." --wait --timeout 120
#  → {"status":"resolved","action":"approve","operator_id":"ou_…"}  (or "timeout")

# Free-text answer back from the human:
claude-hub --json feishu send-card --kind needs_input --to ops \
    --title "Release note?" --body "One line please" --field-name note --wait

# Display-only cards (live workspace data; --wait is rejected here):
claude-hub feishu send-card --kind status --to ops --workspace-id ws1
claude-hub feishu send-card --kind task --to ops --workspace-id ws1 --task-id t1

# Poll a decision out-of-band (e.g. after a non-waiting send):
claude-hub --json feishu result <token>
```

Credentials resolve flag > env (`$FEISHU_APP_ID`/`$FEISHU_APP_SECRET`) >
backend config.

Relaying the click back lives in **your own Feishu bot**, not in Hub. In its
`card.action.trigger` handler:

```python
from claude_hub.cli.feishu_cards import parse_card_action
from claude_hub.cli.client import HubClient

def on_card_action(callback_body: dict):
    decision = parse_card_action(callback_body)   # raw Feishu callback JSON
    if decision is None:
        return                                    # foreign card — ignore
    with HubClient(base_url="http://127.0.0.1:8173") as client:
        try:
            client.submit_card_result({
                "token": decision["token"],
                "action": decision["action"],
                "form": decision["form"],
                "operator_id": decision["operator_id"],
            })
        except Exception:
            pass   # 409 == already resolved / double-click; safe to ignore
```

That `POST /api/feishu/cards/result` unblocks the waiting `send-card --wait`.

## Smoke Test

1. Start the backend. Wire your own Feishu bot's `card.action.trigger` handler
   to call `parse_card_action` + `submit_card_result` (see Usage above).
2. `claude-hub feishu bind me --chat-id <your oc_… chat id>`.
3. `claude-hub --json feishu send-card --kind approval --to me --title T --body B --wait`
   — the command blocks; an Approve/Reject card appears in the chat.
4. Click a button. Your bot receives the callback, parses it, and POSTs the
   result; the CLI prints `{"status":"resolved","action":"approve|reject",…}`
   and exits 0.
5. Re-run with `--kind needs_input … --field-name reply`; type text, submit;
   the printed `form` carries `{"reply":"…"}`.
6. `--kind status --workspace-id <ws>` (no `--wait`) renders the live board as a
   card; `--wait` on a display kind exits non-zero by design.

For unit coverage without Feishu: `parse_card_action` is tested against raw
dicts and attribute objects in `tests/test_feishu_parse.py`.

## Key Pitfalls

- **Register before send.** `send-card` POSTs `register` *before* pushing the
  card, so a fast human click can't reach `submit` before the token exists
  (which would 409 and lose the decision).
- **First-write-wins + idempotent register.** Double-clicks and re-registers
  must never overwrite a recorded decision — `submit` returns `False` /
  endpoint returns `409`, and the original `action` stands.
- **Display kinds carry no token and can't `--wait`.** There is nothing to
  collect; the CLI rejects `--wait` for `status`/`task` up front.
- **lark imported lazily, and only for sending.** `feishu_sender` imports
  `lark_oapi` inside its function so `import claude_hub.cli.main` works without
  the SDK; tests inject a fake `lark_oapi` module tree via
  `monkeypatch.setitem(sys.modules,…)`. The inbound relay path
  (`parse_card_action`) takes no lark dependency at all — it parses plain dicts.
- **Foreign cards ignored.** `parse_card_action` returns `None` unless the
  control's `value` is a dict carrying `hub_token`, so unrelated cards your bot
  sees never hit the store.
- **Raw dict OR SDK object.** `parse_card_action` reads fields with both
  key and attribute access, so it works on the raw webhook JSON your bot
  receives and on lark-oapi event objects alike.
- **TTL pruning.** Unresolved tokens expire (default window) so the store can't
  grow unbounded; resolved tokens stay readable within the TTL.

## Files

- `backend/claude_hub/services/feishu_card_results.py` (new — result store)
- `backend/claude_hub/api/feishu.py` (new — `/api/feishu/cards/*`)
- `backend/claude_hub/cli/feishu_cards.py` (new — builders + contract +
  `parse_card_action`)
- `backend/claude_hub/cli/feishu_sender.py` (new — `send_card`)
- `backend/claude_hub/cli/feishu_store.py` (new — chat-id bindings)
- `backend/claude_hub/cli/hub_commands.py` (new — reusable `/hub` dispatcher)
- `backend/claude_hub/cli/commands/feishu.py` (new — `feishu` group)
- `backend/claude_hub/cli/client.py` (`register_card`/`submit_card_result`/`get_card_result`)
- `backend/tests/test_feishu_card_results.py`, `test_feishu_api.py`,
  `test_feishu_store_cards.py`, `test_feishu_commands.py`,
  `test_feishu_parse.py` (parse function), `test_hub_commands.py`
