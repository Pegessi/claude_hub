# 2026-11-08 fix intermittent Image unavailable — completed-tool image grants and retry

Task `2f873001-3260-4cc3-8304-5aed9f4005b6`, branch
`codex/fix-image-unavailable-20261108`, base main
`356d07a0dfe0eb9efeaac1369ce4e81cafc77d40` (== origin/main at fetch).
Sole author worktree:
`/home/tiger/claude_hub_worktree/rv-image-unavailable-20261108`. The earlier
integration candidate and the separate HTML-popup gate investigation remain
frozen and untouched.

## Problem (bounded evidence)

- 26 sampled active-Chat image requests: 25 HTTP 200, 1 HTTP 404; 14
  selected histories fully paged; 76 per-Chat image paths inventoried
  (`image-unavailable-fix-20261108/history-image-inventory.json`,
  `selected-chats.json`).
- Confirmed historical failure
  (`historical-failing-tool-metadata.json`,
  `historical-image-probes.json`): a canonical `Read`
  (`call_o3w05qapqqfpi0hvzvg9bjpd`) started and completed successfully at
  stream sequences 3087/3088 for an image that still exists (94,761 bytes),
  but the current `agent-image` GET returns 404 because the file is outside
  the reader session's cwd and outside the temporary-file grant.
- Root mechanism: `local_images._agent_image_allowed_roots` contains only
  the reader's `workspace_path`; the original-byte LRU is also keyed per
  session id, so legitimate out-of-cwd tool output had no authorization
  path. The frontend turned the first img error into a permanent
  "Image unavailable" placeholder with no recovery.

## Selected contract (from `design-contract.md`)

No new registry, no second endpoint, no cross-session fallback, no directory
grant, no status-probe fetch, no automatic 404 loop. The authorization
boundary is a successfully completed recognized image-view tool call for
the exact path in the same session/incarnation; the UI gets an explicit
user-controlled retry using the existing authorized URL.

## Backend changes

`backend/claude_hub/services/agent_stream/local_images.py`:

1. `referenced_completed_tool_image(session, raw_path)` — fail-closed grant
   reader used as the last fallback in `_read_referenced_image` (after cwd
   containment and the temporary grant). It scans the session's canonical
   persisted stream and returns the strict-resolved regular file only when:
   - an assistant `tool_call_started` for a recognized image tool
     (`image_tool_path`: image `Read` / `view_image`) names the exact raw
     path, and
   - a `tool_call_completed` with `status == "completed"` carries the same
     call id **and** the same `run_epoch`/`turn_id`.
   Absolute paths only; symlinks, traversal, control chars, non-regular
   files, another session, failed/start-only calls, foreign run/turn and
   non-image tools return `None`. The shared `_read_agent_image` still
   applies size/magic/regular-file validation, so no new file-reading code
   was added; retention/replacement rules are untouched.
2. `ImageReferenceBuffer` now tracks started image-view calls in a bounded
   `(run_epoch, turn_id, call_id) -> paths` map and emits their paths only
   on the matching successful completion; failed, unknown-call and
   foreign-run completions emit nothing, and turn end prunes pending
   entries. `tool_call_started` still feeds the existing immediate cwd/temp
   capture path, so current behavior for in-scope files is unchanged.

Historical successful records are authorized directly from persisted
events — no backfill or migration.

## Frontend changes

- `controllers/useChatImages.ts`: added `isAgentImagePending` (only
  `tool.status === 'running'`), per-part `agentImageRetryNonces`, and
  `retryAgentImage(partKey)` which removes the failed marker (no permanent
  tombstone after an explicit retry) and bumps a nonce appended to the
  otherwise unchanged scoped URL. No status-classifying fetch, no automatic
  retry, no tool replay.
- `ChatAgentImage.vue` and `ChatSubthread.vue`: running tool images render
  an accessible pending state; failed blocks keep "Image unavailable" plus
  an accessible `Retry` button wired to the same part; the lightbox uses
  the nonce URL.
- `MarkdownContent.vue`: generated Markdown image placeholders
  (`.quoted-img-missing`) now include a Retry button that rebuilds the same
  authorized image URL with a retry nonce, preserving the original
  `data-quoted-img`/`data-local-img` markers and sanitizer behavior; a
  second failure re-enters the placeholder.

## Tests (red first, deterministic at the real boundary)

Backend `tests/test_agent_stream_agent_image.py` (60 tests pass):

- New red-then-green grant tests monkeypatch `_TEMP_IMAGE_ROOTS` to an
  isolated dir so the out-of-cwd artifact cannot pass via the `/tmp`
  exception (the required false-positive guard): genuine success; exact
  path not basename; started-only/failed/user/non-image-tool denied;
  same call-id different run/turn denied; cross-session denied; symlink
  and non-image refused; invalid replacement does not resurrect the old
  image.
- New `ImageReferenceBuffer` tests: no retention before successful
  completion, started paths emitted once on completion, foreign run/turn
  ignored, turn-end pruning.
