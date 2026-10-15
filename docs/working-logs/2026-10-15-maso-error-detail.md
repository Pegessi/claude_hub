# MASO error events were unreadable in Chat

## Symptom

A MASO Chat (tab `ec36ef58-e9f4-4b38-951c-5936dd3a4032`) showed a red error
banner whose entire content was:

```
{'sequence_number': 1, 'type': 'error'}
```

The reader could not tell what failed or what to do. The session carried two
such events (`stream_sequence` 573 and 818), each immediately followed by
`turn_completed` with `status=failed, stop_reason=error`.

## Root cause

Not a Hub serialization bug. The chain, confirmed in the installed
`maso_cowork`/`maso_core` 0.7.3 wheels:

1. `maso_core/llm/responses_errors.py:raise_for_response_error` turns a provider
   SSE `error` event into `raise RuntimeError(_raw(response))` — the exception
   argument is the frame **mapping**, not a string. With
   `model_dump(exclude_unset=True)` an upstream frame that set no description
   fields reduces to `{'sequence_number': 1, 'type': 'error'}`.
2. `maso_core/agent.py:519` catches it, sets `stop_reason = error`, and fires the
   `agent_error` hook with the exception object.
3. `maso_cowork/server/plugin.py:750` emits
   `{'agent_id', 'error': str(error), 'stop_reason', 'continue_state'}`. `str()`
   of a `RuntimeError` holding a dict is that dict's Python repr.

So MASO really does send a string, and Hub forwarded it faithfully. Two Hub
defects remain, and they are what this change fixes:

- `maso_events.py` read only `data["error"]` and used it as the whole message.
  When the "description" is a repr of a mapping that has no description, the
  user is handed noise as if it were the diagnosis.
- Both branches used `str(...)`, so a future non-string `error` would leak a
  Python repr through Hub itself.

`redacted: true` is only the marker `redaction.py` stamps on every copy it
returns. `_redact_value` does not touch this message: the key `message` does not
match the sensitive-key pattern, no token pattern matches, and the string is far
below `MAX_FIELD_CHARS`. Verified by a test that compares the redacted payload
with the original.

## What MASO actually sends on `agent_error`

| Field | Type | Range observed in 0.7.3 |
| --- | --- | --- |
| `agent_id` | str | root id, or a child id; Hub already routes on it |
| `error` | str | `str(exception)`; a provider frame's repr when the exception argument was a mapping |
| `stop_reason` | str | `completed` / `max_turns` / `aborted` / `error` by declaration, but **always `error`** at this hook — both call sites set it before firing |
| `continue_state` | dict | `{can_continue, reason}` for MASO's own Continue button, which Hub does not expose |

`continue_state` is therefore not surfaced: describing a control Hub does not
have would mislead. `stop_reason` is kept in the payload and named in the
sentence only when it is not `error`, because repeating `error` inside "MASO run
failed" tells the reader nothing — and because the contract may widen.

## Behavior after the fix

The adapter splits `error` into a description and verbatim evidence:

- A mapping, or a string that parses as one, yields its first readable
  `message` / `error` / `detail` / `reason` / `description` field (up to three
  levels deep) and keeps the whole frame as compact JSON in `detail`.
- An ordinary string stays the description, unchanged, with no `detail`.
- Anything else contributes no description and is rendered as JSON, never a
  repr.

The reported input now produces:

```
MASO run failed and reported no error description. Retry the turn; if it
repeats, check the MASO service logs.
{"sequence_number": 1, "type": "error"}
```

A frame that does carry a description produces
`MASO run failed: Rate limit exceeded` with
`{"code": 2005, "message": "Rate limit exceeded"}` as evidence. A child agent
keeps its `status` event and the same wording (`MASO sub-agent failed …`),
without the retry advice, which the parent Chat cannot act on for a child.

Advice is attached only to the no-description case: when Hub has nothing to
report, the only honest statement is that the cause is in MASO's logs. Hub never
claims to know why the run failed.

The frontend error part gained an optional `detail`, rendered as a muted,
wrapping monospace line under the message so the statement is read first.

## Boundaries

- Hub cannot recover information the provider never sent. No wording implies
  otherwise.
- Fields are treated as independently unreliable: a missing, empty or
  non-string `stop_reason` is dropped rather than defaulted, and the evidence is
  reported even when no description accompanies it.
- `detail` is attached to the child `status` payload too, but only the error
  banner renders it; the status line stays one line.
- `hub_transport_error` is unchanged — its message is Hub's own constant.

## Validation

Worktree `~/claude_hub_worktree/maso-error-detail`, branch
`fix/maso-error-detail`, base `45c552c`.

- `./scripts/verify.sh backend-format` — pass
- `./scripts/verify.sh backend-types` — pass, 253 source files
- `./scripts/verify.sh backend-tests` (default scope) — 3331 passed, 7 skipped
- `./scripts/verify.sh frontend-lint`, `frontend-types`, `frontend-tests`
  (785 passed), `frontend-build` — pass
- Gate discrimination: with `maso_events.py` reverted, 9 of 13 new backend
  tests fail; with both frontend files reverted, 3 of 4 new frontend tests
  fail. Both reverts were restored and the sources checksum-compared.

Not run: the mocked browser gate (`browser-mocks`), which needs an installed
Chromium and owned ports, and `verify.sh all`. The banner change is covered by
DOM-source and reducer assertions only — no rendered-layout evidence.
