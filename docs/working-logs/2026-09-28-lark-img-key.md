# Agent-quoted images in Chat — resolver/proxy layer (Lark `img_v3_` first)

Date: 2026-09-28
Branch: `fix/chat-lark-img-key` (base `main` f63bbfc)
Task: 4314001b — `[lark-img P1] 飞书 img_v3 图片端点+markdown src改写`

Commits:

1. `74fcaa0` — initial lark-specific endpoint + markdown rewrite (review round 1).
2. `53f4ccf` — backend generalized into a resolver registry; route renamed to
   `/stream/quoted-image`.
3. (this round, follow-up commit) — frontend matcher layer + renamed
   contracts, docs.

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

### Root cause

- marked turns the bare key into `<img src="img_v3_0215v_…">`. The browser
  resolves it relative to the Hub page URL → Hub 404 → broken image.
- The pre-existing restricted reader
  `GET /tabs/{id}/stream/agent-image?path=…` (`backend/claude_hub/api/agent_stream.py`)
  only serves files inside the tab session's `workspace_path` (cwd
  allowlist). The Lark downloads live in `~/.claude/oncall/.tmp_img`,
  **outside** every tab cwd, and the markdown never carries an absolute path.

## Generic architecture (review round 2)

Review feedback: don't special-case Lark — agents will quote images from many
sources (other Lark keys, Slack/DingTalk/CDN, base64, skill-local temp
images). The fix is now an extensible **agent-quoted image resolve/proxy
layer**; Lark `img_v3_` is simply the first registered provider.

### Backend

`backend/claude_hub/services/agent_stream/quoted_images.py` (new):

- **Result types** — `LocalImage(data, media_type)`, `DataImage(data,
  media_type)`, `RedirectImage(location)`; `QuotedImageResult` is their
  union. Lark returns `LocalImage` today; the route already renders
  `RedirectImage` as a `307` and `DataImage` as inline bytes, so a remote/CDN
  or inline provider needs no route change.
- **`QuotedImageResolver`** (base): `name`, anchored `key_pattern`,
  `max_key_length`; `matches(key)` performs the shared key validation
  (string, non-empty, ≤256, no control chars, anchored full-match);
  `resolve(key)` validates then calls provider `_resolve`.
- **`LocalFileResolver(QuotedImageResolver)`** — the shared security base for
  the usual "key → file under allowed roots" providers. Subclasses implement
  only `roots()`, `candidate_files(key)`, and optionally
  `accepts_resolved(real, root)`. The base:
  1. resolves every root with `Path.resolve(strict=True)`;
  2. fully resolves each candidate (symlinks expanded) and requires a regular
     file accepted by `accepts_resolved` against a resolved root;
  3. reads with a 10 MiB cap (`QUOTED_IMAGE_MAX_BYTES`);
  4. sniffs magic via the existing attachments `_magic_mime`
     (PNG/JPEG/GIF/WebP);
  5. raises `QuotedImageUnavailable` for every failure.
- **Registry** — `register_resolver()`, `registered_resolvers()`,
  `resolve_quoted_image(key)`; first match wins, so register from most
  specific to most general. `LarkImgV3Resolver` is registered at import.
- **`LarkImgV3Resolver(LocalFileResolver)`** — key pattern
  `img_v3_[A-Za-z0-9_-]+`; root from `default_lark_resource_root()`
  (`Path.home()/.claude/oncall/.tmp_img`, a function for monkeypatching);
  candidates come from globbing `*/lark-im-resources/<key><suffix>` over the
  five image suffixes; `accepts_resolved` enforces the exact 3-part shape
  `<sender>/lark-im-resources/<file>` after resolution.

`backend/claude_hub/api/agent_stream.py`:

- Single provider-neutral route
  `GET /api/workspaces/tabs/{tab_id}/stream/quoted-image?key=…`
  (`get_tab_quoted_image`): `Depends(get_current_user)` + shared
  `_terminal_tab_session_or_404(tab_id)` ownership lookup, then dispatch; any
  `QuotedImageUnavailable` becomes the same opaque 404
  ("image not available", `X-Content-Type-Options: nosniff`). Local/data
  responses carry nosniff (`LocalImage` gets `Cache-Control: private,
  max-age=300`; `DataImage` no-store; redirect keeps nosniff on the 307).
- The round-1 `/stream/lark-image` route was **renamed, not aliased** — there
  is only one surface and one frontend contract.

### Frontend

`frontend/src/utils/quotedImage.ts` (renamed from `larkImage.ts`):

- `QuotedImageMatcher { name, pattern }` and the `QUOTED_IMAGE_MATCHERS` list;
  `LARK_IMG_V3_MATCHER` (`/^img_v3_[A-Za-z0-9_-]+$/`) is entry #1. First
  match wins.