- All pre-existing cwd, temporary, retention, owner-lifecycle and
  invalid-replacement tests stay green (359 tests across the six affected
  media/attachment suites).

Frontend `tests/useChatImages.test.mjs` (new, 6 tests): URL encoding and
retry nonce, pending predicate, error→retry→re-failure state machine,
per-part isolation from attachment errors. 101 tests pass across the image
node suites; `vue-tsc` and ESLint clean.

## Validation ledger (all on the locked worktree envs; evidence under
`image-unavailable-fix-20261108/author/`)

- Targeted backend: 359 passed (`backend-targeted-2.log`); the file alone
  60 passed.
- Targeted frontend: 101 passed (`frontend-targeted-1.log`);
  `vue-tsc` exit 0 (`frontend-tsc-2.log`); ESLint exit 0; Black clean.
- Repository-wide `verify.sh all`, browser-mocks and terminal-hmr runs are
  recorded in the final handoff with their own evidence roots. The known
  separate `browser_chat_image_quality` HTML-popup flake is not altered
  (no Enter substitution, no timeout change, no repeat driver) and is
  reported separately if encountered.

## Limits / risks

- The grant relies on canonical persisted tool events; an image produced
  entirely outside a recognized tool record still 404s by design.
- No claim is made that every historical transient browser failure is
  explained; the user screenshot path mapping remains the controller's
  separate workstream.
