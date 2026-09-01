import { marked, type Token, type Tokens } from 'marked'
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
 * A rendered markdown block that is a single HTML element (paragraph,
 * heading, code block, blockquote, table, …).
 */
export interface RenderedHtmlBlock {
  key: string
  html: string
}

/**
 * A rendered markdown list. The list is rendered as a single ``<ul>`` or
 * ``<ol>`` element with keyed ``<li>`` children. Each completed item's HTML
 * is cached; only the final (still-growing) item is re-rendered on each
 * delta. This bounds the DOM work for long streamed lists to the size of
 * the final item, not the whole list.
 */
export interface RenderedListBlock {
  key: string
  list: {
    ordered: boolean
    start: number
    items: { key: string; html: string }[]
  }
}

export type RenderedBlock = RenderedHtmlBlock | RenderedListBlock

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
 * Render a single list item to the inner HTML of its ``<li>`` element.
 *
 * Uses marked itself: a synthetic list token containing only this item is
 * passed to ``marked.parser``, producing ``<ul><li>…</li></ul>`` (or
 * ``<ol>…</ol>``). The ``<li>`` inner HTML is extracted via the DOM (or a
 * regex fallback in Node). This relies on marked's own list/item parsing —
 * no regex line-splitting of the raw markdown.
 */
