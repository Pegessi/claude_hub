/**
 * Agent-quoted image resolution/proxy layer (frontend).
 *
 * Agents quote images in chat markdown using provider-specific **bare
 * tokens** — no scheme, directory, or extension. Lark/Feishu uses
 * ``img_v3_<key>``; future sources (other Lark keys, Slack/DingTalk, a CDN
 * token, a skill-local temp image) will use their own token shapes. The
 * browser cannot load any of those directly.
 *
 * This module is the single place that decides whether an ``<img src>`` is a
 * bare provider token and, if so, rewrites it to the backend's
 * provider-neutral reader
 * (``GET /tabs/{tabId}/stream/quoted-image?key=…``). It is deliberately
 * matcher-driven:
 *
 * - each source is a :class:`QuotedImageMatcher` in ``QUOTED_IMAGE_MATCHERS``;
 * - the first matcher that recognizes the token wins;
 * - adding a source is adding one matcher — render code and the backend route
 *   contract never change.
 *
 * Anything already loadable by the browser — http(s), ``data:``, ``blob:``,
 * an existing attachment/agent-image URL, a relative or cwd-absolute path —
 * is not a bare token and is passed through untouched.
 */

/** Maximum key length accepted by the backend resolver layer. */
const QUOTED_IMAGE_KEY_MAX_LEN = 256

/** Endpoint path builder. Tab id and key are always percent-encoded. */
function quotedImageEndpointUrl(tabId: string, key: string): string {
  return `/api/workspaces/tabs/${encodeURIComponent(tabId)}/stream/quoted-image?key=${encodeURIComponent(key)}`
}

/**
 * A bare-token image provider. ``pattern`` must be anchored and must never
 * permit path separators, glob/query metacharacters, whitespace, or a scheme
 * marker — those characters alone would make the token browser-loadable, and
 * the whole point of this layer is to proxy *only* opaque bare keys.
 */
export interface QuotedImageMatcher {
  /** Provider name (tests/introspection). */
  readonly name: string
  /** Anchored allowlist for the bare token. */
  readonly pattern: RegExp
}

/**
 * Provider #1 — Lark/Feishu ``img_v3_<key>`` images lark-cli downloaded to
 * ``~/.claude/oncall/.tmp_img/<sender>/lark-im-resources/``. Mirrors the
 * backend ``LarkImgV3Resolver`` charset.
 */
export const LARK_IMG_V3_MATCHER: QuotedImageMatcher = {
  name: 'lark-img-v3',
  pattern: /^img_v3_[A-Za-z0-9_-]+$/,
}

/**
 * Dispatch chain. First match wins, so order matchers from most specific to
 * most general when adding providers.
 */
export const QUOTED_IMAGE_MATCHERS: readonly QuotedImageMatcher[] = [
  LARK_IMG_V3_MATCHER,
]

/**
 * True when *src* is a bare provider token this layer should proxy.
 *
 * Defense in depth beyond the matcher pattern: reject non-strings, trimmed /
 * oversized values, and anything carrying a scheme marker (``:``) or a path
 * separator (``/``, ``\\``) so ordinary URLs and local paths can never be
 * proxied even if a future pattern is written too loosely.
 */
export function isBareQuotedImageKey(src: unknown): src is string {
  if (typeof src !== 'string') return false
  if (!src || src.length > QUOTED_IMAGE_KEY_MAX_LEN) return false
  if (src !== src.trim()) return false
  if (src.includes('/') || src.includes('\\') || src.includes(':')) return false
  return QUOTED_IMAGE_MATCHERS.some((m) => m.pattern.test(src))
}

/**
 * Name of the registered provider that owns *src*, or ``null`` when no
 * matcher recognizes it.
 */
export function quotedImageProviderName(src: unknown): string | null {
  if (!isBareQuotedImageKey(src)) return null
  const matcher = QUOTED_IMAGE_MATCHERS.find((m) => m.pattern.test(src))
  return matcher ? matcher.name : null
}

/**
 * Build the restricted backend reader URL for a validated bare token.
 * Returns ``null`` when *src* is not a registered bare token.
 */
export function resolveQuotedImageUrl(tabId: string, src: unknown): string | null {
  if (!tabId || !isBareQuotedImageKey(src)) return null
  return quotedImageEndpointUrl(tabId, src)
}

/**
 * Matches the ``src`` attribute of an ``<img>`` tag in already-sanitized
 * marked output. marked/DOMPurify always emit quoted attributes, and a bare
 * token contains no quotes or ``&``, so a single regex pass is safe and needs
 * no DOM (also keeps this util unit-testable in Node).
 */
const IMG_SRC_ATTR_RE = /(<img\b[^>]*?\bsrc=)(["'])(.*?)\2/gi

/**
 * Rewrite every proxied bare-token ``<img src>`` in a rendered HTML string to
 * the backend quoted-image endpoint, tagging each rewritten element so the UI
 * can degrade gracefully on load failure. All other src values pass through
 * byte-for-byte.
 */
export function rewriteQuotedImageSrcs(html: string, tabId: string): string {
  if (!html || !tabId) return html
  return html.replace(IMG_SRC_ATTR_RE, (match, prefix: string, quote: string, src: string) => {
    const url = resolveQuotedImageUrl(tabId, src)
    if (url === null) return match
    return `${prefix}${quote}${url}${quote} data-quoted-img="${src}"`
  })
}
