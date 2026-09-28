/**
 * Bare Lark (Feishu) ``img_v3_`` image-key rewriting.
 *
 * lark-cli downloads inbound IM images to
 * ``~/.claude/oncall/.tmp_img/<sender>/lark-im-resources/img_v3_<key>.<ext>``
 * on the host running the backend. When an agent quotes such an image in
 * markdown the source is only the bare filename stem::
 *
 *     ![Image](img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g)
 *
 * The browser would otherwise resolve that stem as a relative URL against the
 * Hub page and get a 404 (broken image). These helpers recognize the bare key
 * and rewrite it to the backend's restricted reader
 * (``GET /tabs/{tabId}/stream/lark-image?key=...``), which locates the
 * downloaded file server-side.
 *
 * Everything else — http(s), data:, blob:, existing attachment URLs, and the
 * cwd-absolute paths used by the view_image/Read agent-image card — is left
 * untouched.
 */

/**
 * A bare Lark image key: ``img_v3_`` prefix plus the provider's safe charset.
 * No scheme, no path separators, no glob/query metacharacters. Mirrors the
 * backend charset in ``agent_stream.py``.
 */
const BARE_LARK_IMG_KEY_RE = /^img_v3_[A-Za-z0-9_-]+$/

/** Maximum key length the backend will accept. */
const LARK_IMG_KEY_MAX_LEN = 256

/**
 * True when *src* is a bare Lark image key we should proxy through the
 * backend reader. Explicitly excludes anything that already carries a scheme
 * or a path (the charset regex already forbids ``/``, ``\``, ``:`` and
 * quotes, but the checks stay here as defense in depth).
 */
export function isBareLarkImageKey(src: unknown): src is string {
  if (typeof src !== 'string') return false
  const trimmed = src.trim()
  if (!trimmed || trimmed.length > LARK_IMG_KEY_MAX_LEN) return false
  if (trimmed !== src) return false
  if (src.includes('/') || src.includes('\\') || src.includes(':')) return false
  return BARE_LARK_IMG_KEY_RE.test(src)
}

/**
 * Build the restricted backend reader URL for a validated bare key.
 */
export function larkImageEndpointUrl(tabId: string, key: string): string {
  return `/api/workspaces/tabs/${encodeURIComponent(tabId)}/stream/lark-image?key=${encodeURIComponent(key)}`
}

/**
 * Matches the ``src`` attribute of an ``<img>`` tag in already-sanitized
 * marked output. marked/DOMPurify always emit quoted attributes, and the bare
 * key charset contains no quotes or ``&``, so a single regex pass is safe and
 * needs no DOM (also keeps this util unit-testable in Node).
 */
const IMG_SRC_ATTR_RE = /(<img\b[^>]*?\bsrc=)(["'])(.*?)\2/gi

/**
 * Rewrite every bare ``img_v3_`` ``<img src>`` in a rendered HTML string to
 * the backend lark-image endpoint, tagging each rewritten element so the UI
 * can degrade gracefully on load failure. All other src values pass through
 * byte-for-byte.
 */
export function rewriteLarkImageSrcs(html: string, tabId: string): string {
  if (!html || !tabId) return html
  return html.replace(IMG_SRC_ATTR_RE, (match, prefix: string, quote: string, src: string) => {
    if (!isBareLarkImageKey(src)) return match
    const url = larkImageEndpointUrl(tabId, src)
    return `${prefix}${quote}${url}${quote} data-lark-img="${src}"`
  })
}
