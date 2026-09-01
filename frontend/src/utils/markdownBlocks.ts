import { marked, type Token } from 'marked'
import DOMPurify from 'dompurify'

/**
 * Marked render options.
 *
 * ``gfm: true`` enables GitHub-Flavored Markdown (tables, strikethrough,
 * task lists, autolinks). ``breaks: true`` renders single newlines inside
 * paragraphs as ``<br>`` — matching the previous ``marked.parse`` behaviour
 * so existing single-newline fixtures keep their line breaks.
 */
const MARKED_OPTIONS = { gfm: true, breaks: true }

/**
 * Split markdown source into top-level block tokens.
 *
 * Uses ``marked.lexer`` so block boundaries (fenced code, lists,
 * blockquotes, headings, paragraphs) are identified by marked itself —
 * no hand-rolled fence/list/quote splitter that could mis-split
 * continuations. ``space`` tokens (blank-line separators) are dropped
 * because they render to empty HTML.
 */
export function splitBlockTokens(source: string): Token[] {
  return marked.lexer(source, MARKED_OPTIONS).filter((t) => t.type !== 'space')
}

/**
 * Render a single block token to sanitized HTML.
 */
export function renderBlockToken(token: Token): string {
  return DOMPurify.sanitize(marked.parser([token], MARKED_OPTIONS))
}

/**
 * Per-block render cache that avoids caching the live tail.
 *
 * During a streaming render the last block token is the "live tail": its
 * ``raw`` text changes on every delta. Caching every version of it would
 * leak one HTML string per delta. Instead we cache only *completed* blocks
 * — every token except the last — and render the live tail uncached. A
 * block becomes cached as soon as a following top-level block appears (it
 * is no longer the tail), or when ``complete`` is passed to ``render``.
 *
 * This keeps the cache size bounded to the number of completed blocks,
 * not the number of deltas.
 */
export class MarkdownBlockCache {
  private cache = new Map<string, string>()

  /**
   * Render a full markdown source, caching completed blocks.
   *
   * @param source - markdown text.
   * @param complete - when true, the final block is also cached (stream
   *   has ended). Defaults to false for live streaming.
   */
  render(source: string, complete = false): string {
    const tokens = splitBlockTokens(source)
    if (tokens.length === 0) return ''

    let html = ''
    const lastIndex = tokens.length - 1
    for (let i = 0; i < tokens.length; i++) {
      const token = tokens[i]
      const isLast = i === lastIndex
      const key = token.raw

      if (!isLast || complete) {
        // Completed block: parse once and cache.
        let blockHtml = this.cache.get(key)
        if (blockHtml === undefined) {
          blockHtml = renderBlockToken(token)
          this.cache.set(key, blockHtml)
        }
        html += blockHtml
      } else {
        // Live tail: render but do not cache.
        html += renderBlockToken(token)
      }
    }
    return html
  }

  /** Number of cached blocks. */
  get size(): number {
    return this.cache.size
  }

  /** Clear the cache. */
  clear(): void {
    this.cache.clear()
  }

  /** Check whether a block's raw text is cached. */
  has(raw: string): boolean {
    return this.cache.has(raw)
  }
}
