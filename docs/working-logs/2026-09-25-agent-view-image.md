# 2026-09-25 — Agent-produced images render in Structured Chat (view_image)

## Symptom

In a TraeX solo Chat tab (`a50d8522`, "nsys profile"), the agent ran
`merlin-cli grafana screenshot`, producing
`/Users/bytedance/Projects/codex_workspace/60b_debug/tasks/.../gpu-utilization-*.png`,
then called the `view_image(path=…)` tool. The UI rendered the call as a
generic tool card whose only content was the path string — the picture never
appeared. User-uploaded images rendered fine, but agent-produced local images
had no equivalent path to the browser.

## Root cause (confirmed against live evidence)

- Backend already normalizes the Codex/TraeX app-server `imageView` item to a
  tool call `name="view_image"`, `args={"path": …}`:
  `services/agent_stream/codex_jsonl.py:374-376`. The persisted transcript for
  the incident tab contains exactly one such event
  (`tool_call_started`, seq 4156, absolute path under the tab cwd; the PNG
  still exists on disk, 77,489 bytes, magic `89 50 4E 47`).
- Frontend had zero `view_image` references. `StructuredPane.vue` renders tool
  calls only as a generic grouped tool card (`tool_group`) showing
  `args`/`result` text.
- User uploads use a different, already-working channel:
  `turn_started.attachments` → opaque id →
  `GET /api/workspaces/tabs/{tab}/stream/attachments/{id}`
  (`api/agent_stream.py` attachment routes +
  `services/agent_stream/attachments.py` bounded preview store).

The tab's launch cwd is `/Users/bytedance/Projects/codex_workspace`
(`~/.claude_hub/tabs.json`), which is exactly the directory the screenshot
lives under — so the existing tab-cwd scoping is the natural security root.

## Design

Give agent images a path that mirrors the user-attachment channel, but serve
bytes from disk under a strict allowlist rather than from a preview cache.

Backend — one new tab-scoped route:

`GET /api/workspaces/tabs/{tab_id}/stream/agent-image?path=…`
(`api/agent_stream.py:1757`). It resolves the live tab session exactly like
the attachment routes (`_terminal_tab_session_or_404`) and delegates to
`_resolve_agent_image` (`api/agent_stream.py:1684`).

Frontend — provider-independent recognition + dedicated render:

- `frontend/src/utils/agentImage.ts` — `agentImagePathFromTool(name, args)`:
  - Codex/TraeX: `view_image` → `args.path`.
  - Claude Code: there is no dedicated image tool; it reads images with the
    generic `Read` tool via `args.file_path`. Treated as an image only when the
    suffix is png/jpeg/jpg/gif/webp (the backend still validates magic bytes,
    so the suffix is a UI hint only). Code reads keep the generic tool card.
- `agentStreamTimeline.ts` — on `tool_call_started`, a recognized image call is
  split out of the ordinary tool groups into a new
  `{ kind: 'agent_image', tool, path }` part (same pattern as `subagent`). It
  is also recorded in `turn.tools` for status, and counts as one process step
  in fold stats (`countProcessSteps`) and folds with working process
  (`isProcessPart`).
- `StructuredPane.vue` — renders the part as a thumbnail (reusing
  `turn-attachment-button/img` chrome for visual parity, incl. dark theme) that
  opens the existing `openImageLightbox`, with a small mono file-name subtitle
  (full path on hover). URL is built by
  `agentImageUrl(path) = /api/workspaces/tabs/{tab}/stream/agent-image?path=<encoded>`.

## Security model — why this is not arbitrary file read

The endpoint is a restricted reader. Every layer fails closed to the same
opaque 404 (`api/agent_stream.py`):

1. **Tab/session ownership.** The route is keyed by tab id and resolves the
   live tab through `_terminal_tab_session_or_404` (unknown tab → 404). A path
   is only ever interpreted relative to *that* tab's cwd — cross-tab requests
   cannot read another tab's working directory even with its absolute path
   (`test_cross_tab_cwd_is_out_of_scope`).