function renderListItemInnerHtml(item: Tokens.ListItem, listToken: Tokens.List): string {
  const syntheticList: Tokens.List = {
    type: 'list',
    raw: item.raw,
    ordered: listToken.ordered,
    start: listToken.start,
    loose: listToken.loose,
    items: [item],
  }
  const html = DOMPurify.sanitize(marked.parser([syntheticList], MARKED_OPTIONS))
  if (typeof document === 'undefined') {
    const match = html.match(/<li[^>]*>([\s\S]*)<\/li>/)
    return match ? match[1] : html
  }
  const template = document.createElement('template')
  template.innerHTML = html
  const li = template.content.querySelector('li')
  return li ? li.innerHTML : html
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
 * List items
 * ----------
 * A contiguous list is a single top-level marked token. Without special
 * handling the whole list's ``innerHTML`` is replaced on every delta —
 * O(list length) DOM work per delta, O(n²) overall. To bound this, a list
 * token is split into its marked-parsed ``items``. Every item except the
 * last is treated as completed: its ``<li>`` inner HTML is rendered once,
 * sanitized, link-wrapped, and cached by the item's raw text. Only the
 * final item is re-rendered each delta. The list is emitted as a single
 * ``RenderedListBlock`` so the host can render one stable ``<ul>``/``<ol>``
 * with keyed ``<li>`` children — Vue leaves completed ``<li>`` nodes
 * untouched and updates only the final item's ``innerHTML``.
 *
 * Cache keys include the ``linkMarkdownPaths`` mode so that switching the
 * mode does not serve stale (un)linked HTML.
 */
export class MarkdownBlockCache {
  private cache = new Map<string, string>()
  /** The linkMarkdownPaths mode the cache was populated for. If the mode
   *  changes, the cache is invalidated. */
  private linkMode: boolean | null = null

  private cacheKey(raw: string, linkMarkdownPaths: boolean, listLoose?: boolean): string {
    const link = linkMarkdownPaths ? 'l' : 'n'
    // List items are rendered in the context of their parent list's ``loose``
    // flag, which controls whether the item content is wrapped in ``<p>``.
    // ``loose`` is a list-wide property that can change from false to true as
    // items are appended (a blank line between items makes the whole list
    // loose). Including it in the key ensures cached items are invalidated
    // when the list's loose state changes.
    const loose = listLoose === undefined ? '' : `:loose=${listLoose}`
    return `${link}${loose}:${raw}`
  }

  /**
   * Render a full markdown source into a list of block descriptors.
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

      if (token.type === 'list') {
        blocks.push(this.renderListBlock(token as Tokens.List, `block:${i}`, {
          isLast,
          complete,
          linkMarkdownPaths,
        }))
      } else {
        blocks.push(this.renderHtmlBlock(token, `block:${i}`, {
          isLast,
          complete,
          linkMarkdownPaths,
        }))
      }
    }
    return blocks
  }

  private renderHtmlBlock(
    token: Token,
    key: string,
    opts: { isLast: boolean; complete: boolean; linkMarkdownPaths: boolean },
  ): RenderedHtmlBlock {
    const { isLast, complete, linkMarkdownPaths } = opts
    const rawKey = this.cacheKey(token.raw, linkMarkdownPaths)

    if (!isLast || complete) {
      let blockHtml = this.cache.get(rawKey)
      if (blockHtml === undefined) {
        blockHtml = renderBlockToken(token)
        if (linkMarkdownPaths) {
          blockHtml = linkPathMentions(blockHtml)
        }
        this.cache.set(rawKey, blockHtml)
      }
      return { key, html: blockHtml }
    }

    // Live tail: render but do not cache.
    let blockHtml = renderBlockToken(token)
    if (linkMarkdownPaths) {
      blockHtml = linkPathMentions(blockHtml)
    }
    return { key, html: blockHtml }
  }

  private renderListBlock(
    token: Tokens.List,
    key: string,
    opts: { isLast: boolean; complete: boolean; linkMarkdownPaths: boolean },
  ): RenderedListBlock {
    const { isLast, complete, linkMarkdownPaths } = opts
    const items = token.items
    const lastItemIdx = items.length - 1

    const renderedItems = items.map((item, idx) => {
      const itemIsLast = idx === lastItemIdx
      const itemKey = `${key}:item:${idx}`
      const rawKey = this.cacheKey(item.raw, linkMarkdownPaths, token.loose)

      // An item is completed (and therefore cached) when:
      //  - it is not the final item of the list, OR
      //  - the list itself is not the final top-level block (a following
      //    block means the list — and thus its final item — is done), OR
      //  - the stream has ended (complete=true).
      // Otherwise the item is the live tail and is re-rendered each delta
      // without being cached.
      const itemCompleted = !itemIsLast || !isLast || complete

      if (itemCompleted) {
        let itemHtml = this.cache.get(rawKey)
        if (itemHtml === undefined) {
          itemHtml = renderListItemInnerHtml(item, token)
          if (linkMarkdownPaths) {
            itemHtml = linkPathMentions(itemHtml)
          }
          this.cache.set(rawKey, itemHtml)
        }
        return { key: itemKey, html: itemHtml }
      }

      // Live tail item: render but do not cache.
      let itemHtml = renderListItemInnerHtml(item, token)
      if (linkMarkdownPaths) {
        itemHtml = linkPathMentions(itemHtml)
      }
      return { key: itemKey, html: itemHtml }
    })

    const start = typeof token.start === 'number' ? token.start : 1

    return {
      key,
      list: {
        ordered: token.ordered,
        start,
        items: renderedItems,
      },
    }
  }

  /** Number of cached blocks/items. */
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
    return this.cache.has(this.cacheKey(raw, true)) ||
      this.cache.has(this.cacheKey(raw, false))
  }
}

/** Join rendered blocks back into a single HTML string (for tests and
 *  callers that need the legacy concatenated output).
 *
 *  The list output mirrors marked's own formatting: a newline after the
 *  opening tag, between ``<li>`` elements, and before the closing tag, so
 *  that ``joinBlocks(cache.render(src))`` equals ``marked.parse(src)``.
 */
export function joinBlocks(blocks: RenderedBlock[]): string {
  return blocks.map((b) => {
    if ('list' in b) {
      const tag = b.list.ordered ? 'ol' : 'ul'
      const startAttr = b.list.ordered && b.list.start !== 1
        ? ` start="${b.list.start}"`
        : ''
      const items = b.list.items.map((it) => `<li>${it.html}</li>`).join('\n')
      return `<${tag}${startAttr}>\n${items}\n</${tag}>\n`
    }
    return b.html
  }).join('')
}
