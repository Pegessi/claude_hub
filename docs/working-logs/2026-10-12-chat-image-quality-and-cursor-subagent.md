# Chat image quality, 1 GiB LRU image cache, and Cursor sub-agent cards

Task `ab216dae`: images sent from Chat looked heavily compressed, the local
image cache should be LRU with a 1 GiB default, and Cursor's native
sub-agents did not show their work.

## Image quality

The image a Chat shows after sending is the attachment cache copy, not the
original. The composer produced it by resampling to a 1024px edge and encoding
JPEG at 0.85, lowering quality until it fit 512 KiB. Screenshots and PNGs lost
most of their detail; the provider always received the original.

The composer now stores the original itself whenever it fits the stored-image
bounds: 8 MiB (the composer's own upload limit) and an 8192px edge. Only images
above the edge bound are re-encoded: scaled to fit the edge and the 16.7M-pixel
canvas limit of mobile Safari (dimensions are floored, so rounding cannot
exceed it), then PNG if it fits, otherwise WebP 0.95, JPEG 0.92, WebP 0.85,
JPEG 0.85. Encoders the browser lacks are skipped by checking the blob type,
and the size is halved only when none fits. GIFs stay animated because the
original bytes are kept.

The backend dimension bound moves to 8192px and the per-image cap to 8 MiB.
When the original is its own stored image, the composer sends
`preview_is_original: true` instead of a second base64 copy, and
`_decode_attachments` reuses the decoded original, so the request carries and
counts those bytes once. A copy in both fields would double the upload and
could push a 40 MiB request past the Docker nginx 100 MiB body limit. Setting
both fields is rejected. The composer checks the 40 MiB request total against
the real size of each re-encoded preview once it exists, rather than a fixed
512 KiB reserve. Display thumbnails now load lazily and decode asynchronously.

This changes the privacy note: the local cache now holds originals rather than
1024px previews. They stay under the existing state-root manifest, the per
session directories, and the same eviction and deletion paths.

## LRU cache with a 1 GiB default

Eviction was FIFO by creation time with a 64 MiB/200 session and 512 MiB/2000
global quota. The manifest entry now carries `last_used_at`: set on save and
refreshed by reads at most once per hour, so browsing history does not rewrite
the manifest on every image. Older entries fall back to `created_at`. Session
and global eviction sort by `(last_used_at, created_at)`.

Defaults are one 1 GiB pool: global 1 GiB/5000 images, and the session quota
equals the global one so a single long Chat may use the whole pool. Override
with `ATTACHMENT_MAX_GLOBAL_BYTES`, `ATTACHMENT_MAX_SESSION_BYTES` and the
matching `*_COUNT` settings.

## Cursor native sub-agents

Cursor CLI `2026.10.01-e373342` offers only `--print --output-format
stream-json`; it has no app-server or ACP mode. Its stream emits a `Task` start
and completion. Nothing about the child's own tool calls or messages arrives
between them, and the parent transcript does not contain them either. Live
child activity is therefore not available from the CLI integration; the Cursor
SDK may expose it, and that would be a separate integration.

What was missing was rendering what the stream does carry:

- `subagentType` is now a protobuf oneof object such as `{explore: {}}`, which
  the parser rejected as a non-string, so the card lost its type. It now takes
  the single key, and plain strings from older builds still work.
- The completion `result` is a record. `payloadString` dropped it, so the
  card had no report. `subagentResultText` joins
  `success.conversationSteps[].assistantMessage.text`, falls back to the
  `error`, `failure` or `rejected` message, and leaves non-Cursor results to
  the existing string path. Old history renders correctly because the fix is in
  the reducer.

## Validation

- `./scripts/verify.sh all`: docs, backend-format, backend-types,
  frontend-lint, frontend-types, frontend-tests and frontend-build passed.
  backend-tests stopped at
  `test_queue_status_wedge.py::test_queue_then_generation_completes_normally`.
  That test is flaky without this change: it failed 2 of 8 isolated runs on
  `codex/task-recover-dialog-reporter-key`, which shares this branch's base
  and does not contain it. The full backend suite with it and the known
  `test_service_restart.py::test_frontend_build_timeout_keeps_current_backend_and_dist`
  flake deselected passed (2812 passed, 19 skipped). An earlier full run of
  this branch passed the whole backend suite.
- `tests/test_agent_stream_attachments.py` covers default limits, read
  recency, the hourly touch interval, legacy manifests, a flagged original used
  as its own preview beside a re-encoded one, and rejection of mixed or doubly
  set previews.
- `agentStreamAttachments.test.mjs`, `subagentTool.test.mjs` and
  `agentStreamTimelineReducer.test.mjs` cover the original pass-through, PNG at
  the edge bound, the canvas limit and encoder fallback, floored dimensions,
  halving, the send wire form and request byte count, the oneof type, and the
  Cursor report. Over a grid of 47,011 sizes past the edge bound, the previous
  rounding exceeded the canvas limit for 23,063; flooring exceeds it for none.
- `frontend/tests/browser_chat_image_quality.py` against an owned Vite server
  with mocked APIs: a 1600x1000 PNG is sent once with `preview_is_original` and
  no `preview_data_url`; a 9000x40 PNG gets an 8192x36 PNG preview; a Cursor
  `Task` card shows `explore` and the child report.
- `test_agent_stream.py` run after `test_agent_stream_attachments.py` fails two
  `set_env` tests with "no current event loop" on `main` too; in the default
  order both files pass.
