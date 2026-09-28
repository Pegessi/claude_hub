# Lark `img_v3_` images in Chat — endpoint + markdown src rewrite

Date: 2026-09-28
Branch: `fix/chat-lark-img-key` (base `main` f63bbfc)
Task: 4314001b — `[lark-img P1] 飞书 img_v3 图片端点+markdown src改写`

## Symptom

In Chat, an agent quoting a Lark (Feishu) message renders its image as a
broken image / "图片不可用". Evidence: terminal tab `257420e9`
(`~/.claude_hub/workspaces/terminal-tabs/agent_streams/terminal-tab-257420e9-….jsonl`)
contains markdown with a **bare key** and no scheme/directory/extension:

```
![Image](img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g)
```

lark-cli had already downloaded the image locally:

```
~/.claude/oncall/.tmp_img/<sender>/lark-im-resources/img_v3_<key>.<ext>
```

(`<sender>` subdirectories per peer — observed `chuxuan/`, `sc/`; `.jpg`
observed, `.png/.jpeg/.gif/.webp` also possible.)

### Root cause (file:line)

- marked turns the bare key into `<img src="img_v3_0215v_…">`. The browser
  resolves it relative to the Hub page URL → Hub 404 → broken image.
- The pre-existing restricted reader
  `GET /tabs/{id}/stream/agent-image?path=…` (`backend/claude_hub/api/agent_stream.py:1773`
  before this change) only serves files inside the tab session's
  `workspace_path` (cwd allowlist, `_agent_image_allowed_roots`). The Lark
  downloads live in `~/.claude/oncall/.tmp_img`, **outside** every tab cwd,
  and the markdown never carries an absolute path anyway. So no existing link
  could serve these images.

## Design

### Backend — `GET /api/workspaces/tabs/{tab_id}/stream/lark-image?key=…`

New restricted reader in `backend/claude_hub/api/agent_stream.py`, modeled on
the agent-image endpoint but **key-based, never path-based**:

1. **Auth / ownership** — `Depends(get_current_user)` + the shared
   `_terminal_tab_session_or_404(tab_id)` live-tab lookup, identical to the
   attachment/agent-image endpoints. Not an unauthenticated image proxy.
2. **No caller-controlled path** — the query value is only a key. It must
   fully match `^img_v3_[A-Za-z0-9_-]+$`, be ≤ 256 chars, and contain no
   control characters. Slashes, backslashes, `:`, `*`, `?`, `.`, spaces and
   quotes are all outside the charset, so traversal/glob-injection/absolute
   payloads fail validation before any filesystem call.
3. **Fixed-root lookup** — `_lark_image_resource_root()` returns
   `Path.home()/.claude/oncall/.tmp_img` (a function so tests monkeypatch a
   tmp tree). The file is found by globbing exactly
   `<root>/*/lark-im-resources/<key><suffix>` for the whitelisted suffixes
   (`.png .jpg .jpeg .gif .webp`). No path segment comes from the client.
4. **Containment after symlink resolution** — `candidate.resolve(strict=True)`
   fully expands links; the result must be a regular file,
   `relative_to(real_root)` must succeed, and the relative shape must be
   exactly `<sender>/lark-im-resources/<file>` (3 parts, middle segment
   `lark-im-resources`). A symlink pointing outside the root, or a file
   dropped at the root / directly under a sender dir, is not reachable.
5. **Content allowlist** — bytes are sniffed with the existing
   `sniff_image_mime` (PNG/JPEG/GIF/WebP magic, reused from
   `services/agent_stream/attachments.py`). A text file named
   `img_v3_x.jpg` is refused. Read is capped at 10 MiB
   (`_AGENT_IMAGE_MAX_BYTES`).
6. **No information leak** — every failure (bad key, missing root/file,
   non-regular, escape, oversized, bad magic) is the same opaque 404
   ("image not available") with `X-Content-Type-Options: nosniff`; success
   carries `Cache-Control: private, max-age=300`.

Remote sessions: the endpoint reuses the tab lookup; resource files are a
host-local concept, and the fixed root is the backend host's home — remote
tabs authenticate and 404 just like local tabs without a download.

### Frontend — bare-key recognition + markdown src rewrite

