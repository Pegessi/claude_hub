import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/attachmentHistory.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { markAttachmentNavigation, isAttachmentNavigation, backFromAttachmentPreview } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

const URL_A = 'http://hub/?tab=A'
const URL_B = 'http://hub/?tab=B'

function mockWindow(t, { href = URL_A, state = null } = {}) {
  const previous = globalThis.window
  const listeners = []
  let backs = 0
  globalThis.window = {
    location: { href },
    history: { state, back() { backs++ } },
    addEventListener(type, callback, options) { listeners.push({ type, callback, options }) },
  }
  t.after(() => { globalThis.window = previous })
  return { listeners, backCount: () => backs }
}

test('only the explicitly marked same-URL event skips Chat routing', t => {
  mockWindow(t)
  const previewBack = { state: null }
  markAttachmentNavigation(previewBack, URL_A)
  assert.equal(isAttachmentNavigation(previewBack), true)
  assert.equal(isAttachmentNavigation({ state: null }), false)
  globalThis.window.location.href = URL_B
  const realNavigation = { state: null }
  markAttachmentNavigation(realNavigation, URL_A)
  assert.equal(isAttachmentNavigation(realNavigation), false)
})

test('Forward into a preview marker requires its original URL', t => {
  mockWindow(t)
  assert.equal(isAttachmentNavigation({ state: { attachmentPreview: 'preview-1', attachmentPreviewUrl: URL_A } }), true)
  assert.equal(isAttachmentNavigation({ state: { attachmentPreview: 'preview-1', attachmentPreviewUrl: URL_B } }), false)
  assert.equal(isAttachmentNavigation({ state: { attachmentPreview: 'preview-1' } }), false)
  assert.equal(isAttachmentNavigation({ state: { attachmentPreviewUrl: URL_A } }), false)
})

test('explicit cleanup marks its traversal after the component is gone', t => {
  const { listeners, backCount } = mockWindow(t, { state: { attachmentPreview: 'preview-1' } })
  assert.equal(backFromAttachmentPreview('preview-1', URL_A), true)
  assert.equal(backCount(), 1)
  assert.equal(listeners.length, 1)
  assert.equal(listeners[0].type, 'popstate')
  assert.deepEqual(listeners[0].options, { capture: true, once: true })
  const cleanupBack = { state: null }
  listeners[0].callback(cleanupBack)
  assert.equal(isAttachmentNavigation(cleanupBack), true)
  assert.equal(isAttachmentNavigation({ state: null }), false)
})

test('cleanup never hides a traversal to another real route', t => {
  const { listeners } = mockWindow(t, { state: { attachmentPreview: 'preview-1' } })
  assert.equal(backFromAttachmentPreview('preview-1', URL_A), true)
  globalThis.window.location.href = URL_B
  const realBack = { state: null }
  listeners[0].callback(realBack)
  assert.equal(isAttachmentNavigation(realBack), false)
})

test('stale preview cleanup cannot move history or register a suppression', t => {
  const { listeners, backCount } = mockWindow(t, { state: { attachmentPreview: 'preview-2' } })
  assert.equal(backFromAttachmentPreview('preview-1', URL_A), false)
  globalThis.window.history.state = { attachmentPreview: 'preview-1' }
  globalThis.window.location.href = URL_B
  assert.equal(backFromAttachmentPreview('preview-1', URL_A), false)
  assert.equal(backCount(), 0)
  assert.deepEqual(listeners, [])
})
