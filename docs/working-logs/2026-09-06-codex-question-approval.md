# Codex requestUserInput question cards

Date: 2026-09-06

## Overview

Native Codex (app-server 0.153.x) can block the in-flight turn on a
server→client question: it sends a JSON-RPC **request**
`item/tool/requestUserInput` (legacy alias `tool/requestUserInput`) and waits
for a matching JSON-RPC **response** before continuing. The structured Chat UI
now surfaces these as the same interactive approval card used for Cursor
`AskQuestion`, and routes the user's selection back as the blocking response —
unblocking the turn instead of cancelling it.

This is the Codex counterpart to the Cursor `AskQuestion` card
([2026-09-02-chat-composer-ux-and-ask-question.md](2026-09-02-chat-composer-ux-and-ask-question.md)).
The difference: Cursor native uses one-shot `--print` subprocesses, so its
answer is delivered as a steer + follow-up message. Codex holds a persistent
app-server process, so the answer is delivered **on the same stdin** as the
blocking tool result — a cleaner, non-cancelling round trip.

## Wire contract

App-server → client (blocking request):

```json
{"jsonrpc":"2.0","id":42,"method":"item/tool/requestUserInput",
 "params":{"itemId":"it-9","threadId":"…","turnId":"…",
   "questions":[{"id":"q1","header":"Color","question":"Pick a color",
     "options":[{"label":"red"},{"label":"blue"}],"multiSelect":true}]}}
```

Client → app-server (answer response):

```json
{"jsonrpc":"2.0","id":42,"result":{"answers":{"q1":{"answers":["red","blue"]}}}}
```

Dismissal answers with `{"answers":{}}`. The turn stays blocked until this
response arrives; there is no completion event for a blocking request.

## Backend

### `native.py` — transport

- **Three-way stdout dispatch (latent bug fix).** `_drain_stdout` dispatched on
  `if "id" in record: <response> else: <notification>`. A server→client request
  has **both** `id` and `method`, so it was misclassified as a response, found
  no pending future, and was silently **dropped** — the app-server would hang
  forever. Now: `id`+`method` → server request; `id`+no `method` → response;
  no `id` → notification. Both the main loop and the trailing partial-line
  handler use the same discriminator.
