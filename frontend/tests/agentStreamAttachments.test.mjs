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

test('accepts JPEG, GIF, and WebP', () => {
  for (const type of ['image/jpeg', 'image/gif', 'image/webp']) {
    assert.equal(
      validateImageAttachment(fakeFile({ type })),
      null,
      `${type} should be accepted`,
    )
  }
})

test('rejects unsupported mime types', () => {
  for (const type of ['text/plain', 'application/pdf', 'image/svg+xml', 'image/tiff', 'image/bmp']) {
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

function stubImagePipeline({ width, height, sizeFor, webp = true }) {
  const canvases = []
  const encodes = []
  globalThis.Image = class {
    set src(value) {
      this.naturalWidth = width
      this.naturalHeight = height
      queueMicrotask(() => this.onload())
    }
  }
  globalThis.document = {
    createElement(tag) {
      assert.equal(tag, 'canvas')
      const canvas = {
        width: 0,
        height: 0,
        getContext: () => ({ drawImage() {} }),
        toBlob(callback, type, quality) {
          const actual = type === 'image/webp' && !webp ? 'image/png' : type
          encodes.push([canvas.width, canvas.height, type, quality])
          callback({ type: actual, size: sizeFor(canvas.width, canvas.height, actual, quality) })
        },
      }
      canvases.push(canvas)
      return canvas
    },
  }
  globalThis.FileReader = class {
    readAsDataURL(blob) {
      this.result = `data:${blob.type};encoded`
      queueMicrotask(() => this.onload())
    }
  }
  return { canvases, encodes }
}

const MiB = 1024 * 1024

test('an original within the stored-image bounds is kept byte-identical', async () => {
  const { canvases } = stubImagePipeline({ width: 3024, height: 4032, sizeFor: () => 0 })
  const original = 'data:image/jpeg;base64,ORIGINAL'
  const file = fakeFile({ type: 'image/jpeg', size: 8 * MiB })
  assert.equal(await mod.generatePreviewDataUrl(file, original), original)
  assert.equal(canvases.length, 0)
})

test('an oversized image is scaled to the edge bound and kept lossless when PNG fits', async () => {
  const { encodes } = stubImagePipeline({ width: 16384, height: 4096, sizeFor: () => 2 * MiB })
  const preview = await mod.generatePreviewDataUrl(fakeFile({ size: 6 * MiB }), 'data:image/png;base64,X')
  assert.equal(preview, 'data:image/png;encoded')
  assert.deepEqual(encodes, [[8192, 2048, 'image/png', undefined]])
})

test('re-encoding fits the mobile canvas limit, skips missing encoders and keeps high quality', async () => {
  const { encodes } = stubImagePipeline({
    width: 9000,
    height: 9000,
    webp: false,
    sizeFor: (_w, _h, type) => (type === 'image/png' ? 20 * MiB : 3 * MiB),
  })
  const preview = await mod.generatePreviewDataUrl(fakeFile({ size: 7 * MiB }), 'data:image/png;base64,X')
  assert.equal(preview, 'data:image/jpeg;encoded')
  assert.ok(encodes.every(([w, h]) => w * h <= 4096 * 4096))
  assert.deepEqual(encodes.at(-1), [4096, 4096, 'image/jpeg', 0.92])
})

test('scaled dimensions never round past the mobile canvas limit', async () => {
  for (const [width, height] of [[8193, 2050], [10000, 6000], [12345, 7777]]) {
    const { encodes } = stubImagePipeline({ width, height, sizeFor: () => MiB })
    await mod.generatePreviewDataUrl(fakeFile({ size: 6 * MiB }), 'data:image/png;base64,X')
    const [w, h] = encodes[0]
    assert.ok(w * h <= 4096 * 4096, `${width}x${height} -> ${w}x${h}`)
    assert.ok(Math.max(w, h) <= 8192)
  }
})

test('the send request carries an original used as its preview once', () => {
  const original = 'data:image/png;base64,AAAA'
  const resized = 'data:image/webp;base64,QUJD'
  const att = { filename: 'a.png', mime_type: 'image/png', data_url: original }
  assert.deepEqual(mod.toStreamSendAttachment({ ...att, preview_data_url: original }), {
    ...att,
    preview_is_original: true,
  })
  assert.deepEqual(mod.toStreamSendAttachment({ ...att, preview_data_url: resized }), {
    ...att,
    preview_data_url: resized,
  })
  assert.equal(mod.attachmentRequestBytes({ ...att, size_bytes: 3, preview_data_url: original }), 3)
  assert.equal(mod.attachmentRequestBytes({ ...att, size_bytes: 3, preview_data_url: resized }), 6)
  assert.equal(mod.attachmentRequestBytes({ ...att, size_bytes: 3 }), 3)
  assert.equal(mod.dataUrlDecodedBytes('data:image/png;base64,QQ=='), 1)
  assert.equal(mod.dataUrlDecodedBytes('data:image/png;base64,QUI='), 2)
})

test('the size is halved only when no high-quality encoding fits', async () => {
  const { encodes } = stubImagePipeline({
    width: 8192,
    height: 2048,
    sizeFor: (w) => (w > 4096 ? 9 * MiB : 4 * MiB),
  })
  const file = fakeFile({ type: 'image/jpeg', size: 9 * MiB })
  const preview = await mod.generatePreviewDataUrl(file, 'data:image/jpeg;base64,X')
  assert.equal(preview, 'data:image/png;encoded')
  assert.deepEqual(encodes.at(-1), [4096, 1024, 'image/png', undefined])
})
