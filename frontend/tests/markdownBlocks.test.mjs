import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { hrtime } from 'node:process'
import test from 'node:test'
import ts from 'typescript'
import { marked } from 'marked'

// ── DOMPurify mock ──────────────────────────────────────────────────────
// DOMPurify requires a DOM; in Node tests we stub it to return the input
// unchanged (marked output is safe for our fixtures) and count calls.
let sanitizeCalls = 0
const mockDOMPurify = {
  sanitize: (html) => {
    sanitizeCalls++
    return html
  },
}

// ── Load markdownBlocks.ts with mocked dompurify ────────────────────────
const source = await readFile(
  new URL('../src/utils/markdownBlocks.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})

// Replace the marked and dompurify imports with globals so the transpiled
// module (loaded via data URL, which has no node_modules resolution) uses
// the real marked and our DOMPurify mock.
const mocked = outputText
  .replace(
    /import \{ marked \} from ['"]marked['"];?/,
    'const marked = globalThis.__marked;',
  )
  .replace(
    /import DOMPurify from ['"]dompurify['"];?/,
    'const DOMPurify = globalThis.__mockDOMPurify;',
  )

globalThis.__marked = marked
globalThis.__mockDOMPurify = mockDOMPurify

const mod = await import(
  `data:text/javascript;base64,${Buffer.from(mocked).toString('base64')}`
)
const { splitBlockTokens, MarkdownBlockCache, joinBlocks } = mod

// Spy on marked.parser to count parse calls.
let parserCalls = 0
const originalParser = marked.parser.bind(marked)
marked.parser = (tokens, opt) => {
  parserCalls++
  return originalParser(tokens, opt)
}

function resetCounters() {
  sanitizeCalls = 0
  parserCalls = 0
}

// Convenience: render and join blocks back to a single HTML string.
function renderString(cache, src, opts) {
  return joinBlocks(cache.render(src, opts))
}

// ── Block splitting correctness ─────────────────────────────────────────

test('splitBlockTokens drops space tokens', () => {
  const tokens = splitBlockTokens('# Hello\n\nWorld')
  assert.deepEqual(
    tokens.map((t) => t.type),
    ['heading', 'paragraph'],
  )
})

test('fenced code is a single block token', () => {
  const src = '```js\nconst x = 1;\n```'
  const tokens = splitBlockTokens(src)
  assert.equal(tokens.length, 1)
  assert.equal(tokens[0].type, 'code')
})

test('list items are grouped into one list block', () => {
  const src = '- a\n- b\n- c'
  const tokens = splitBlockTokens(src)
  assert.equal(tokens.length, 1)
  assert.equal(tokens[0].type, 'list')
})

test('blockquote is a single block token', () => {
  const src = '> quote line 1\n> quote line 2'
  const tokens = splitBlockTokens(src)
  assert.equal(tokens.length, 1)
  assert.equal(tokens[0].type, 'blockquote')
})

// ── Marked options preserved (gfm + breaks) ─────────────────────────────

test('single newline renders as <br> (breaks:true preserved)', () => {
  const src = 'line1\nline2'
  const cache = new MarkdownBlockCache()
  const html = renderString(cache, src)
  assert.ok(html.includes('<br>'), `expected <br> in ${JSON.stringify(html)}`)
})

test('rendered output equals marked.parse with gfm+breaks', () => {
  const fixtures = [
    '# Heading\n\nParagraph',
    '```\ncode\n```',
    '- a\n- b',
    '> quote',
    'line1\nline2',
    '| a | b |\n|---|---|\n| 1 | 2 |',
  ]
  for (const src of fixtures) {
    const cache = new MarkdownBlockCache()
    const actual = renderString(cache, src)
    const expected = marked.parse(src, { gfm: true, breaks: true })
    assert.equal(
      actual,
      expected,
      `mismatch for fixture ${JSON.stringify(src)}`,
    )
  }
})

// ── Per-block caching: completed blocks parsed once ─────────────────────

test('completed blocks are cached and not re-parsed on growth', () => {
  resetCounters()
  const cache = new MarkdownBlockCache()

  // First render: one paragraph (live tail, not cached).
  renderString(cache, 'First paragraph')
  assert.equal(cache.size, 0, 'single live tail should not be cached')

  // Add a second paragraph. The first is now complete → parsed once and
  // cached. The second is the live tail → parsed once. Total 2 parses.
  resetCounters()
  renderString(cache, 'First paragraph\n\nSecond paragraph')
  assert.equal(parserCalls, 2, 'first cached + second live tail = 2 parses')
  assert.equal(cache.size, 1, 'first block should now be cached')

  // Grow only the live tail (second block). First block is cached → 0
  // parses; second block is still the live tail → 1 parse.
  resetCounters()
  renderString(cache, 'First paragraph\n\nSecond paragraph extended')
  assert.equal(parserCalls, 1, 'only the live tail is re-parsed')
  assert.equal(sanitizeCalls, 1, 'sanitize tracks parser 1:1')
  assert.equal(cache.size, 1, 'cache size unchanged')

  // Add a third paragraph. Second block is promoted (parsed + cached),
  // third is the live tail. First block stays cached. Total 2 parses.
  resetCounters()
  renderString(cache, 'First paragraph\n\nSecond paragraph extended\n\nThird')
  assert.equal(parserCalls, 2, 'second promoted + third live = 2 parses')
  assert.equal(cache.size, 2, 'first two blocks cached')

  // Grow only the third block. First two cached → 0; third live → 1.
  resetCounters()
  renderString(cache, 'First paragraph\n\nSecond paragraph extended\n\nThird more')
  assert.equal(parserCalls, 1, 'only the live tail is re-parsed')
  assert.equal(cache.size, 2, 'cache size unchanged')
})

test('cache size grows with completed blocks, not deltas', () => {
  const cache = new MarkdownBlockCache()
  // Simulate a long stream where only the last block grows.
  let text = 'Block A'
  renderString(cache, text)
  assert.equal(cache.size, 0, 'single live tail block should not be cached')

  // Append to the live tail many times — cache must stay empty.
  for (let i = 0; i < 100; i++) {
    text += ` word${i}`
    renderString(cache, text)
  }
  assert.equal(
    cache.size,
    0,
    `cache should stay empty while only the tail grows, got size ${cache.size}`,
  )

  // Now add a second block: the first becomes complete and is cached.
  text += '\n\nBlock B'
  renderString(cache, text)
  assert.equal(cache.size, 1, 'first block should be cached once a second exists')

  // Grow the second (tail) block many times — cache stays at 1.
  for (let i = 0; i < 100; i++) {
    text += ` word${i}`
    renderString(cache, text)
  }
  assert.equal(
    cache.size,
    1,
    `cache should stay at 1 while only the tail grows, got size ${cache.size}`,
  )
})

test('complete=true caches the final block too', () => {
  const cache = new MarkdownBlockCache()
  renderString(cache, 'Only block', { complete: true })
  assert.equal(cache.size, 1, 'final block should be cached when complete=true')
})

test('live tail is not cached even across many growing updates', () => {
  resetCounters()
  const cache = new MarkdownBlockCache()
  let text = 'tail'
  // 2000 growing deltas on a single block.
  for (let i = 0; i < 2000; i++) {
    text += 'x'
    renderString(cache, text)
  }
  // Cache must be empty (only the live tail exists).
  assert.equal(cache.size, 0, 'live tail must not be cached')
  // Parse calls = number of renders (each re-parses the live tail), but
  // cache size stays 0 — no memory leak.
  assert.ok(parserCalls > 0, 'parser should have been called for the live tail')
})

test('thousands of growing tail updates keep cache bounded to completed blocks', () => {
  const cache = new MarkdownBlockCache()
  let text = 'P1'
  renderString(cache, text)

  // Grow P1 5000 times.
  for (let i = 0; i < 5000; i++) {
    text += 'a'
    renderString(cache, text)
  }
  assert.equal(cache.size, 0, 'no completed blocks yet → cache empty')

  // Add P2: P1 becomes cached.
  text += '\n\nP2'
  renderString(cache, text)
  assert.equal(cache.size, 1)

  // Grow P2 5000 times.
  for (let i = 0; i < 5000; i++) {
    text += 'b'
    renderString(cache, text)
  }
  assert.equal(cache.size, 1, 'only P1 is cached; P2 is live tail')

  // Add P3: P2 cached.
  text += '\n\nP3'
  renderString(cache, text)
  assert.equal(cache.size, 2)
})

// ── Index-stable block keys (no duplicate keys for identical blocks) ────

test('block keys are index-based and unique even for identical raw text', () => {
  const cache = new MarkdownBlockCache()
  // Two identical paragraphs.
  const blocks = cache.render('Hello\n\nHello')
  assert.equal(blocks.length, 2)
  assert.equal(blocks[0].key, 'block:0')
  assert.equal(blocks[1].key, 'block:1')
  assert.notEqual(blocks[0].key, blocks[1].key, 'identical blocks must have different keys')
})

test('completed block key stays stable (index-based) while live tail grows', () => {
  const cache = new MarkdownBlockCache()

  const first = cache.render('P1\n\nP2')
  assert.equal(first[0].key, 'block:0')
  assert.equal(first[1].key, 'block:1')

  // Grow the live tail. The completed block's key must not change.
  const second = cache.render('P1\n\nP2 extended')
  assert.equal(second[0].key, 'block:0', 'completed block key is index-based and stable')
  assert.equal(second[1].key, 'block:1', 'live tail key stays at its index')
})

test('live tail key is stable while it grows (same index)', () => {
  const cache = new MarkdownBlockCache()
  let text = 'tail'
  for (let i = 0; i < 10; i++) {
    text += 'x'
    const blocks = cache.render(text)
    assert.equal(blocks[0].key, 'block:0', 'single block always at index 0')
  }
})

test('when a new block appears, the previous tail keeps its index key', () => {
  const cache = new MarkdownBlockCache()

  const first = cache.render('P1')
  assert.equal(first[0].key, 'block:0')

  const second = cache.render('P1\n\nP2')
  // P1 is now completed but stays at index 0 — same key, so Vue reuses
  // its DOM node.
  assert.equal(second[0].key, 'block:0')
  assert.equal(second[1].key, 'block:1')
})

// ── Completed block descriptor stability ────────────────────────────────

test('completed block html stays stable (cached) while live tail grows', () => {
  const cache = new MarkdownBlockCache()

  const first = cache.render('P1\n\nP2')
  const completedHtml = first[0].html

  const second = cache.render('P1\n\nP2 extended')
  assert.equal(second[0].html, completedHtml, 'completed block html is cached and stable')
})

test('completed blocks are not re-parsed when only the live tail changes', () => {
  const cache = new MarkdownBlockCache()
  cache.render('P1\n\nP2')

  resetCounters()
  cache.render('P1\n\nP2 more text')
  assert.equal(parserCalls, 1, 'only the live tail is parsed; completed blocks are cached')
})

// ── linkMarkdownPaths cache identity ────────────────────────────────────

test('cache is invalidated when linkMarkdownPaths mode changes', () => {
  const cache = new MarkdownBlockCache()

  // Render without link wrapping.
  cache.render('See foo.md for details\n\nMore text')
  assert.equal(cache.size, 1)

  // Switch to link wrapping. The cache must be cleared so the completed
  // block is re-rendered with links.
  resetCounters()
  cache.render('See foo.md for details\n\nMore text', { linkMarkdownPaths: true })
  assert.equal(parserCalls, 2, 'both blocks re-parsed after mode switch (cache cleared)')
  assert.equal(cache.size, 1)
})

test('linkMarkdownPaths mode is part of the cache identity (cache invalidates on mode switch)', () => {
  const cache = new MarkdownBlockCache()

  // Render without link wrapping. The completed block is cached under the
  // "no-link" cache key.
  cache.render('See foo.md for details\n\nMore text')
  assert.equal(cache.size, 1)

  // Switch to link wrapping. The cache must be cleared (different mode),
  // so the completed block is re-parsed under the "link" cache key.
  resetCounters()
  cache.render('See foo.md for details\n\nMore text', { linkMarkdownPaths: true })
  assert.equal(parserCalls, 2, 'both blocks re-parsed after mode switch (cache cleared)')
  assert.equal(cache.size, 1)

  // Switch back to no-link. Cache must be cleared again — the mode is part
  // of the cache identity, so the previously cached "link" entry is stale.
  resetCounters()
  cache.render('See foo.md for details\n\nMore text')
  assert.equal(parserCalls, 2, 'cache invalidated again when switching back to no-link')
})

// ── Joined HTML correctness ─────────────────────────────────────────────

test('joined block HTML exactly equals the legacy concatenated output', () => {
  const fixtures = [
    '# Heading\n\nParagraph',
    '```\ncode\n```',
    '- a\n- b',
    '> quote',
    'line1\nline2',
    '| a | b |\n|---|---|\n| 1 | 2 |',
    'P1\n\nP2\n\nP3',
  ]
  for (const src of fixtures) {
    const cache = new MarkdownBlockCache()
    const actual = joinBlocks(cache.render(src))
    const expected = marked.parse(src, { gfm: true, breaks: true })
    assert.equal(
      actual,
      expected,
      `joined HTML must equal marked.parse for ${JSON.stringify(src)}`,
    )
  }
})

test('complete=true gives every block an index key (no sentinel)', () => {
  const cache = new MarkdownBlockCache()
  const blocks = cache.render('P1\n\nP2', { complete: true })
  assert.equal(blocks.length, 2)
  assert.equal(blocks[0].key, 'block:0')
  assert.equal(blocks[1].key, 'block:1')
})

// ── Long-list performance (lists split into keyed items) ────────────────
// marked treats a contiguous list as a single top-level token. We split it
// into its marked-parsed ``items`` so each completed ``<li>`` is cached and
// left untouched by Vue; only the final (still-growing) item is re-rendered
// per delta. This bounds the DOM work for long streamed lists to the size
// of the final item, not the whole list.
//
// Parser-call accounting per delta:
//   * Item-boundary delta (a new ``- item`` line is appended): the previous
//     final item freezes and is parsed+cached once, and the new final item
//     is parsed once → 2 parser calls.
//   * In-item growth delta (text is appended to the final item): only the
//     final (live) item is re-parsed → 1 parser call.
// Completed items before the final one are served from the cache and never
// re-parsed. The actual >50ms Long Task claim is measured in the Playwright
// browser run, not here.

test('long streamed list: item-boundary deltas cost 2 parser calls, in-item growth costs 1', () => {
  const cache = new MarkdownBlockCache()
  const ITEM_COUNT = 50

  let src = '- item 0'
  cache.render(src)

  // Grow the list one item at a time (item-boundary deltas). Each delta
  // freezes the previous final item (1 parse) and renders the new final
  // item (1 parse) → 2 parser calls.
  for (let i = 1; i < ITEM_COUNT; i++) {
    src += `\n- item ${i}`
    resetCounters()
    cache.render(src)
    assert.equal(
      parserCalls,
      2,
      `item-boundary delta ${i}: freeze previous + render new final = 2 parses, got ${parserCalls}`,
    )
  }

  // Now append text to the final item (in-item growth). Only the final
  // (live) item is re-parsed → 1 parser call.
  for (let k = 0; k < 10; k++) {
    src += ` more${k}`
    resetCounters()
    cache.render(src)
    assert.equal(
      parserCalls,
      1,
      `in-item growth delta ${k}: only the live final item is parsed, got ${parserCalls}`,
    )
  }
})

test('long streamed list: completed items are cached and not re-parsed', () => {
  const cache = new MarkdownBlockCache()
  // A completed paragraph followed by a growing list.
  const prefix = 'Intro paragraph.\n\n'
  let src = prefix + '- item 0'
  cache.render(src)

  resetCounters()
  // Grow the list. The intro paragraph is cached and must not be re-parsed.
  for (let i = 1; i < 50; i++) {
    src += `\n- item ${i}`
    cache.render(src)
  }

  // 49 item-boundary deltas × 2 parses each = 98. The intro paragraph is
  // cached (0 parses). No completed list item is ever re-parsed.
  assert.equal(parserCalls, 98, '49 item-boundary deltas × 2 parses = 98; intro stays cached')
})

test('long list: cache holds one entry per completed item, not one per list', () => {
  const cache = new MarkdownBlockCache()
  const list = Array.from({ length: 50 }, (_, i) => `- item ${i}`).join('\n')
  const src = `${list}\n\nAfter list`

  // First render: the list is followed by another block, so every list item
  // is completed and cached (one cache entry per item). The "After list"
  // paragraph is the live tail.
  cache.render(src)
  assert.equal(cache.size, 50, 'one cache entry per completed list item (50 items)')

  resetCounters()
  // Grow the live tail. No list item is re-parsed.
  cache.render(`${src} more`)
  assert.equal(parserCalls, 1, 'cached list items are not re-parsed; only the live tail is')
})

test('long streamed list: cache size equals completed items; complete=true caches the final item', () => {
  const cache = new MarkdownBlockCache()
  const ITEM_COUNT = 100

  let src = '- item 0'
  for (let i = 1; i < ITEM_COUNT; i++) {
    src += `\n- item ${i}`
  }
  // The list is the only (and therefore last) block, complete=false:
  // items 0..98 are cached (99 entries), item 99 is the live tail.
  cache.render(src)
  assert.equal(cache.size, ITEM_COUNT - 1, '99 completed items cached; final item is live tail')

  // complete=true: the final item is also cached.
  resetCounters()
  cache.render(src, { complete: true })
  assert.equal(cache.size, ITEM_COUNT, 'complete=true caches the final item too (100 entries)')
})

// ── List rendering correctness (matches marked.parse exactly) ───────────

test('ordered list start attribute is preserved', () => {
  const cache = new MarkdownBlockCache()
  const src = '3. a\n4. b'
  const actual = joinBlocks(cache.render(src))
  const expected = marked.parse(src, { gfm: true, breaks: true })
  assert.equal(actual, expected)
  // And the block descriptor carries the start value.
  const blocks = cache.render(src)
  assert.equal(blocks[0].list.start, 3)
})

test('nested list renders identically to marked.parse', () => {
  const cache = new MarkdownBlockCache()
  const src = '- a\n  - nested\n- c'
  const actual = joinBlocks(cache.render(src))
  const expected = marked.parse(src, { gfm: true, breaks: true })
  assert.equal(actual, expected)
})

test('loose list (blank line between items) renders identically to marked.parse', () => {
  const cache = new MarkdownBlockCache()
  const src = '- a\n\n- b'
  const actual = joinBlocks(cache.render(src))
  const expected = marked.parse(src, { gfm: true, breaks: true })
  assert.equal(actual, expected)
})

test('task list checkboxes render identically to marked.parse', () => {
  const cache = new MarkdownBlockCache()
  const src = '- [ ] todo\n- [x] done'
  const actual = joinBlocks(cache.render(src))
  const expected = marked.parse(src, { gfm: true, breaks: true })
  assert.equal(actual, expected)
})

test('list item with emphasis/links renders identically to marked.parse', () => {
  const cache = new MarkdownBlockCache()
  const src = '- **bold** and [link](https://example.com)\n- `code`'
  const actual = joinBlocks(cache.render(src))
  const expected = marked.parse(src, { gfm: true, breaks: true })
  assert.equal(actual, expected)
})

// ── Tight-to-loose list transition (list.loose is list-wide) ────────────
// ``list.loose`` is a property of the whole list, not individual items. When
// a blank-line-separated item is appended, marked flips the entire list from
// tight to loose, which wraps every item's content in ``<p>``. The item
// cache key must include the list's ``loose`` flag so already-cached items
// are re-rendered (not served stale tight HTML) after the transition.

test('tight-to-loose list transition re-renders cached items with <p> wrapping', () => {
  const cache = new MarkdownBlockCache()
  // Tight list: items a and b are cached without <p> wrapping.
  let src = '- a\n- b'
  let blocks = cache.render(src)
  assert.equal(blocks[0].list.items[0].html, 'a', 'tight list item a has no <p> wrapper')
  assert.equal(blocks[0].list.items[1].html, 'b', 'tight list item b has no <p> wrapper')

  // Append a blank line + new item → marked makes the whole list loose.
  src += '\n\n- c'
  blocks = cache.render(src)

  // Items a and b must now be wrapped in <p> (loose list), not served the
  // stale tight HTML from the cache.
  assert.equal(
    blocks[0].list.items[0].html,
    '<p>a</p>\n',
    'item a re-rendered with <p> wrapping after list became loose',
  )
  assert.equal(
    blocks[0].list.items[1].html,
    '<p>b</p>\n',
    'item b re-rendered with <p> wrapping after list became loose',
  )

  // The full joined output must match a fresh marked.parse.
  assert.equal(
    joinBlocks(blocks),
    marked.parse(src, { gfm: true, breaks: true }),
    'joined output matches marked.parse after tight-to-loose transition',
  )
})

test('loose list stays loose as more items are appended (no stale tight entries)', () => {
  const cache = new MarkdownBlockCache()
  let src = '- a\n\n- b'
  cache.render(src)

  // Append more items; the list stays loose.
  src += '\n- c\n- d'
  const blocks = cache.render(src)

  // Every item must have <p> wrapping (loose).
  for (const item of blocks[0].list.items) {
    assert.ok(
      item.html.startsWith('<p>'),
      `loose list item must start with <p>, got ${JSON.stringify(item.html)}`,
    )
  }
  assert.equal(joinBlocks(blocks), marked.parse(src, { gfm: true, breaks: true }))
})

// ── DOM-promotion: completed <li> HTML is stable while the final item grows ──

test('earlier list items HTML does not change while only the final item grows', () => {
  const cache = new MarkdownBlockCache()
  let src = '- item 0\n- item 1\n- item 2'

  const first = cache.render(src)
  const firstList = first[0].list
  // Snapshot the HTML of every item except the final (live) one.
  const frozenHtml = firstList.items.slice(0, -1).map((it) => it.html)

  // Grow only the final item.
  src += ' and more text'
  const second = cache.render(src)
  const secondList = second[0].list

  // The completed items' HTML must be byte-for-byte identical (cached,
  // never re-rendered). Only the final item's HTML changes.
  secondList.items.slice(0, -1).forEach((it, idx) => {
    assert.equal(
      it.html,
      frozenHtml[idx],
      `completed item ${idx} HTML must not change when only the final item grows`,
    )
  })
  assert.notEqual(
    secondList.items[secondList.items.length - 1].html,
    firstList.items[firstList.items.length - 1].html,
    'the final (live) item HTML does change',
  )
})

test('long streamed list: per-delta render time is informational (not asserted)', () => {
  const cache = new MarkdownBlockCache()
  const ITEM_COUNT = 100

  let src = ''
  let maxRenderMs = 0
  for (let i = 0; i < ITEM_COUNT; i++) {
    src += (src ? '\n' : '') + `- item ${i}`
    const start = hrtime.bigint()
    cache.render(src)
    const elapsedMs = Number(hrtime.bigint() - start) / 1e6
    if (elapsedMs > maxRenderMs) maxRenderMs = elapsedMs
  }

  // Informational only. The real >50ms Long Task assertion lives in the
  // Playwright E2E run. Here we just log the observed max.
  console.log(
    `[informational] max per-delta list render for ${ITEM_COUNT} items: ${maxRenderMs.toFixed(2)}ms`,
  )
})