- Lessons used: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`
  (full-diff review discipline; applied at final handoff).

## Threat-review B1–B6 dispositions (independent pai phase one)

Reviewer artifact: `image-unavailable-fix-20261108/reviewer/threat-review.md`.
All six blocking gaps were closed with concrete code and negative tests
before broad validation; no registry/endpoint/directory widening was added.

- **B1 — producer-specific success provenance.** A generic
  `status == "completed"` is no longer trusted. `_PROVEN_IMAGE_TOOL_SUCCESS`
  is an explicit allowlist of `(agent_type, tool)` pairs whose failure
  encoding is adapter-verified; today only `(claude, Read)` qualifies
  (Claude JSONL `tool_result.is_error` → failed). Codex transcript
  `function_call_output` hard-codes "completed" and MASO omits its success
  flag, so both fail closed. Tests: Codex `view_image` with a
  hard-coded completed event denied; MASO completed-without-success denied.
- **B2 — full identity pairing.** Grant key is
  `(session, run_epoch, turn_id, sub_thread_id, call_id)`; the start must
  exist exactly once (duplicate start → ambiguous → deny) and precede the
  completion in the same run/turn/sub-thread; same call id in another
  run/turn/sub-thread is denied; a failed first attempt followed by a fresh
  start+completion under a new run is decided on the new occurrence.
- **B3 — symlink/TOCTOU bound (strengthened after controller red).** The
  first implementation used lstat checks plus leaf `O_NOFOLLOW`; the
  controller's `controller-parent-swap-red.json` proved a parent-directory
  symlink swap between check and open still redirected the read to a decoy
  inode. The final `_open_regular_image_pinned` walks the absolute path with
  `openat(dir_fd=...)`: every directory component is opened with
  `O_DIRECTORY | O_NOFOLLOW` relative to the previous component's pinned
  descriptor, then the final file with `O_NOFOLLOW | O_NONBLOCK`; fstat
  rejects non-regular files before reading. Once opened, a component's
  identity cannot be changed by a later ancestor swap. A deterministic
  route-boundary regression
  (`test_b3_parent_directory_symlink_swap_between_check_and_open_is_denied`)
  performs the exact swap and asserts 404 with no decoy bytes, then 200
  after restore. Absolute-only paths, no `..`, no `~`; FIFO/device
  substitution cannot hang before fstat due to `O_NONBLOCK`.
- **B4 — retained-byte revalidation and caching.** A grant-only source
  (lexically outside cwd and temp roots; `_is_grant_only_path`) re-proves
  its provenance before any retained LRU fallback; if the granting turn was
  truncated/cleared, the retained manifest entry is discarded and the route
  404s instead of resurrecting authority-less bytes (test asserts the
  source-key entry is gone and repeated requests stay closed). The route
  sends `Cache-Control: no-store` for `completed_tool` sources instead of
  the tab route's `immutable` header; cwd/temp retention semantics are
  unchanged.
- **B5 — no pre-completion fetch, no permanent loading.** The backend
  buffer emits newly authorized paths only on the matching successful
  completion (bounded pending map pruned at turn end). Frontend
  `isAgentImagePending(part, active)` shows the pending state only while the
  tool is running inside an active turn/thread; a completed/interrupted turn
  (`turn.completed`, subthread `lifecycleStatus !== 'active'`) settles the
  part into image or retryable placeholder, never an endless loader.
- **B6 — retry mechanics and re-render loops.** Retry removes the failed
  marker and adds a nonce to the same authorized URL; a second failure
  re-enters the placeholder with no automatic loop (composable tests).
  `MarkdownContent` keeps a per-instance `failedImageUrls` set so v-html
  re-renders during streaming neutralize already-failed images instead of
  re-requesting them; the accessible Retry button clears the entry and
  remounts the image. No status-probe fetch was added.
- **N1–N6:** the pairing scan remains bounded by the session's persisted
  event count (no new unbounded in-memory map); owner checks bracket every
  scan; forged-transcript residual risk is unchanged from existing tailer
  trust and documented; a negative test confirms assistant Markdown alone
  never grants a non-temp out-of-cwd path; redaction and suffix/magic
  filters apply on the pairing path via `image_tool_path`.

## N1 scan cost

The completed-tool grant scans the session's canonical stream JSONL on
each out-of-cwd image request (same read pattern as the pre-existing
`referenced_temporary_image`; no new in-memory index or unbounded map was
added). It is O(events) per request with constant extra memory, bounded
by the session's persisted event count. No benchmark is asserted here: an
earlier draft claimed ~0.2 ms for a synthetic 10,000-event stream, which
the final review could not substantiate because every line goes through
`json.loads`. The independent reviewer's read-only measurement on the
live `claude-hub-agent-5.jsonl` (21,104 lines, 7.4 MB) was ~83 ms per
full scan (`reviewer/final-review.md`); that figure is the reviewer's
measurement, not an author benchmark. If event volumes grow beyond
observed chat sizes, a bounded index can be added later without changing
the grant contract; no truncation is applied to historical scans, so
legitimate historical grants are never hidden.

## Ordinary code-review follow-up (D1/D2/N1) and rendered-browser proof

The independent ordinary source review (`reviewer/code-review-941730e.md`)
raised two frontend correctness defects and one cache-label inconsistency;
all were fixed as ordinary display-bug work, with no new mechanism or
directory access.

- **D1 — recovered Markdown image keeps its preview behavior.** The first
  implementation replaced the whole `button[data-image-preview]` wrapper
  with a placeholder and rebuilt a bare `<img>` on Retry, dropping
  click/keyboard lightbox access and the `loading=lazy`/`decoding=async`
  attributes. `MarkdownContent.handleError` now captures the original node
  and its full rendering contract; Retry rebuilds the same preview-button
  wrapper (class, `data-image-preview`, aria-label) around a fresh nonce
  image, so recovered finished-message images still open the lightbox and
  keep lazy/async attributes.
- **D2 — reactive `turnActive`.** `ChatAgentImage.vue` copied the prop into
  a plain constant at mount, so a turn settling after mount never flipped
  the pending state. It is now a `computed` over `props.turnActive`; an
  interrupted/crashed turn without a completion settles into image or
  Retry placeholder rather than an endless "Loading image…" spinner (the
  reducer's `turn_completed` side effect is no longer the only guarantee).
- **Retry nonce identity.** `withFreshNonce` strips any previous
  `retry=` parameter before appending a fresh one, so repeated retries keep
  one stable URL identity and never accumulate query parameters.
- **N1 — retained-cache label consistency.** Retained bytes served for a
  grant-only (out-of-cwd, non-temp) source are now labeled
  `IMAGE_SOURCE_COMPLETED_TOOL` instead of `IMAGE_SOURCE_CWD`, so the tab
  route sends `no-store` consistently for provenance-granted images; the
  immutable attachment-ID/blob route is untouched.
- **Rendered-browser regression in the existing fixture.** Per the
  functional-scope instruction, the new behavior is asserted inside the
  existing `browser_chat_image_quality.py` (no new framework/scenario):
  generated-Markdown failure → placeholder → first Retry stays usable while
  404 continues → second Retry after recovery renders a decoded image inside
  the original preview button (lightbox still opens, lazy/decoding
  preserved, single nonce); the failed image is requested exactly once at
  hydration and not re-requested after the fixture's one text delta and turn
  completion; the completed native tool image shows no pending placeholder,
  Retry recovers it and the lightbox opens. Results are written to
  `report.image_retry`. Stated coverage limits (per final review): the
  fixture has only one text delta, so same-block re-render resistance across
  multiple deltas is covered by the `useChatImages`/Markdown unit tests, not
  this browser run; the native tool is already completed in a completed turn,
  so the browser proves the settled state while the dynamic
  running-tool-settles transition stays unit-tested; repeated failure is
  browser-proven for Markdown and unit-tested for native cards.
- **Settled-state timing fix.** The pre-existing mobile tap-target
  assertion read `bounding_box()` immediately after viewport resize and
  caught a 43.865 px mid-reflow value against the CSS's real
  `min-height: 44px` contract. It now polls the actual geometry until it
  settles at ≥44 px (bounded 2 s, no blind delay, no weakened threshold).
  A separate intermittent `context.expect_page()` popup-delivery timeout
  (external-link target, unrelated to image display) appeared in one run and
  passed in the next; it is recorded as a distinct pre-existing flake, not
  relabeled or masked.

Evidence: `author/validation/image-quality-retry-7` (full PASS,
`image_retry` report with markdown/native results); run
`image-quality-retry-6` preserves the distinct popup timeout.
