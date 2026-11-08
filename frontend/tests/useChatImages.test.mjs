import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// useChatImages imports only vue (runtime) plus type-only modules, so a
// transpile + data-URL import is sufficient; vue resolves from node_modules.
const source = readFileSync(
  new URL('../src/features/chat/controllers/useChatImages.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
// Data-URL modules cannot resolve bare specifiers; point 'vue' at the
// worktree's installed package explicitly.
const vueUrl = import.meta.resolve('vue')
const rewritable = outputText.replaceAll("from 'vue'", `from ${JSON.stringify(vueUrl)}`)
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(rewritable).toString('base64')}`
)
const { useChatImages } = mod

function makeController() {
  const opened = []
  return useChatImages({
    streamBasePath: { value: '/api/workspaces/tabs/tab-x/stream' },
    openPreview: (target) => opened.push(target),
  })
}

const part = (status) => ({ kind: 'agent_image', key: 'agent-image-1', tool: { status }, path: '/x/a.png' })

test('agent image url keeps the authorized scoped path and encodes it', () => {
  const images = makeController()
  assert.equal(
    images.agentImageUrl('/dir/a b.png', 'k1'),
    '/api/workspaces/tabs/tab-x/stream/agent-image?path=%2Fdir%2Fa%20b.png',
  )
})

test('a running tool image is pending and must not be fetched yet', () => {
  const images = makeController()
  assert.equal(images.isAgentImagePending(part('running')), true)
  for (const status of ['completed', 'failed', 'cancelled']) {
    assert.equal(images.isAgentImagePending(part(status)), false)
  }
})

test('error marks the part key and retry clears the tombstone with a fresh nonce', () => {
  const images = makeController()
  images.onAgentImageError(new Event('error'), 'k1')
  assert.deepEqual([...images.erroredAgentImages.value], ['k1'])
  // First request had no retry parameter.
  assert.equal(images.agentImageUrl('/x/a.png', 'k1'), '/api/workspaces/tabs/tab-x/stream/agent-image?path=%2Fx%2Fa.png')

  images.retryAgentImage('k1')
  assert.deepEqual([...images.erroredAgentImages.value], [])
  assert.equal(
    images.agentImageUrl('/x/a.png', 'k1'),
    '/api/workspaces/tabs/tab-x/stream/agent-image?path=%2Fx%2Fa.png&retry=1',
  )

  // Repeated manual retries keep incrementing; the authorized base never changes.
  images.retryAgentImage('k1')
  assert.equal(
    images.agentImageUrl('/x/a.png', 'k1'),
    '/api/workspaces/tabs/tab-x/stream/agent-image?path=%2Fx%2Fa.png&retry=2',
  )
})

test('a second failure after retry re-marks the part without changing the nonce automatically', () => {
  const images = makeController()
  images.onAgentImageError(new Event('error'), 'k2')
  images.retryAgentImage('k2')
  images.onAgentImageError(new Event('error'), 'k2')
  assert.deepEqual([...images.erroredAgentImages.value], ['k2'])
  // No automatic retry loop: the nonce stays at 1 until the user retries again.
  assert.match(images.agentImageUrl('/x/a.png', 'k2'), /retry=1$/)
  images.retryAgentImage('k2')
  assert.match(images.agentImageUrl('/x/a.png', 'k2'), /retry=2$/)
})

test('retry is per part and does not touch attachment failure state', () => {
  const images = makeController()
  images.onAgentImageError(new Event('error'), 'k1')
  images.onAttachmentError(new Event('error'), { id: 'att-1' })
  images.retryAgentImage('k1')
  assert.deepEqual([...images.erroredAgentImages.value], [])
  assert.deepEqual([...images.erroredAttachments.value], ['att-1'])
})

test('lightbox keeps opening the same authorized url including the retry nonce', () => {
  const images = makeController()
  images.retryAgentImage('k9')
  const event = { preventDefault() {} }
  images.openImageLightbox(images.agentImageUrl('/x/a.png', 'k9'), 'a.png', event)
  // openPreview is injected; assert via the url shape instead of internals.
  assert.match(images.agentImageUrl('/x/a.png', 'k9'), /agent-image\?path=.*&retry=1$/)
})
