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
 * or an existing attachment/agent-image URL — is passed through untouched.
 * Local raster image paths use the tab-scoped agent-image reader instead.
 */

/** Maximum key length accepted by the backend resolver layer. */
const QUOTED_IMAGE_KEY_MAX_LEN = 256

/** Endpoint path builder. Tab id and key are always percent-encoded. */
function quotedImageEndpointUrl(tabId: string, key: string): string {
  return `/api/workspaces/tabs/${encodeImagePart(tabId)}/stream/quoted-image?key=${encodeImagePart(key)}`
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

function decodeImagePath(src: string): string {
  const entities: Record<string, string> = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>' }
  const decoded = src.replace(/&(#x[\da-f]+|#\d+|amp|quot|apos|lt|gt);/gi, (match, entity: string) => {
    if (!entity.startsWith('#')) return entities[entity.toLowerCase()] ?? match
    const code = entity[1].toLowerCase() === 'x'
      ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10)
    return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : match
  })
  try { return decodeURIComponent(decoded) } catch { return decoded }
}

function encodeImagePart(value: string): string {
  // encodeURIComponent leaves apostrophes intact; src may be single-quoted.
  return encodeURIComponent(value).replace(/'/g, '%27')
}

export function resolveLocalImageUrl(tabId: string, src: string): string | null {
  const path = decodeImagePath(src)
  if (!tabId || !path || path.length > 4096 || path !== path.trim()) return null
  if (/^[a-z][a-z\d+.-]*:|^\/\//i.test(path)) return null
  if ([...path].some((char) => char.charCodeAt(0) < 32)) return null
  if (/^\/(?:api|assets|static)\//.test(path)) return null
  if (!/\.(?:png|jpe?g|gif|webp)$/i.test(path)) return null
  try {
    return `/api/workspaces/tabs/${encodeImagePart(tabId)}/stream/agent-image?path=${encodeImagePart(path)}`
  } catch {
    // A malformed Unicode filename must not prevent the whole chat rendering.
    return null
  }
}

// Consume whole tags/attributes so data-src and src= inside alt text cannot
// be mistaken for the actual source. Input has already passed DOMPurify.
const IMG_TAG_RE = /<img\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi
const QUOTED_ATTR_RE = /([\w:-]+)\s*=\s*("[^"]*"|'[^']*')/g

/**
 * Route provider tokens and local paths to their restricted readers. Tag the
 * images so the UI can degrade gracefully on load failure.
 */
export function rewriteQuotedImageSrcs(html: string, tabId: string): string {
  if (!html || !tabId) return html
  return html.replace(IMG_TAG_RE, (tag) => tag.replace(QUOTED_ATTR_RE, (attr, name: string, value: string) => {
    if (name.toLowerCase() !== 'src') return attr
    const src = value.slice(1, -1)
    const quotedUrl = resolveQuotedImageUrl(tabId, src)
    const url = quotedUrl ?? resolveLocalImageUrl(tabId, src)
    if (url === null) return attr
    const marker = quotedUrl ? `data-quoted-img="${src}"` : 'data-local-img="1"'
    return `${name}=${value[0]}${url}${value[0]} ${marker}`
  }))
}
