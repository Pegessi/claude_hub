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

// ── Long-list performance (lists stay as one block) ─────────────────────
// marked treats a contiguous list as a single top-level token. We do NOT
// split list items (that would be unsafe). Instead we prove deterministically
// that each streaming delta costs exactly one parser/sanitize call (the list
// block is the live tail), regardless of how many items the list contains.
// Completed blocks before the list are cached and never re-parsed. The
// actual >50ms Long Task claim is measured in the Playwright browser run,
// not here.

test('long streamed list: each delta costs exactly one parser call (the list block)', () => {
  const cache = new MarkdownBlockCache()
  const ITEM_COUNT = 100

  let src = '- item 0'
  cache.render(src)

  // Grow the list one item at a time. Each render must invoke the parser
  // exactly once — for the list block, which is the live tail. The number
  // of parser calls is independent of the list length.
  let totalParserCalls = 0
  for (let i = 1; i < ITEM_COUNT; i++) {
    src += `\n- item ${i}`
    resetCounters()
    cache.render(src)
    totalParserCalls += parserCalls
    assert.equal(
      parserCalls,
      1,
      `delta ${i}: exactly one parser call (the list block), got ${parserCalls}`,
    )
  }

  // 99 deltas * 1 parser call each = 99.
  assert.equal(totalParserCalls, ITEM_COUNT - 1)
})

test('long streamed list: completed blocks before the list are not re-parsed', () => {
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

  // Each render parses exactly one block: the list (live tail). The intro
  // paragraph is cached.
  assert.equal(parserCalls, 49, 'only the list block is parsed; intro stays cached')
})

test('long list as a single completed block is cached and not re-parsed', () => {
  const cache = new MarkdownBlockCache()
  const list = Array.from({ length: 50 }, (_, i) => `- item ${i}`).join('\n')
  const src = `${list}\n\nAfter list`

  // First render: list is completed (followed by another block), so it is
  // cached. The "After list" paragraph is the live tail.
  cache.render(src)
  assert.equal(cache.size, 1, 'the 50-item list is cached as one completed block')

  resetCounters()
  // Grow the live tail. The list block must not be re-parsed.
  cache.render(`${src} more`)
  assert.equal(parserCalls, 1, 'cached list block is not re-parsed')
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
