import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// larkImage.ts has zero runtime imports, so transpile + data-URL load is
// enough.
const source = await readFile(
  new URL('../src/utils/larkImage.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { isBareLarkImageKey, larkImageEndpointUrl, rewriteLarkImageSrcs } = mod

const KEY = 'img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g'

// ── isBareLarkImageKey ────────────────────────────────────────────────────

test('accepts a bare img_v3 key with dashes/underscores/digits', () => {
  assert.equal(isBareLarkImageKey(KEY), true)
  assert.equal(isBareLarkImageKey('img_v3_abc-123_XYZ'), true)
})

test('rejects non-strings and empty/whitespace', () => {
  assert.equal(isBareLarkImageKey(undefined), false)
  assert.equal(isBareLarkImageKey(null), false)
  assert.equal(isBareLarkImageKey(42), false)
  assert.equal(isBareLarkImageKey(''), false)
  assert.equal(isBareLarkImageKey('   '), false)
  assert.equal(isBareLarkImageKey(` ${KEY}`), false)
  assert.equal(isBareLarkImageKey(`${KEY} `), false)
})

test('rejects wrong prefix', () => {
  assert.equal(isBareLarkImageKey('img_v2_abc'), false)
  assert.equal(isBareLarkImageKey('photo.jpg'), false)
  assert.equal(isBareLarkImageKey('img_v3_'), false)
})

test('rejects paths, schemes and metacharacters', () => {
  assert.equal(isBareLarkImageKey('img_v3_a/b'), false)
  assert.equal(isBareLarkImageKey('img_v3_a\\b'), false)
  assert.equal(isBareLarkImageKey('img_v3_a:80'), false)
  assert.equal(isBareLarkImageKey('img_v3_../etc'), false)
  assert.equal(isBareLarkImageKey('img_v3_a*'), false)
  assert.equal(isBareLarkImageKey('img_v3_a?b'), false)
  assert.equal(isBareLarkImageKey('img_v3_a b'), false)
  assert.equal(isBareLarkImageKey('img_v3_a&b'), false)
})

test('rejects oversized keys', () => {
  assert.equal(isBareLarkImageKey(`img_v3_${'a'.repeat(300)}`), false)
})

// ── larkImageEndpointUrl ──────────────────────────────────────────────────

test('endpoint URL embeds encoded tab id and key', () => {
  const url = larkImageEndpointUrl('tab a/1', KEY)
  assert.equal(
    url,
    `/api/workspaces/tabs/${encodeURIComponent('tab a/1')}/stream/lark-image?key=${encodeURIComponent(KEY)}`,
  )
})

// ── rewriteLarkImageSrcs ──────────────────────────────────────────────────

test('rewrites a bare img_v3 img src and tags the element', () => {
  const html = `<p><img src="${KEY}" alt="Image"></p>`
  const out = rewriteLarkImageSrcs(html, 'tab-a')
  assert.match(
    out,
    new RegExp(
      `<img src="/api/workspaces/tabs/tab-a/stream/lark-image\\?key=${KEY}"[^>]*data-lark-img="${KEY}"[^>]*alt="Image">`,
    ),
  )
})

test('rewrites single-quoted src too', () => {
  const out = rewriteLarkImageSrcs(`<img src='${KEY}'>`, 'tab-a')
  // The src keeps its single quotes; the injected marker attribute is always
  // double-quoted (the key charset contains no quotes).
  assert.equal(
    out,
    `<img src='/api/workspaces/tabs/tab-a/stream/lark-image?key=${KEY}' data-lark-img="${KEY}">`,
  )
})

test('leaves http(s)/data/blob/relative/absolute srcs untouched', () => {
  const cases = [
    '<img src="https://example.com/a.jpg">',
    '<img src="http://example.com/a.jpg">',
    '<img src="data:image/png;base64,AAAA">',
    '<img src="blob:https://example.com/uuid">',
    '<img src="./shot.png">',
    '<img src="/api/workspaces/tabs/tab-a/stream/attachments/x">',
    '<img src="/Users/me/work/shot.png">',
  ]
  for (const html of cases) {
    assert.equal(rewriteLarkImageSrcs(html, 'tab-a'), html, html)
  }
})

test('does not proxy malformed img_v3-like srcs', () => {
  const html = '<img src="img_v3_../../etc/passwd">'
  assert.equal(rewriteLarkImageSrcs(html, 'tab-a'), html)
})

test('rewrites only img srcs, not anchors or text', () => {
  const html = `<p>${KEY} <a href="${KEY}">link</a> <img src="${KEY}"></p>`
  const out = rewriteLarkImageSrcs(html, 'tab-a')
  assert.ok(out.includes(`<a href="${KEY}">`))
  assert.ok(out.includes(`<p>${KEY}`))
  assert.ok(out.includes('stream/lark-image?key='))
})

test('rewrites multiple images in one html string', () => {
  const html = `<img src="${KEY}"><img src="img_v3_other-key">`
  const out = rewriteLarkImageSrcs(html, 'tab-a')
  const matches = out.match(/stream\/lark-image\?key=/g)
  assert.equal(matches?.length, 2)
})

test('no tab id or empty html is a passthrough', () => {
  const html = `<img src="${KEY}">`
  assert.equal(rewriteLarkImageSrcs(html, ''), html)
  assert.equal(rewriteLarkImageSrcs('', 'tab-a'), '')
})
