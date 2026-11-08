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
