import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createRenderer, isProxy, watch } from 'vue'
import ts from 'typescript'

const moduleUrls = new Map()
async function moduleUrl(path) {
  if (moduleUrls.has(path)) return moduleUrls.get(path)
  const source = await readFile(new URL(`../src/${path}.ts`, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
    const url = specifier.startsWith('@/')
      ? await moduleUrl(specifier.slice(2))
      : import.meta.resolve(specifier)
    outputText = outputText.replaceAll(`'${specifier}'`, JSON.stringify(url))
      .replaceAll(`"${specifier}"`, JSON.stringify(url))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(path, url)
  return url
}
const { useAgentStream } = await import(await moduleUrl('composables/useAgentStream'))
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
})
function mountStream() {
  let stream
  const app = renderer.createApp({ setup() { stream = useAgentStream(); return () => null } })
  app.mount({})
  return { stream, unmount: () => app.unmount() }
}

test('the final delta-only hydration page is committed before live, without deep proxies', async t => {
  const event = { stream_sequence: 0, type: 'text_delta', payload: { text: 'Latest message' } }
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push(url)
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) return {
      ok: true, json: async () => ({ events: [event], next_sequence: 0, has_more: false }),
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  let visibleAtLive
  const unwatch = watch(stream.connectionState, state => {
    if (state === 'live') visibleAtLive = stream.events.value.slice()
  }, { flush: 'sync' })
  t.after(unwatch)
  await stream.start('hydration-flush-test', 'terminal-tab')
  assert.deepEqual(visibleAtLive, [event])
  assert.equal(stream.events.value[0], event)
  assert.equal(isProxy(stream.events.value), false)
  assert.equal(isProxy(stream.events.value[0].payload), false)
  assert.ok(requests.some(url => url.endsWith('/wait')))
})
