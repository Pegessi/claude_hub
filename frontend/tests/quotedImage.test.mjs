import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// quotedImage.ts has zero runtime imports, so transpile + data-URL load is
// enough.
const source = await readFile(
  new URL('../src/utils/quotedImage.ts', import.meta.url),
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
const {
  isBareQuotedImageKey,
  quotedImageProviderName,
  resolveQuotedImageUrl,
  rewriteQuotedImageSrcs,
  QUOTED_IMAGE_MATCHERS,
  LARK_IMG_V3_MATCHER,
} = mod

const KEY = 'img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g'

// ── matcher registry shape / extension point ──────────────────────────────

test('registry ships the lark matcher as the first provider', () => {
  assert.ok(Array.isArray(QUOTED_IMAGE_MATCHERS) || QUOTED_IMAGE_MATCHERS.length >= 1)
  assert.equal(QUOTED_IMAGE_MATCHERS[0].name, 'lark-img-v3')
  assert.equal(LARK_IMG_V3_MATCHER.name, 'lark-img-v3')
})

// ── isBareQuotedImageKey ──────────────────────────────────────────────────

test('accepts a bare img_v3 key with dashes/underscores/digits', () => {
  assert.equal(isBareQuotedImageKey(KEY), true)
  assert.equal(isBareQuotedImageKey('img_v3_abc-123_XYZ'), true)
})

test('rejects non-strings and empty/whitespace', () => {
  assert.equal(isBareQuotedImageKey(undefined), false)
  assert.equal(isBareQuotedImageKey(null), false)
  assert.equal(isBareQuotedImageKey(42), false)
  assert.equal(isBareQuotedImageKey(''), false)
  assert.equal(isBareQuotedImageKey('   '), false)
  assert.equal(isBareQuotedImageKey(` ${KEY}`), false)
  assert.equal(isBareQuotedImageKey(`${KEY} `), false)
})

test('rejects wrong / unregistered provider prefix', () => {
  assert.equal(isBareQuotedImageKey('img_v2_abc'), false)
  assert.equal(isBareQuotedImageKey('photo.jpg'), false)
  assert.equal(isBareQuotedImageKey('img_v3_'), false)
  // Bare tokens from other providers are not proxied until a matcher exists.
  assert.equal(isBareQuotedImageKey('slack_F1234567890ABCDE'), false)
  assert.equal(isBareQuotedImageKey('ding_abc-123'), false)
  assert.equal(isBareQuotedImageKey('cdn_xyz_789'), false)
  assert.equal(isBareQuotedImageKey('tmpimg_42'), false)
})

test('rejects paths, schemes and metacharacters', () => {
  assert.equal(isBareQuotedImageKey('img_v3_a/b'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a\\b'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a:80'), false)
  assert.equal(isBareQuotedImageKey('img_v3_../etc'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a*'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a?b'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a b'), false)
  assert.equal(isBareQuotedImageKey('img_v3_a&b'), false)
})

test('rejects oversized keys', () => {
  assert.equal(isBareQuotedImageKey(`img_v3_${'a'.repeat(300)}`), false)
})

// ── quotedImageProviderName (dispatch) ────────────────────────────────────

test('provider dispatch identifies lark and returns null otherwise', () => {
  assert.equal(quotedImageProviderName(KEY), 'lark-img-v3')
  assert.equal(quotedImageProviderName('https://example.com/a.png'), null)
  assert.equal(quotedImageProviderName('slack_x'), null)
})

// ── resolveQuotedImageUrl ─────────────────────────────────────────────────

test('endpoint URL embeds encoded tab id and key', () => {
  const url = resolveQuotedImageUrl('tab a/1', KEY)
  assert.equal(
    url,
    `/api/workspaces/tabs/${encodeURIComponent('tab a/1')}/stream/quoted-image?key=${encodeURIComponent(KEY)}`,
  )
})

test('resolve returns null for non-bare/foreign srcs and empty tab', () => {
  assert.equal(resolveQuotedImageUrl('tab-a', 'https://x/a.png'), null)
  assert.equal(resolveQuotedImageUrl('tab-a', 'data:image/png;base64,A'), null)
  assert.equal(resolveQuotedImageUrl('tab-a', 'slack_x'), null)
  assert.equal(resolveQuotedImageUrl('', KEY), null)
})

// ── rewriteQuotedImageSrcs ────────────────────────────────────────────────

test('rewrites a bare img_v3 img src and tags the element', () => {
  const html = `<p><img src="${KEY}" alt="Image"></p>`
  const out = rewriteQuotedImageSrcs(html, 'tab-a')
  assert.match(
    out,
    new RegExp(
      `<img src="/api/workspaces/tabs/tab-a/stream/quoted-image\\?key=${KEY}"[^>]*data-quoted-img="${KEY}"[^>]*alt="Image">`,
    ),
  )
})

test('rewrites single-quoted src too', () => {
  const out = rewriteQuotedImageSrcs(`<img src='${KEY}'>`, 'tab-a')
  // The src keeps its single quotes; the injected marker attribute is always
  // double-quoted (the bare-token charset contains no quotes).
  assert.equal(
    out,
    `<img src='/api/workspaces/tabs/tab-a/stream/quoted-image?key=${KEY}' data-quoted-img="${KEY}">`,
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
    assert.equal(rewriteQuotedImageSrcs(html, 'tab-a'), html, html)
  }
})

test('does not proxy malformed or foreign bare srcs', () => {
  for (const src of ['img_v3_../../etc/passwd', 'slack_F1234567890ABCDE']) {
    const html = `<img src="${src}">`
    assert.equal(rewriteQuotedImageSrcs(html, 'tab-a'), html, src)
  }
})

test('rewrites only img srcs, not anchors or text', () => {
  const html = `<p>${KEY} <a href="${KEY}">link</a> <img src="${KEY}"></p>`
  const out = rewriteQuotedImageSrcs(html, 'tab-a')
  assert.ok(out.includes(`<a href="${KEY}">`))
  assert.ok(out.includes(`<p>${KEY}`))
  assert.ok(out.includes('stream/quoted-image?key='))
})

test('rewrites multiple images in one html string', () => {
  const html = `<img src="${KEY}"><img src="img_v3_other-key">`
  const out = rewriteQuotedImageSrcs(html, 'tab-a')
  const matches = out.match(/stream\/quoted-image\?key=/g)
  assert.equal(matches?.length, 2)
})

test('no tab id or empty html is a passthrough', () => {
  const html = `<img src="${KEY}">`
  assert.equal(rewriteQuotedImageSrcs(html, ''), html)
  assert.equal(rewriteQuotedImageSrcs('', 'tab-a'), '')
})
