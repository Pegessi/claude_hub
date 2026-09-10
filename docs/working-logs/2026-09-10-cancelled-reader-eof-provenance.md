# Cancelled Chat reader EOF provenance

Date: 2026-09-10

## System overview

Top-level Claude and Cursor Chat sessions launch one provider subprocess per
turn. A long-lived `SessionTailer` consumes every subprocess through one
transport queue and assigns normalized records to the currently active
`turn_id` and `run_epoch`.

A one-shot reader that is cancelled cannot simply stop: a consumer parked in
`read_line` would never wake. Its `finally` therefore publishes an EOF sentinel.
That sentinel reports *our* cancellation, not the provider's end of stream, and
the two are indistinguishable by value — `None` either way. The generation tag
is the only witness: a sentinel is authoritative only while its generation is
still the live one.

## Symptom

A Chat turn rendered its complete answer, then reported `provider exited
without a completion record` and was marked failed. Evidence trail:

- Provider side (the CLI's own transcript): `stop_reason=end_turn`, the full
  2497-character answer, token usage recorded, and the `Stop` hook exiting 0.
  The provider had succeeded.
- Hub side (the persisted event stream): the turn's last `text_delta` was
  missing its final character — `…旧 worktree` where the CLI had persisted
  `…旧 worktree？` — followed 12 ms later by the synthetic `error` and
  `turn_completed(status=failed)`.
- `backend.log` had nothing. `_fail_active_turn` and its EOF caller both log
  nothing on the fallback path, so the only surviving trace was the synthesized
  event itself.

The missing final delta is the tell: the consumer's stream was cut at the tail,
not answered badly. The provider kept producing after the Hub stopped listening.

## Root cause

`ProviderSession.stop()` called `_terminate_process()` without retiring the
stdout generation:

```python
async def stop(self) -> None:
    self._started = False
    await self._terminate_process()   # cancels the reader; generation untouched
    self._end_turn()
```

`_terminate_process` cancels the reader, whose `finally` publishes
`(generation, None)` tagged with **that reader's own generation** — which is
still live, because nothing advanced it. `read_line` matched the tag and
returned `None`, the tailer read it as the provider's end of stream, saw no
terminal completion record, and synthesized a failed turn.

The other two callers had each remembered to advance the generation first
(`cancel_active_turn`, `_spawn_oneshot`). That is the defect: the invariant was
maintained by callers remembering, not by the code that depends on it.

## Module design

Retirement moved into the code that does the killing:

```python
def _invalidate_stdout_stream(self) -> int:
    self._stdout_generation += 1
    return self._stdout_generation

async def _terminate_process(self) -> None:
    # Retire the reader BEFORE cancelling it: its ``finally`` runs while we
    # await it below, so the sentinel it publishes must already be stale.
    self._invalidate_stdout_stream()
    ...
```

- `cancel_active_turn` and `_spawn_oneshot` drop their explicit advances. The
  advance per spawn stays exactly one, which matters because
  `CursorNativeSession._send_text` predicts the next generation *before*
  spawning to register staged image files under it — the drain deletes only its
  own generation's files.
- `stop()` needs no change: it now retires the reader it kills by construction.
- Codex is unaffected. It is a persistent JSON-RPC app-server whose `read_line`
  reads a per-process notification queue and never consults
  `_stdout_generation`, so the extra advance is inert there.

Every caller of `_terminate_process` either installs a replacement reader,
breaks out of its consume loop, or cancels the consumer, so no consumer is left
parked on a stream that will never speak again — which is what lets the
sentinel be unconditionally stale.

## Key issues / pitfalls

- A bare EOF is not sufficient provenance when one consumer queue is shared by
  several process lifecycles. This is the same lesson as
  `2026-09-04-chat-one-shot-generation-isolation.md`, on the sibling path: that
  fix covered the *next* turn reading a superseded sentinel; this one covers the
  *current* turn reading a sentinel from a reader we killed underneath it.
- An invariant that two of three callers uphold is not an invariant. Move it
  into the operation, not the call sites.
- The fallback branch was silent, which is why a 4.2 GB `backend.log` held no
  evidence. A synthesized failure that the operator cannot trace is worse than
  the failure. It now logs the exit error.

## Validation

- Red: `test_stop_while_turn_in_flight_does_not_surface_cancellation_as_eof`
  fails on the unfixed transport with `assert 1 < 1` — the sentinel still
  carries the live generation.
- Green: the same test passes; `test_agent_stream.py` +
  `test_agent_stream_native.py` 171 passed.
- Full backend suite (browser-driven E2E excluded): 1358 passed, 1 skipped,
  1 failed — `test_recovery_real_ttyd.py::test_real_cold_restart_7tab_bijection`
  (`post-restart codex-A1: ttyd process missing`). That failure reproduces
  identically with this change stashed, so it pre-exists and is unrelated.
- `black`, `isort`, and `mypy claude_hub` (94 files) clean.

## Follow-up

The trigger of the original incident was not proven from the artifacts: the fix
closes a class of it (`stop()` mid-turn) and makes the next occurrence legible
via the new log line. If it recurs, `exit_error=None` plus a retired-generation
check will separate "provider really closed stdout" from "we killed the reader".