2. **Directory allowlist (traversal + symlink escape).**
   `_agent_image_allowed_roots` (`agent_stream.py:1664`) yields exactly one
   root: `Path(session.workspace_path).expanduser().resolve()` — the tab launch
   cwd (a managed agent launched with `--cwd .` scopes to its worktree).
   Remote sessions return no root. In `_resolve_agent_image`, the candidate is
   fully resolved with `Path.resolve(strict=True)` — this expands `..` **and**
   every symlink — and must be a regular file that is `relative_to(root)`
   (`agent_stream.py:1731`). `../../etc/passwd`, an absolute `/etc/passwd`, an
   absolute image outside cwd, and a symlink inside cwd pointing outside are
   all rejected. Relative input is anchored at the root first; an absolute
   input passes only if it already lies within the root.
3. **Content allowlist (real MIME, not extension).** The file is read and its
   magic bytes are sniffed via the existing attachment whitelist helper
   `_magic_mime` (`agent_stream.py:1751`, imported from
   `services/agent_stream/attachments.py`); only PNG/JPEG/GIF/WebP are served.
   A text file under cwd, or `lying.png` with non-image bytes, is refused.
4. **Size bound.** At most `_AGENT_IMAGE_MAX_BYTES` (10 MiB,
   `agent_stream.py:1648`) is read; larger files 404.
5. **No information leak.** Empty/control-char/NUL paths, missing files,
   non-regular files, traversal, non-image, oversize, unknown tab, and remote
   all return an identical `404 image not available` with
   `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`, so the
   endpoint cannot be used as a file-existence oracle. Successful responses
   also carry `nosniff` and a private, short cache policy.

This mirrors the already-reviewed containment approach in
`services/workspace_manager/_artifacts.py` (expanduser → resolve(strict) →
relative_to over an explicit root list + is_file), scoped to the single tab
cwd rather than workspace/worktree roots.

## History / refresh & degradation

`tool_call_started` (name + args) is persisted in the per-tab jsonl transcript,
and history hydrates contiguously from sequence 0, so the `agent_image` part
and its URL are reconstructed on refresh / in past turns. If the agent later
deletes or moves the file, the scoped GET 404s and the `<img @error>` handler
records the part key in `erroredAgentImages`, swapping the thumbnail for a
stable "图片不可用" placeholder (path subtitle retained) — no broken-image
icon, and the set is part of the turn's `v-memo` deps so the swap renders.

Copy ("copy conversation") only includes text parts (`chatTurnCopy.ts`), so
images add neither binary nor path spam. Fork seeding is provider-format text
only and is unaffected.

## Tests / evidence

- Backend `tests/test_agent_stream_agent_image.py` — 16 cases, all green:
  absolute + cwd-relative + nested valid 200 with correct content-type and
  `nosniff`; JPEG sniff; `..` traversal, `/etc/passwd`, absolute image outside
  cwd, symlink→outside image, symlink→/etc/passwd all 404; non-image bytes and
  fake-extension 404; missing/empty 404; unknown tab 404; cross-tab scope
  denial (with a positive same-tab control); remote session 404.
  `black`/`isort`/`mypy` clean on changed files.
- Direct resolver smoke against the **real** incident PNG served `image/png`
  77,489 bytes; traversal/non-image/symlink/remote probes all 404.
- Frontend `tests/agentImage.test.mjs` (7) and
  `tests/agentStreamTimelineAgentImage.test.mjs` (6) cover provider mapping,
  image-part + percent-encoded src, ordinary-tool fallback, and fold count.
  Full `node --test` suite 501/0 (the only red file, `forkFromTurn.test.mjs`,
  fails identically on pristine HEAD due to a node `localStorage` harness
  quirk). `vue-tsc`, ESLint, and `vite build` all pass.

## Risks / non-goals

- Link validity is bounded by the file's lifetime on the agent's disk; the
  UI degrades to a placeholder rather than breaking (see above).
- Scope is the tab launch cwd only. Images an agent writes to a temp dir
  outside cwd are not served; widening roots is a deliberate future decision,
  not a default.
- Only the tab Structured Chat surface exists for structured streams
  (`StructuredPane` is mounted solely by `TerminalPane`), so a single
  tab-scoped route covers Codex, TraeX, and Claude. A future workspace-scoped
  chat surface would add a parallel session-scoped route reusing the same
  `_resolve_agent_image`.
