# 2026-06-16 — Feishu interactive cards over the `claude-hub` CLI

## System Overview

This change gives an **external agent that is itself a Feishu bot** two small,
stateless CLI helpers for talking to a **human** over Feishu — "Scenario A". The
agent sends a card to the human and receives the `card.action.trigger` callback
**in the same process**, then drives Hub through the CLI:

```
you (human) ──Feishu──► agent (the Feishu bot) ──claude-hub CLI──► Claude Hub
            ◄─Feishu───                         ◄────────────────
```

Because the agent both sends the card and receives the click, Hub is **not** in
the Feishu loop. There is nothing to correlate across processes, so Hub ships:

- **no** outbound card sender,
- **no** token / result store and **no** `/api/feishu/cards/*` endpoints,
- **no** chat-id bindings or long-polling.

It exposes only two pure, IO-free helpers as thin CLI commands that speak JSON on
stdout, so the agent can `subprocess` them and parse the result without writing
any Python:

- `feishu build-card` — build a card's JSON (the agent sends it to Feishu itself).
- `feishu parse-action` — parse a raw `card.action.trigger` callback into a
  normalized `{token, action, form, operator_id, chat_id}` decision.

This builds on the earlier CLI (`2026-06-15-claude-hub-cli.md`) and adds **no**
new backend surface.

## Module Design

CLI (`backend/claude_hub/cli/`):

- `feishu_cards.py` — pure card builders + the correlation contract **and its
  inverse**, `parse_card_action()`. Every interactive control's `value` carries
  reserved keys `hub_token` (`TOKEN_KEY`) and `hub_action` (`ACTION_KEY`).
  `INTERACTIVE_KINDS` (`approval`, `needs_input`, `plan_confirm`) embed a token;
  `DISPLAY_KINDS` (`status`, `task`) render live backend data and carry no
  token. `needs_input` uses a `form` container with a named `input` so the
  callback's `form_value` maps field-name → entered text. `parse_card_action()`
  accepts the **raw callback dict** Feishu delivers (or a lark-oapi attribute
  object, or the inner `event`), returns `{token, action, form, operator_id,
  chat_id}`, and yields `None` for foreign cards / malformed payloads. It is
  lark-independent and never raises.
- `commands/feishu.py` — the `feishu` group with exactly two commands:
  - `build-card --kind <kind>` — builds the card JSON and prints
    `{kind, token, card}`. Interactive kinds get a `token`
    (`secrets.token_urlsafe(16)`, or `--token` to pin one); display kinds carry
    `token: null` and fetch live board data via `get_board`. No other IO.
  - `parse-action [PAYLOAD]` — reads the raw callback from the positional
    argument **or** stdin, runs `parse_card_action`, and prints the decision as
    JSON. A foreign card / no-token payload prints `null` and exits `1` so the
    caller can branch on the exit code; invalid JSON raises a `ClickException`.

There is no lark dependency anywhere in this path: `build-card` only emits JSON
and `parse-action` only parses plain dicts.

## Usage

```bash
# Build a card; the printed `token` is embedded in every control. The agent POSTs
# `card` to Feishu's CreateMessage API with msg_type="interactive".
claude-hub feishu build-card --kind approval --title "Deploy v2?" --body "All checks pass."
#  → {"kind":"approval","token":"x9…","card":{…}}

# Free-text input with a custom field name, or a plan confirmation:
claude-hub feishu build-card --kind needs_input --title "Release note?" \
    --body "One line" --field-name note
claude-hub feishu build-card --kind plan_confirm --title T --body "..."

# Display-only cards render live workspace data and carry no token:
claude-hub feishu build-card --kind status --workspace-id ws1
claude-hub feishu build-card --kind task   --workspace-id ws1 --task-id t1

# In the bot's card.action.trigger handler, parse the callback (arg or stdin):
claude-hub feishu parse-action "$CALLBACK_JSON"
echo "$CALLBACK_JSON" | claude-hub feishu parse-action
#  → {"token":"x9…","action":"approve","form":{},"operator_id":"ou_…","chat_id":"oc_…"}
```

The agent keeps the `token` it got from `build-card`, sends the card, and on the
callback matches `parse-action`'s `token` back to the pending card to learn the
human's decision — all inside its own process.

## Smoke Test

No Feishu credentials are needed for the helpers themselves:

1. `claude-hub feishu build-card --kind approval --title T --body B` — prints
   `{"kind":"approval","token":"…","card":{…}}`; the `card` JSON contains
   `hub_token` / `hub_action` in each button's `value`.
2. Construct a `card.action.trigger` callback carrying that `hub_token` and feed
   it to `claude-hub feishu parse-action '<json>'` (or via stdin) — prints
   `{token, action, form, operator_id, chat_id}` and exits 0.
3. Feed a foreign card (no `hub_token`) — prints `null` and exits 1.
4. `build-card --kind status --workspace-id <ws>` renders the live board as a
   card with `token: null` (requires a running backend).

Unit coverage: `tests/test_feishu_parse.py` exercises `parse_card_action` against
raw dicts and attribute objects; `tests/test_feishu_commands.py` covers both CLI
commands.

## Key Pitfalls

- **The agent owns correlation, not Hub.** Since send and receive happen in one
  process, the agent tracks `token → pending card` itself. Hub never sees the
  click, so there is no store to register against and no race to guard.
- **Display kinds carry no token.** `status`/`task` need no response;
  `build-card` prints `token: null` for them.
- **Foreign cards → exit 1.** `parse_card_action` returns `None` unless the
  control's `value` is a dict carrying `hub_token`; the CLI prints `null` and
  exits 1 so unrelated cards are easy to skip.
- **Raw dict OR SDK object.** `parse_card_action` reads fields with both key and
  attribute access, so it works on the raw webhook JSON and on lark-oapi event
  objects alike.
- **No lark dependency.** Neither command imports `lark_oapi`; sending the card
  is the agent's job.

## Files

- `backend/claude_hub/cli/feishu_cards.py` (builders + contract +
  `parse_card_action`)
- `backend/claude_hub/cli/commands/feishu.py` (the `feishu` group: `build-card`,
  `parse-action`)
- `backend/tests/test_feishu_parse.py` (parse function),
  `backend/tests/test_feishu_commands.py` (both commands)
