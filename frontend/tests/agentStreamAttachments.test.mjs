import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// agentStreamAttachments.ts has no runtime imports (only File/FileReader DOM
// globals), so it transpiles and loads cleanly.
const source = await readFile(
  new URL('../src/utils/agentStreamAttachments.ts', import.meta.url),
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
const { validateImageAttachment } = mod

function fakeFile({ name = 'img.png', type = 'image/png', size = 1024 } = {}) {
  // Minimal File-like object; validateImageAttachment only reads .type and .size.
  return { name, type, size }
}

test('accepts a valid PNG under the size limit', () => {
  const err = validateImageAttachment(fakeFile({ type: 'image/png', size: 1024 }))
  assert.equal(err, null)
})

test('accepts JPEG, GIF, WebP, BMP', () => {
  for (const type of ['image/jpeg', 'image/gif', 'image/webp', 'image/bmp']) {
    assert.equal(
      validateImageAttachment(fakeFile({ type })),
      null,
      `${type} should be accepted`,
    )
  }
})

test('rejects unsupported mime types', () => {
  for (const type of ['text/plain', 'application/pdf', 'image/svg+xml', 'image/tiff']) {
    const err = validateImageAttachment(fakeFile({ type }))
    assert.ok(err, `${type} should be rejected`)
    assert.ok(err.includes('Unsupported image type'))
  }
})

test('rejects files over 8 MB', () => {
  const over = 8 * 1024 * 1024 + 1
  const err = validateImageAttachment(fakeFile({ size: over }))
  assert.ok(err)
  assert.ok(err.includes('8 MB'))
})

test('accepts a file exactly at the 8 MB limit', () => {
  const atLimit = 8 * 1024 * 1024
  const err = validateImageAttachment(fakeFile({ size: atLimit }))
  assert.equal(err, null)
})

test('rejects files with empty/unknown mime type', () => {
  const err = validateImageAttachment(fakeFile({ type: '' }))
  assert.ok(err)
  assert.ok(err.includes('unknown'))
})