- `_handle_server_request` stashes a `requestUserInput` request in
  `_pending_questions` (keyed by the server's JSON-RPC id) and forwards a
  synthetic notification `{method, params}` so the adapter emits the approval
  card. Any other server request gets `-32601 method not found` so the
  app-server never hangs on an unknown method.
- `answer_pending_question(answers)` maps the composer's
  `[{questionId, selected:[labels]}]` to `{answers:{questionId:{answers:[labels]}}}`
  and writes it as the JSON-RPC response for **every** pending question, then
  clears the pending map. Empty answers → `{"answers":{}}`. Returns `False`
  when nothing is pending so the caller falls through to normal delivery.
- Base `ProviderSession.answer_pending_question` returns `False` (Claude/Cursor
  have no blocking-question channel).
- `stop()` clears `_pending_questions` — unanswered questions die with the
  app-server process.
- `supports_approval_ui = True` on `CodexNativeSession` (was implicitly off).
- `parse_ask_question_response(text)` recognizes the composer's
  `{"type":"ask_question_response",…}` JSON payload and returns the answers
  list, else `None`.

### `codex_jsonl.py` — adapter

- `_normalize_notification` maps `requestUserInput` (both method aliases) via
  `_normalize_question`, which emits `TOOL_CALL_STARTED` (name
  `request_user_input`, `call_id=str(itemId)`) followed by `APPROVAL_REQUIRED`
  (`kind:"ask_question"`, `title` = first prompt, `questions`).
- `_codex_normalize_questions` maps Codex `{id, header, question,
  options:[{label}], multiSelect}` → shared card `{id, prompt,
  options:[{id:label,label}], allow_multiple}`. `prompt` falls back to
  `header`; entries missing id/prompt/options are skipped. **Option ids are the
  labels themselves**, so a selected label is also the answer value.
- `supports_approval_ui = True` on the adapter.

### `tailer.py` — answer routing

- In `send_message`, **before** the busy-check/steer guard (which would
  `turn/cancel` the blocked turn) and before the send lock, a text payload that
  parses as an `ask_question_response` is routed to
  `transport.answer_pending_question`. If the transport consumes it (a question
  was pending), `send_message` returns early — no `turn/start`, no steer. The
  `not images` guard lets a malformed answer + images request fall through to
  normal image handling rather than dropping the images.

## Frontend

- `agentStreamTimeline.ts`: `request_user_input` is excluded from `tool_group`
  parts (same as `AskQuestion`), so the raw tool row is hidden and only the
  approval card shows. No `tool_call_completed` mapping is needed — Codex emits
  no completion for a blocking request, and the hidden `running` tool is
  invisible (tools render only via `tool_group` parts).

## Key issues / pitfalls

- **The `_drain_stdout` drop was the load-bearing fix.** Without the three-way
  dispatch, the question request never reached the adapter and the app-server
  blocked indefinitely. Discriminator: JSON-RPC responses have `id` but never
  `method`; server requests have both; notifications have `method` but no `id`.
- **Answer must bypass the busy-check.** The blocked turn is "in flight", so
  the normal guard would raise or steer-cancel it. Intercepting before the lock
  delivers the answer as the tool result the turn is waiting for.
- **Import direction is acyclic.** `codex_jsonl.py` imports
  `_CODEX_QUESTION_METHODS` from `native.py`; `native.py` imports only
  `…models`. The reverse would cycle through `ttyd_manager.py`.
- **stdin write convention.** `answer_pending_question` writes without
  `_send_lock`, matching `cancel_active_turn` (single small JSON line; the turn
  is blocked so there is no concurrent `turn/start`).

## Review fixes (post-implementation)

An independent adversarial review of `2e16c6d` found one blocking regression and
two robustness gaps; all are fixed on the branch.

- **Blocking — answering a question locked the composer forever.** The answer
  is intercepted in `send_message` *before* `turn_started` is published, so no
  authoritative turn ever carries the answer's `client_turn_id`. But the
  frontend `submit()` unconditionally pushed an optimistic pending turn keyed
  by that id; the only removal path is a watcher that drops a pending turn once
  its id appears in an authoritative turn. The bubble therefore stayed pinned
  forever (showing the raw JSON answer), and `turnInFlight`
  (`isChatModeLocked(pendingDirectTurns.length > 0, …)`) stayed `true`, so
  every subsequent send was silently queued and never flushed. Fix: an answer
  (`messageOverride`) is not a new turn — skip the optimistic push. The card is
  already marked resolved by `submitQuestionResponse` as the visual ack, and
  `turnInFlight` stays correctly `true` from the blocked authoritative turn
  until its `turn/completed` arrives. Claude/Cursor are unaffected (their
  answer goes through the full steer path and publishes `turn_started`).
- **Race — concurrent `answer_pending_question` calls could double-answer.**
  The old loop popped one id per iteration then `await`ed the write, so a
  second call could observe a partially-popped map and re-send for the
  remaining ids. Fix: snapshot + `clear()` before any await (atomic — no await
  between the check and the clear), then drain the snapshot. Exactly one call
  drains; the other sees an empty map and returns `False`.
- **Stale state — `cancel_active_turn` left `_pending_questions` populated.**
  Stopping while a question is pending killed the blocked turn but left the
  request id in the map, so the next turn's answer was also sent to a dead
  request. Fix: `cancel_active_turn` clears the map (the app-server is no
  longer waiting on that id after `turn/cancel`).

### Deferred (non-blocking, pre-existing pattern)

- **Answer is not durably persisted.** No `turn_started`/`approval_resolved`
  is recorded for the answer, so on reload the card renders as open inside a
  completed turn. Re-submitting a stale card then sends the raw JSON as a
  genuine new turn (nothing pending → falls through to normal delivery). Same
  class of stale-card issue exists for Claude; a follow-up should emit an
  `approval_resolved` event or mark the card resolved durably.
- **All-skipped question hangs silently.** If every question in a request is
  skipped by the adapter (e.g. all have empty `options`), no card is emitted
  but the request stays pending and the turn blocks with no UI; only Stop
  recovers it. Degenerate input (Codex always sends options); auto-dismissing
  with `{"answers":{}}` would be more robust.

## Validation

- Backend: `test_agent_stream_native.py` (transport: stash/forward, answer
  response shape, dismissal, no-pending `False`, unknown-method `-32601`, base
  `False`, parser, **concurrent-answer no-double-answer**, **cancel clears
  pending questions**) + `test_agent_stream.py` (adapter: card emission, legacy
  alias, multiSelect/header fallback, invalid-entry skip). 37 passed in the
  targeted native+adapter run. The concurrent-answer test forces a yield in the
  fake stdin `drain()` (the real `StreamWriter.drain` yields mid-write) so the
  two calls actually interleave; verified it FAILS on the pre-fix code
  (`assert 2 == 1` — both calls drained, double-answering id 43) and PASSES on
  the fix. Full suite (excluding the Playwright/tmux E2E files CI also
  ignores): 1271 passed, 13 failed — all 13 are the documented environmental
  `tmux session not created` / real-ttyd failures, none in `agent_stream`.
- `black --check`, `isort --check-only`, `mypy` clean on the changed source.
  (black also reformatted a pre-existing drift in `test_agent_stream_native.py`
  at the `test_codex_model_switch_via_update_env` test — present on `main`
  `fa2153c`, not introduced here.)
- Frontend: `agentStreamTimeline.test.mjs` — `request_user_input` hidden from
  `tool_group` and renders an approval card; does not merge with adjacent tool
  groups. 270 passed across the full `tests/*.test.mjs` suite. `eslint`,
  `vue-tsc` (via `pnpm build`), and `vite build` clean. The composer-lock fix
  is in `StructuredPane.vue`'s `submit()` closure (not unit-tested directly);
  verified by the typecheck/build passing and the failure-chain trace above.