- `isBareQuotedImageKey(src)` — matcher match plus defense-in-depth guards
  (non-string/empty/≤256 rejected; untrimmed rejected; any `:`, `/`, `\`
  rejected) so a loose future pattern can never proxy an ordinary URL or
  local path.
- `quotedImageProviderName(src)` — dispatch introspection (null when
  unregistered).
- `resolveQuotedImageUrl(tabId, src)` — null unless a registered matcher
  owns the token; URL is
  `/api/workspaces/tabs/{tabId}/stream/quoted-image?key=…` with both parts
  percent-encoded.
- `rewriteQuotedImageSrcs(html, tabId)` — one regex pass over sanitized
  marked output; only `<img … src="bare-token">` is rewritten and tagged
  `data-quoted-img="<token>"`. Anchors/code/text and all non-bare srcs pass
  through byte-for-byte. No DOM needed (unit-testable in Node).

Wiring:

- `utils/markdownBlocks.ts` — `MarkdownBlockCache.render()` option renamed to
  `quotedTabId`; rewrite is a post-process pass (with `linkPathMentions`) for
  html blocks and list items; cache key + invalidation include the tab id.
- `components/MarkdownContent.vue` — `tabId` prop (unchanged name, generic
  meaning), capture-phase `@error` swaps a `data-quoted-img` image that fails
  to load with a muted `.quoted-img-missing` `[image unavailable]`
  placeholder.
- `components/StructuredPane.vue` — the four `MarkdownContent` sites pass
  `:tab-id="props.tabId"` (unchanged from round 1).

### How to add a new image source (the extension point)

Backend (no route/test-contract change):

```python
class SlackResolver(qi.LocalFileResolver):       # or qi.QuotedImageResolver
    name = "slack"
    key_pattern = re.compile(r"slack_[A-Za-z0-9_-]+")
    def roots(self): return [Path.home() / ".cache" / "slack-img"]
    def candidate_files(self, key): return ...    # fixed-shape glob/path
    # accepts_resolved() override only for extra shape checks
qi.register_resolver(SlackResolver())
```

For a remote source, extend `QuotedImageResolver` directly and return
`RedirectImage("https://…")` (307 today) or `DataImage(...)`.

Frontend (one matcher):

```ts
export const SLACK_MATCHER: QuotedImageMatcher = {
  name: 'slack', pattern: /^slack_[A-Za-z0-9_-]+$/,
}
// add to QUOTED_IMAGE_MATCHERS (order = specificity)
```

Nothing else changes: the route, the cache, and `MarkdownContent` are
provider-neutral. Until both sides register a shape, an unknown bare token
renders as its original src (and the browser naturally 404s) — never
mis-proxied to a provider that doesn't own it.

## Tests

Backend — `backend/tests/test_agent_stream_quoted_image.py` (25 cases, root
monkeypatched to tmp):

- Lark security base through the neutral endpoint: valid jpeg/png → 200 +
  content-type + nosniff; unknown tab → 404; missing key → 404;
- 11 parametrized malformed keys → 404 (wrong prefix, prefix-only, `../`,
  separators, `.`, NUL, `*`, `?`, leading traversal, empty);
- non-image magic → 404; symlink escaping root → 404; files outside
  `lark-im-resources/` not glob-reachable; missing root → 404;
- **dispatch/registry**: unregistered foreign bare tokens (`slack_…`,
  `ding_…`, `cdn_…`, `tmpimg_…`) → 404 / `QuotedImageUnavailable`;
  `lark-img-v3` registered and wins `img_v3_`;
- **extension point**: a newly registered `LocalFileResolver` subclass owns
  its key with no route change while Lark still owns `img_v3_`; a
  `RedirectImage` resolver yields a `307` + `location` + nosniff through the
  real HTTP route.

Frontend:

- `frontend/tests/quotedImage.test.mjs` (17 cases): matcher registry shape;
  valid/invalid/foreign token recognition; provider-name dispatch; URL
  encoding + null for non-bare/foreign/empty-tab; double/single-quoted
  rewrite; http/https/data/blob/relative/absolute/attachment passthrough;
  malformed/foreign src not proxied; anchors/text untouched; multiple
  images; empty-input passthrough.
- `frontend/tests/markdownBlocks.test.mjs` (4 integration cases): rewrite
  only with `quotedTabId`; ordinary URLs and an unregistered foreign token
  left as-is; list-item rewrite; cache invalidation on tab-id switch.

Validation: backend targeted pytest (quoted-image + existing agent-image
regression) = 41 passed; black/isort/mypy clean. Frontend `vue-tsc --noEmit`
= 0, `pnpm lint:check` = 0, `pnpm build` green; 586 node unit tests pass
(all suites except `forkFromTurn.test.mjs`, which fails identically on
unmodified `main` under Node 25 — `localStorage.getItem is not a function`,
pre-existing/environmental). Full `tests/` not run per instruction.

## Files

- `backend/claude_hub/services/agent_stream/quoted_images.py` — new layer.
- `backend/claude_hub/api/agent_stream.py` — single `/stream/quoted-image`
  route + result-type handling.
- `backend/tests/test_agent_stream_quoted_image.py` — new (replaces the
  round-1 lark-named test file).
- `frontend/src/utils/quotedImage.ts` — matcher layer (replaces
  `larkImage.ts`).
- `frontend/src/utils/markdownBlocks.ts` — generic rewrite pass + cache key.
- `frontend/src/components/MarkdownContent.vue` — `data-quoted-img`,
  `.quoted-img-missing`, `quotedTabId`.
- `frontend/src/components/StructuredPane.vue` — passes `tab-id` (4 sites).
- `frontend/tests/quotedImage.test.mjs`, `frontend/tests/markdownBlocks.test.mjs`.
