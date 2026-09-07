# 2026-09-07 — Cursor Chat Image Attachments

## System overview

The Cursor Chat composer rejected image paste with "This chat does not
support image attachments." The gate is a single capability flag:

```text
StructuredPane.vue supportsImages  ←  caps.supports_images  ←  ProviderSession.supports_images
```

Claude and Codex set `supports_images = True`; Cursor was the only provider
with `False`. The frontend attach button is `:disabled="!supportsImages"` and
the paste handler short-circuits with the rejection message when the cap is
false, so flipping the backend flag opens the whole pipeline (the API gate at
`agent_stream.py` passes; no API change is needed).

The blocker was transport: Cursor's native CLI
(`agent --trust --print --output-format stream-json --stream-partial-output`)
has **no structured image-input flag** — no `--image`, no `--attach`, no ACP
attachment contract. The prompt is plain text on stdin.

## Feasibility (validated empirically)

The fix is a **file-reference + Read-tool** transport: persist the image bytes
to a temp file, reference the absolute path in a sentinel-wrapped prompt block,
and let Cursor's multimodal Read tool read it. This was de-risked before any
production code by probing the real CLI:

- Image **inside** the workspace → the agent emitted a `readToolCall` for the
  path and answered correctly ("Red").
- Image **outside** the workspace (`/tmp/cursor-img-external/red.png`) → the
  agent read it and answered correctly ("Red", exit 0).

The outside-workspace case is the load-bearing one: it proves the temp files
can live in an app-owned runtime directory (`runtime_home/tmp/cursor-images/`)
that is not the session workspace.

## Module design

### `native.py`

- **Sentinel wrap/strip** (mirrors the question-protocol block):
  `wrap_image_attachment_guidance(text, image_paths)` prepends
  `<<<HUB_IMAGE_ATTACHMENT_V1>>>…<<<END_HUB_IMAGE_ATTACHMENT_V1>>>` listing each
  absolute path with guidance to Read them before answering.
  `strip_image_attachment_guidance(text)` removes it (no-op on absent/malformed
  block). The two strips are independent and compose in either order.
- **Temp directory**: `_cursor_image_temp_dir()` returns
  `runtime_home/tmp/cursor-images/`, created mode 0700 (each parent component
  chmoded to defeat umask). `cleanup_cursor_temp_dir(max_age_seconds=None)`
  removes leftover files; the default removes *all* (startup single-ownership
  guarantee), a bounded age is a mid-run safety net.
- **`CursorNativeSession`**:
  - `supports_images = True`.
  - `_stage_images(images)` — validate magic bytes (`_detect_image_mime`),
    write each to a 0600 `cursor-img-*` file in the temp dir, append to
    `_staged_images`.
  - `_send_text(text)` — wrap the question-protocol guidance, then (if images
    were staged) the image block; transfer `_staged_images` → `_inflight_images`
    **before** spawning; spawn the one-shot. On spawn failure, clean up the
    inflight files (no process will read them).
  - `_drain_oneshot_stdout` override — `await super()` in a `try/finally` that
    calls `_clear_inflight_images()`. The base drain awaits `proc.wait()`
    before returning on natural EOF, so by the time the `finally` runs the
    process has exited and finished reading the files.
  - `stop()` — clears both staged and inflight images.

### `cursor_cli_transcript.py`

The stream-json stdout `type:user` echo is already dropped by the normalizer,
and Hub persists its own clean `TURN_STARTED` before `send_message`. The only
path that persists the raw prompt is the transcript-file `role:user` branch,
which now calls `strip_image_attachment_guidance` after the existing
`strip_question_protocol_guidance`.

### `main.py`

A startup block calls `cleanup_cursor_temp_dir()` after the Codex equivalent,
under the same `BackendInstanceLock` single-ownership rationale (before any tab
can stage new files).

## Key issues / pitfalls

- **Fire-and-forget spawn.** `_spawn_oneshot` returns as soon as stdin is
  written; the agent runs in a background `_drain_oneshot_stdout` task and
  reads the image file *later*. Deleting the file after `_send_text` returns
  would race the Read. Hence the two-list design (`_staged_images` for the next
  turn, `_inflight_images` for the running process) and the drain `finally`.
  The drain only ever clears `_inflight_images`, so a new turn's
  `_staged_images` is never touched by a prior turn's cleanup.
- **Private files, app-owned lifecycle.** `mkstemp(dir=…)` keeps the files
  under the 0700 runtime dir (not the system temp dir, not the durable
  workspace `STATE_ROOT`), chmoded 0600. Original image bytes never land in
  durable/backup state, and no other host user can read them.
- **Timeline hygiene.** The injected paths must never reach the persisted
  timeline or the UI. The strip is applied on transcript read; the stdout
  user-echo is already dropped. Both sentinel strips compose, so a prompt
  carrying both blocks is cleaned regardless of order.
- **Capability gate is one flag.** No API/frontend change was needed — the
  frontend already renders the attach control and paste handler from
  `caps.supports_images`. The static frontend test asserting the fallback
  string still passes (the string remains for any provider whose cap is false).

## Validation

- Backend: black, isort, mypy clean; `pytest` 1328 passed (the 2 failures are
  the pre-existing environmental ttyd/Chromium e2e tests, orthogonal to this
  change). New tests cover wrap/strip round-trip, no-op/malformed strip,
  sentinel composition, invalid-image turn-guard reset, private-file staging,
  the full `send_message` → EOF cleanup lifecycle, and startup cleanup.
- Frontend: ESLint, `vue-tsc` typecheck, production build, and 276 unit tests
  pass.
