# 2026-11-17 The chat reply that stopped at 4,000 characters

## Symptom

The 16:25 CST answer in tab `362cda75` (Maso session `004e986c-bd6`, cwd
`/home/tiger/evo_workspace`) ended mid-sentence with `` `mega_bridge.cpp` 与
`input_v…[truncated] `` in the Chat bubble, in Copy and in "Fork from here".

The diagnosis task (`0befe8fc`) proved the cut is write-side: redaction runs
before the SQLite write *and* before SSE fanout, and history reads serve the
stored row verbatim, so the tail never reached the Hub or the browser.

## What was actually stored

Decoding the stream (zlib + per-stream zdict) for turn `313a9e25`:

| seq | kind | stored | provider truth |
| --- | --- | --- | --- |
| 3042..3050 | `text_delta` | 492 chars, intact | provider chars 0..492 |
| 3051 | `text_delta` (one coarse provider block) | 3,988 chars + marker | provider chars 492..10,583 |
| 3052 | `turn_completed.assistant_text` | 3,988 chars + marker | the whole 10,583-char message |

`~/.maso/sessions/004e986c-bd6/index.json` `messages[115]` holds the message at
10,583 chars, and the surviving pieces line up with it exactly: 492 + 3,988 =
4,480 rendered chars, 6,103 lost. The pre-truncation text of seq 3051 is
recoverable because it is just `provider[492:]`.

## Fix: a per-field budget, not one global cap

`services/agent_stream/redaction.py` truncated *every* string field at
`MAX_FIELD_CHARS = 4000`. It now keeps a table of the payload keys that carry
chat text and gives only those a larger budget:

```python
MAX_FIELD_CHARS = 4000          # tool args/results, status/error text, nested fields
MAX_CHAT_TEXT_CHARS = 128_000   # chat turn text
_CHAT_TEXT_KEYS = {
    TEXT_DELTA:        {"text"},
    THINKING_DELTA:    {"text"},
    TURN_STARTED:      {"summary"},
    TURN_COMPLETED:    {"text", "assistant_text", "final_text"},
}
```

Rules that keep the widening narrow:

- Only the **top level** of the payload is widened. `_redact_value()` recurses
  with its default limit, so a long `metadata.note` or `attachments[i]` string
  still stops at 4,000 and cannot ride along with a turn.
- Secret masking (`_TOKEN_PATTERNS`) and provider-proxy diagnostic scrubbing run
  exactly as before; a 50,000-char answer with a token in it is still masked.
- `status`/`error` `text`, tool `args`/`result` and every other field keep the
  4,000-char cap, so the stored-event bound that cap exists for is unchanged
  except for reader-visible chat text.

The same 4,000 cap was also cutting `turn_completed.assistant_text`/`final_text`
(Feishu delivery, journal, the delivery-length backstop) and
`turn_started.summary` (fork seed, edit-resend prefill). Those are chat text
too, so they move to the same budget; `stored_text_incomplete` now only fires
for a masked secret or a message past 128,000 chars.

## Tests that encoded the old cap

Four files assumed "long chat text gets truncated at 4,000":

- `test_agent_stream.py`: `test_redact_event_truncates_long_fields` became seven
  cases — long chat text survives whole (10,583 / 20,000 / 50,000 chars, every
  chat key), oversized chat text is still bounded at 128,000 with the marker,
  secrets inside long text are still masked, tool args/results and `status`/
  `turn_completed` non-text fields still truncate at 4,000, nested fields stay
  on 4,000.
- `test_agent_stream_attachments.py`: the masked-summary case now exceeds
  128,000 chars; a new case proves a 20,000-char user message is stored whole
  with `stored_text_incomplete = False`.
- `test_edit_resend_recovery.py`: `_redacted_summary` models the summary budget.
- `test_agent_stream_agent_image.py`: `…_before_truncated_completion` relied on
  a 4,500-char answer overflowing the old cap. It now fills the chat budget
  exactly (64,000 + 64,000) so the trailing image link still lands beyond it —
  the property under test (retention must come from the deltas, not from the
  completion summary) is unchanged.

Stale "4000 chars" comments in `runtime/input.py`, `transcript_fork.py`,
`frontend/src/utils/chatEditResend.ts` and `frontend/src/features/chat/timeline/
types.ts` were updated with the behaviour.

## Backfilling the damaged turn

`/tmp/trunc-diag/backfill.py` (dry run by default, `--write` to apply):

1. Reads the provider message, the 9 preceding deltas (492 chars) and both
   target rows.
2. Asserts `provider.startswith(prefix)`, that each stored value is
   `expected[:3988] + "…[truncated]"` — a mismatch aborts instead of guessing.
3. Sets seq 3051 `text` to `provider[492:]` (**10,091** chars — the delta's real
   value, not the whole message: the reducer *appends* a delta, so the full text
   would duplicate the 492-char prefix) and seq 3052 `assistant_text` to the
   whole **10,583** chars.
4. Re-packs both rows with the stream's `zlib.compressobj(6, zdict=dictionary)`
   (the same bytes `local_storage.pack` produces), backs up the original bodies
   as base64 JSON plus a full DB copy to `/tmp/trunc-diag/backup/`, then decodes
   the rows back and re-derives the message: the accumulated deltas equal the
   provider text exactly, envelopes and remaining payload keys unchanged.

`pragma quick_check` stays `ok`. Replaying the served (compacted) events for
that turn through the real frontend reducer (`frontend/tests/
chatPresentationTestHarness.mjs`) shows the before/after:

```
before: lastPart=4493 marker=true  endsWithProviderTail=false
after:  lastPart=10584 marker=false last text part equals provider message: true
```

(10,584 = the message's UTF-16 length in JS; 10,583 code points.)

## Residual risks

- The backfill rewrites two historical rows in the live `storage.sqlite3`. The
  row backup is authoritative for a revert; the running Hub serves them on the
  next history read, but a browser with cached history may need a reload.
- Every already-truncated row elsewhere (83 `text_delta`, 276 `thinking_delta`,
  494 `turn_completed`, 468 `turn_started` per the diagnosis) is still cut. The
  budget stops new damage; recovering the rest needs the same
  provider-transcript rehydrate per session.
- 128,000 chars is a bound, not a guarantee: a single answer longer than that is
  still cut (now with a marker the reader can see, which earlier was also true —
  only far sooner).
