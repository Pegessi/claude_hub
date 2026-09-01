import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
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
const { splitBlockTokens, MarkdownBlockCache } = mod

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
  const html = cache.render(src)
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
    const actual = cache.render(src)
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
  cache.render('First paragraph')
  assert.equal(cache.size, 0, 'single live tail should not be cached')

  // Add a second paragraph. The first is now complete → parsed once and
  // cached. The second is the live tail → parsed once. Total 2 parses.
  resetCounters()
  cache.render('First paragraph\n\nSecond paragraph')
  assert.equal(parserCalls, 2, 'first cached + second live tail = 2 parses')
  assert.equal(cache.size, 1, 'first block should now be cached')

  // Grow only the live tail (second block). First block is cached → 0
  // parses; second block is still the live tail → 1 parse.
  resetCounters()
  cache.render('First paragraph\n\nSecond paragraph extended')
  assert.equal(parserCalls, 1, 'only the live tail is re-parsed')
  assert.equal(sanitizeCalls, 1, 'sanitize tracks parser 1:1')
  assert.equal(cache.size, 1, 'cache size unchanged')

  // Add a third paragraph. Second block is promoted (parsed + cached),
  // third is the live tail. First block stays cached. Total 2 parses.
  resetCounters()
  cache.render('First paragraph\n\nSecond paragraph extended\n\nThird')
  assert.equal(parserCalls, 2, 'second promoted + third live = 2 parses')
  assert.equal(cache.size, 2, 'first two blocks cached')

  // Grow only the third block. First two cached → 0; third live → 1.
  resetCounters()
  cache.render('First paragraph\n\nSecond paragraph extended\n\nThird more')
  assert.equal(parserCalls, 1, 'only the live tail is re-parsed')
  assert.equal(cache.size, 2, 'cache size unchanged')
})

test('cache size grows with completed blocks, not deltas', () => {
  const cache = new MarkdownBlockCache()
  // Simulate a long stream where only the last block grows.
  let text = 'Block A'
  cache.render(text)
  assert.equal(cache.size, 0, 'single live tail block should not be cached')

  // Append to the live tail many times — cache must stay empty.
  for (let i = 0; i < 100; i++) {
    text += ` word${i}`
    cache.render(text)
  }
  assert.equal(
    cache.size,
    0,
    `cache should stay empty while only the tail grows, got size ${cache.size}`,
  )

  // Now add a second block: the first becomes complete and is cached.
  text += '\n\nBlock B'
  cache.render(text)
  assert.equal(cache.size, 1, 'first block should be cached once a second exists')

  // Grow the second (tail) block many times — cache stays at 1.
  for (let i = 0; i < 100; i++) {
    text += ` word${i}`
    cache.render(text)
  }
  assert.equal(
    cache.size,
    1,
    `cache should stay at 1 while only the tail grows, got size ${cache.size}`,
  )
})

test('complete=true caches the final block too', () => {
  const cache = new MarkdownBlockCache()
  cache.render('Only block', true)
  assert.equal(cache.size, 1, 'final block should be cached when complete=true')
})

test('live tail is not cached even across many growing updates', () => {
  resetCounters()
  const cache = new MarkdownBlockCache()
  let text = 'tail'
  // 2000 growing deltas on a single block.
  for (let i = 0; i < 2000; i++) {
    text += 'x'
    cache.render(text)
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
  cache.render(text)

  // Grow P1 5000 times.
  for (let i = 0; i < 5000; i++) {
    text += 'a'
    cache.render(text)
  }
  assert.equal(cache.size, 0, 'no completed blocks yet → cache empty')

  // Add P2: P1 becomes cached.
  text += '\n\nP2'
  cache.render(text)
  assert.equal(cache.size, 1)

  // Grow P2 5000 times.
  for (let i = 0; i < 5000; i++) {
    text += 'b'
    cache.render(text)
  }
  assert.equal(cache.size, 1, 'only P1 is cached; P2 is live tail')

  // Add P3: P2 cached.
  text += '\n\nP3'
  cache.render(text)
  assert.equal(cache.size, 2)
})
