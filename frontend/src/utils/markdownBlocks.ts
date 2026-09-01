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
 * A single rendered markdown block.
 *
 * ``key`` is a stable Vue ``v-for`` key derived from the block's index in
 * the token list. Completed blocks never change position, so their key is
 * stable and Vue reuses their DOM node across renders. The live tail (the
 * still-growing last block) always sits at the last index, so its key is
 * stable while it grows and Vue reuses one DOM node, updating only its
 * ``innerHTML``. The rest of the subtree is left untouched.
 *
 * The cache stores rendered HTML strings keyed by the block's raw text
 * (plus the linkMarkdownPaths mode); it does not cache descriptor objects.
 */
export interface RenderedBlock {
  key: string
  html: string
}

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

const MARKDOWN_PATH_PATTERN =
  /((?:~|\.{1,2}|\/|[\w.-]+\/)?[\w./~@:+-]+\.(?:md|markdown|mdown|mkd)(?::\d+)?(?:[?#][^\s`"'<>)]*)?)/gi

function hasLinkExcludedParent(node: Node): boolean {
  let parent = node.parentElement
  while (parent) {
    if (['A', 'CODE', 'PRE', 'KBD', 'SAMP'].includes(parent.tagName)) return true
    parent = parent.parentElement
  }
  return false
}

/**
 * Wrap markdown path mentions (``foo.md``, ``./bar.md:12``, …) in anchor
 * tags so the host component can intercept clicks. Operates on a single
 * block's HTML string.
 */
export function linkPathMentions(html: string): string {
  if (typeof document === 'undefined') return html
  const template = document.createElement('template')
  template.innerHTML = html
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT)
  const textNodes: Text[] = []
  let node = walker.nextNode()
  while (node) {
    if (node instanceof Text && !hasLinkExcludedParent(node)) {
      textNodes.push(node)
    }
    node = walker.nextNode()
  }

  textNodes.forEach((textNode) => {
    const text = textNode.nodeValue || ''
    const matches = Array.from(text.matchAll(MARKDOWN_PATH_PATTERN))
    if (matches.length === 0) return

    const fragment = document.createDocumentFragment()
    let offset = 0
    matches.forEach((match) => {
      const path = match[0]
      const index = match.index ?? 0
      if (index > offset) {
        fragment.append(document.createTextNode(text.slice(offset, index)))
      }
      const link = document.createElement('a')
      link.href = '#'
      link.dataset.markdownPath = path
      link.textContent = path
      link.className = 'markdown-path-link'
      fragment.append(link)
      offset = index + path.length
    })
    if (offset < text.length) {
      fragment.append(document.createTextNode(text.slice(offset)))
    }
    textNode.replaceWith(fragment)
  })

  return template.innerHTML
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
 *
 * Cache keys include the ``linkMarkdownPaths`` mode so that switching the
 * mode does not serve stale (un)linked HTML.
 */
export class MarkdownBlockCache {
  private cache = new Map<string, string>()
  /** The linkMarkdownPaths mode the cache was populated for. If the mode
   *  changes, the cache is invalidated. */
  private linkMode: boolean | null = null

  private cacheKey(raw: string, linkMarkdownPaths: boolean): string {
    return `${linkMarkdownPaths ? 'l:' : 'n:'}${raw}`
  }

  /**
   * Render a full markdown source into a list of block descriptors.
   *
   * Completed blocks are cached by their raw text (plus link mode); the
   * returned descriptor carries the cached HTML and an index-based stable
   * key. The live tail (the still-growing last block) is rendered fresh
   * each call and carries the last index as its key so Vue reuses one DOM
   * node.
   *
   * @param source - markdown text.
   * @param options.complete - when true, the final block is also cached
   *   (stream has ended). Defaults to false for live streaming.
   * @param options.linkMarkdownPaths - when true, markdown path mentions
   *   are wrapped in anchor tags. Defaults to false.
   */
  render(
    source: string,
    options: { complete?: boolean; linkMarkdownPaths?: boolean } = {},
  ): RenderedBlock[] {
    const { complete = false, linkMarkdownPaths = false } = options

    // Invalidate the cache if the link mode changed since the last render.
    if (this.linkMode !== null && this.linkMode !== linkMarkdownPaths) {
      this.cache.clear()
    }
    this.linkMode = linkMarkdownPaths

    const tokens = splitBlockTokens(source)
    if (tokens.length === 0) return []

    const blocks: RenderedBlock[] = []
    const lastIndex = tokens.length - 1
    for (let i = 0; i < tokens.length; i++) {
      const token = tokens[i]
      const isLast = i === lastIndex
      const rawKey = this.cacheKey(token.raw, linkMarkdownPaths)

      if (!isLast || complete) {
        // Completed block: parse once, apply link wrapping once, cache.
        let blockHtml = this.cache.get(rawKey)
        if (blockHtml === undefined) {
          blockHtml = renderBlockToken(token)
          if (linkMarkdownPaths) {
            blockHtml = linkPathMentions(blockHtml)
          }
          this.cache.set(rawKey, blockHtml)
        }
        blocks.push({ key: `block:${i}`, html: blockHtml })
      } else {
        // Live tail: render (and link-wrap) but do not cache.
        let blockHtml = renderBlockToken(token)
        if (linkMarkdownPaths) {
          blockHtml = linkPathMentions(blockHtml)
        }
        blocks.push({ key: `block:${i}`, html: blockHtml })
      }
    }
    return blocks
  }

  /** Number of cached blocks. */
  get size(): number {
    return this.cache.size
  }

  /** Clear the cache. */
  clear(): void {
    this.cache.clear()
    this.linkMode = null
  }

  /** Check whether a block's raw text is cached. */
  has(raw: string): boolean {
    // Check both link-mode variants.
    return this.cache.has(this.cacheKey(raw, true)) ||
      this.cache.has(this.cacheKey(raw, false))
  }
}

/** Join rendered blocks back into a single HTML string (for tests and
 *  callers that need the legacy concatenated output). */
export function joinBlocks(blocks: RenderedBlock[]): string {
  return blocks.map((b) => b.html).join('')
}
