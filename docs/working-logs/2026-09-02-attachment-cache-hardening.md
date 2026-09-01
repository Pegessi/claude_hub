# Bounded Structured Image History and Cache Hardening

Date: 2026-09-02

## Overview

Structured image turns use two distinct representations:

- the original image is provider input only; Claude keeps it in memory and
  Codex stages it transiently under the isolated runtime temp directory;
- the durable conversation stores only an opaque descriptor and a bounded
  browser-generated preview.

The preview cache is constrained by all of the following defaults:

| Scope | Count limit | Byte limit |
| --- | ---: | ---: |
| one preview | 1 | 512 KiB |
| one session | 200 | 64 MiB |
| all sessions | 2000 | 512 MiB |

Session quota eviction runs before global eviction. Both use immutable
creation time (FIFO); reading old history never refreshes its age. An optional
TTL adds age eviction. Missing or evicted previews preserve the user message
and render a visible placeholder.

This hardening pass also closed three lifecycle gaps:

1. **TTL live enforcement in `save`** — `attachment_max_age_seconds` was
   only enforced during startup `gc`, not during live `save` calls.
2. **Tab-deletion tailer race** — `ttyd_manager.delete_tab` cleared the
   attachment store directly without stopping the in-process tailer, so an
   in-flight send could resurrect deleted previews/events.
3. **Codex inflight image leak on server death** — if the Codex app-server
   died mid-turn (EOF before `turn/completed`), the in-flight image temp
   files leaked until `stop()`.

## 1. TTL Live Enforcement in `save`

### Gap

`attachment_max_age_seconds` was only checked in `gc` (startup recovery).
In a long-lived process, previews older than the TTL would persist until
the next startup GC, even though the setting is documented as a TTL.

### Fix

Added `_evict_aged(self, index) -> bool` to `AgentStreamAttachmentStore`:

- Returns `False` immediately when `max_age_seconds is None` (no scan, no
  extra writes).
- Otherwise iterates the index and `_delete_entry(index, e, persist=False)`
  for entries where `now - created_at > max_age_seconds`.
- Does not persist the index; the caller writes once after all eviction
  passes.

`save`'s eviction block now calls `_evict_aged` before `_evict_session` and
`_evict_global`. `gc`'s inline age-eviction was refactored to call the same
`_evict_aged` helper for consistency.

### Invariant

With `max_age_seconds=None`, an under-quota save writes the manifest exactly
once (publication only). `_evict_aged` is a no-op, so it contributes no
extra write.

### Tests

- `test_save_evicts_entries_older_than_max_age_seconds`: fake clock, save
  `a`, advance past TTL, save `b`, assert `a` is evicted.
- `test_save_with_none_max_age_does_not_evict_by_age`: with
  `max_age_seconds=None`, aged entries survive.
- `test_save_with_max_age_none_incurs_no_extra_index_write`: under-quota
  save with `max_age_seconds=None` writes the manifest exactly once.

## 2. Tab-Deletion Tailer Race

### Gap

`ttyd_manager.delete_tab` called
`AgentStreamAttachmentStore("terminal-tabs", f"terminal-tab-{tab_id}").clear()`
directly. This cleared the preview cache but left the in-process
terminal-tab stream tailer running. If a send was in flight when the tab
was deleted, the tailer could rewrite previews/events after the clear,
resurrecting deleted state.

### Fix

`delete_tab` now delegates to
`discard_session_stream("terminal-tabs", f"terminal-tab-{tab_id}")`.
`discard_session_stream` (in `tailer.py`) guarantees the ordering:

1. `forget_session(session_id)` — stops the tailer (prevents in-flight
   sends from writing further events/previews).
2. `AgentStreamStore(ws, sess).clear()` — clears the event log.
3. `AgentStreamAttachmentStore(ws, sess).clear()` — clears the preview
   cache.

### Test

`test_delete_tab_discards_structured_stream_via_tailer`:

- Mocks `discard_session_stream` and `AgentStreamAttachmentStore.clear`.
- Asserts `delete_tab` calls `discard_session_stream` with
  `("terminal-tabs", f"terminal-tab-{tab_id}")`.
- Asserts `AgentStreamAttachmentStore.clear` is NOT called directly from
  `delete_tab` (the clear happens inside `discard_session_stream`, after
  the tailer is stopped).

## 3. Codex Inflight Image Cleanup on Server Death

### Gap

The Codex app-server is a persistent process. If it dies mid-turn (EOF on
stdout before `turn/completed`), the `turn/completed` handler (which cleans
`_inflight_images`) never runs. The inflight image temp files leaked until
`stop()` was called.

### Fix

The `finally` block of `_drain_stdout` (which runs on EOF or exception)
now cleans both `_inflight_images` and `_staged_images` before signalling
EOF to consumers:

```python
inflight = self._inflight_images
self._inflight_images = []
self._cleanup_images(inflight)
staged = self._staged_images
self._staged_images = []
self._cleanup_images(staged)
```

### Cleanup Paths Summary

| Path | Trigger | Cleans |
| --- | --- | --- |
| `turn/completed` | successful turn | `_inflight_images` |
| `_send_text` exception | turn/start failed | `_inflight_images` |
| `_drain_stdout` finally | server death (EOF) mid-turn | `_inflight_images` + `_staged_images` |
| `stop()` | explicit stop | `_staged_images` + `_inflight_images` |
| `cleanup_codex_temp_dir` | startup | all orphaned temp files |

## Verification

- Backend: 324 focused tests pass across attachment storage/API decoding,
  native transports, streaming, terminal deletion, workspace deletion, and
  orphan reconciliation.
- Frontend: 229 unit tests pass; ESLint and production build succeed.
- `black` and `isort` clean on modified files.
- `mypy`: 0 errors across the eight modified backend source files.
- Browser E2E against the isolated `5275`/`18173` preview confirms that the
  composer clears immediately, the user turn renders a thumbnail, Claude
  receives the original input, and a full page reload restores the same
  attachment URL while positioning the conversation at the bottom.
- Runtime inspection for that E2E turn confirms the durable JSONL contains
  only the attachment descriptor and the cache contains one `41,224`-byte
  `1024x640` JPEG preview; no image data URL is stored in the event log.