- `frontend/src/utils/larkImage.ts` (new):
  - `isBareLarkImageKey(src)` — type/length/charset guard; rejects anything
    with a scheme marker (`:`), `/`, `\`, whitespace, or glob/query
    metacharacters, and anything that is not the exact `img_v3_…` token.
  - `larkImageEndpointUrl(tabId, key)` — encoded endpoint URL.
  - `rewriteLarkImageSrcs(html, tabId)` — one regex pass over sanitized
    marked output replacing only `<img … src="bare-key">` values, tagging the
    element with `data-lark-img="<key>"`. Non-`<img>` matches (anchors, code,
    plain text) and non-bare srcs pass through byte-for-byte. No DOM required,
    so the util is unit-testable under Node.
- `frontend/src/utils/markdownBlocks.ts` — `MarkdownBlockCache.render()`
  gains a `larkTabId` option; the rewrite is a new post-process pass
  (alongside `linkPathMentions`) for both html blocks and list items. The
  cache key and invalidation include the tab id (the URL embeds it), so
  switching tabs can never serve another tab's rewritten URL.
- `frontend/src/components/MarkdownContent.vue` — new optional `tabId` prop
  threaded into the cache; a capture-phase `@error` listener replaces a
  `data-lark-img` image that fails to load with a muted inline
  `[image unavailable]` placeholder (`.lark-img-missing`) — no broken-image
  icon, no large error frame.
- `frontend/src/components/StructuredPane.vue` — all four `MarkdownContent`
  call sites (user text desktop/mobile, assistant text, sub-agent text) pass
  `:tab-id="props.tabId"`.

### Unchanged image paths (no regression)

User-uploaded attachments (`/stream/attachments/…`), `data:` previews,
`blob:`, ordinary http(s) images, and the agent-image card for
view_image/Claude-Read absolute cwd paths all fail the bare-key test and are
emitted exactly as before.

## Tests

Backend — `backend/tests/test_agent_stream_lark_image.py` (19 cases, resource
root monkeypatched to a tmp tree):

- valid key → 200 + correct content-type + nosniff; PNG across a second sender
  dir; unknown tab → 404; missing key → 404;
- 11 parametrized malformed keys → 404 (wrong prefix, prefix-only, `../`,
  separators, `.`, NUL/control, `*`, `?`, leading traversal, empty);
- non-image bytes with image extension → 404 (magic);
- symlink resolving outside the root → 404;
- files outside `lark-im-resources/` (root level, sender level) not
  glob-reachable; missing resource root → 404.

Frontend:

- `frontend/tests/larkImage.test.mjs` (new, 16 cases): recognition of
  valid/invalid keys, URL encoding, rewrite of double/single-quoted src,
  passthrough for http/https/data/blob/relative/absolute/attachment srcs and
  malformed look-alikes, anchors/text untouched, multiple images, empty input.
- `frontend/tests/markdownBlocks.test.mjs` (+4): rewrite through the block
  cache only when `larkTabId` is set, ordinary URLs unaffected, list-item
  rewrite, cache invalidation on tab-id switch.

Validation run: targeted backend pytest (new + existing agent-image suite)
green; black/isort/mypy clean on the changed module. Frontend
`vue-tsc --noEmit` = 0, `pnpm lint:check` = 0, `pnpm build` green, 583 node
unit tests pass (excluding `forkFromTurn.test.mjs`, which fails identically on
unmodified `main` under Node 25 — `localStorage.getItem is not a function`, an
environment-only pre-existing failure unrelated to this change). Full
`tests/` not run per task instruction (real-IO suites need a browser/tmux).

## Files

- `backend/claude_hub/api/agent_stream.py` — endpoint + resolver/validation.
- `backend/tests/test_agent_stream_lark_image.py` — new.
- `frontend/src/utils/larkImage.ts` — new.
- `frontend/src/utils/markdownBlocks.ts` — rewrite pass + cache key.
- `frontend/src/components/MarkdownContent.vue` — `tabId` prop, error fallback.
- `frontend/src/components/StructuredPane.vue` — pass `tab-id` at 4 sites.
- `frontend/tests/larkImage.test.mjs`, `frontend/tests/markdownBlocks.test.mjs`.
