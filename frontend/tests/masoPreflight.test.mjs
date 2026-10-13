import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'
import { effectScope, ref } from 'vue'

const source = (await readFile(new URL('../src/composables/useMasoPreflight.ts', import.meta.url), 'utf8'))
  .replace("from 'vue'", `from '${import.meta.resolve('vue')}'`)
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { useMasoPreflight } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const flush = () => new Promise(resolve => setImmediate(resolve))

function fixture(t) {
  const original = globalThis.fetch
  const requests = []
  globalThis.fetch = (_url, options) => new Promise(resolve => {
    requests.push({ options, resolve })
  })
  const scope = effectScope()
  const active = ref(false)
  const base = ref(undefined)
  const probe = scope.run(() => useMasoPreflight(() => active.value, () => base.value))
  t.after(() => { scope.stop(); globalThis.fetch = original })
  const answer = (index, available, extra = {}) => requests[index].resolve(new Response(JSON.stringify({
    available, base_url: 'http://maso.test', message: available ? 'Ready' : 'Unavailable', ...extra,
  })))
  return { active, base, probe, requests, answer }
}

test('selection checks readiness; retry recovers unavailable service', async t => {
  const { active, probe, requests, answer } = fixture(t)
  assert.equal(requests.length, 0)
  active.value = true
  assert.equal(probe.state.value, 'checking')
  answer(0, false)
  await flush()
  assert.equal(probe.state.value, 'unavailable')
  const retry = probe.check()
  assert.equal(probe.state.value, 'checking')
  answer(1, true)
  await retry
  assert.equal(probe.state.value, 'available')
})

test('changing service immediately retires readiness and ignores stale successful response', async t => {
  const { active, base, probe, requests, answer } = fixture(t)
  active.value = true
  base.value = 'http://other.test'
  assert.equal(probe.state.value, 'checking')
  assert.equal(requests[0].options.signal.aborted, true)
  answer(0, true)
  await flush()
  assert.equal(probe.state.value, 'checking')
  assert.equal(JSON.parse(requests[1].options.body).base_url, 'http://other.test')
  answer(1, false)
  await flush()
  assert.equal(probe.state.value, 'unavailable')
})

test('closing selection ignores pending result and reopening performs a fresh check', async t => {
  const { active, probe, requests, answer } = fixture(t)
  active.value = true
  active.value = false
  answer(0, true)
  await flush()
  assert.equal(probe.state.value, 'idle')
  assert.equal(probe.message.value, '')
  active.value = true
  assert.equal(requests.length, 2)
  answer(1, true)
  await flush()
  assert.equal(probe.state.value, 'available')
  active.value = false
  assert.equal(probe.state.value, 'idle')
  assert.equal(probe.endpoint.value, '')
})

test('malformed check response cannot enable MASO', async t => {
  const { active, probe, answer } = fixture(t)
  active.value = true
  answer(0, 'yes')
  await flush()
  assert.equal(probe.state.value, 'unavailable')
  assert.match(probe.message.value, /Retry/)
})
