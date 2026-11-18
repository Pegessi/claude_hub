import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'
import * as vue from 'vue'

const { parse, compileScript } = createRequire(import.meta.url)('vue/compiler-sfc')
const read = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
function moduleUrl(source) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
}
const envUrl = moduleUrl(read('composables/useLaunchEnvPresets.ts').replaceAll("from 'vue'", `from '${import.meta.resolve('vue')}'`))
const { parseLaunchEnv } = await import(envUrl)
const descriptor = parse(read('components/CodexModelSelector.vue')).descriptor
const script = compileScript(descriptor, { id: 'codex-model-selector-test' }).content
const Component = (await import(moduleUrl(script
  .replaceAll(/from ["']vue["']/g, `from '${import.meta.resolve('vue')}'`)
  .replaceAll("from '@/composables/useLaunchEnvPresets'", `from '${envUrl}'`)))).default
Component.render = () => null

const models = [
  { id: 'gpt-6.2-sol', label: 'GPT-6.2-Sol', default_reasoning_effort: 'low', supported_reasoning_efforts: [{ id: 'low' }, { id: 'high' }] },
  { id: 'gpt-6.2-astra', label: 'GPT-6.2-Astra', default_reasoning_effort: 'medium', supported_reasoning_efforts: [{ id: 'medium' }, { id: 'high' }] },
]
const response = (body, status = 200) => new Response(JSON.stringify(body), { status })
const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function mount(t, text = '', fetch = async () => response(models)) {
  const calls = []
  t.mock.method(globalThis, 'fetch', (url, init) => { calls.push({ url, init }); return fetch(url, init) })
  const envText = vue.ref(text)
  const changes = []
  let instance
  const renderer = vue.createRenderer({
    createComment: () => ({}), insert() {}, remove() {}, parentNode: () => null, nextSibling: () => null,
  })
  const app = renderer.createApp({
    setup: () => () => vue.h(Component, {
      envText: envText.value,
      'onUpdate:envText': value => { changes.push(value); envText.value = value },
      ref: value => { if (value) instance = value },
    }),
  })
  app.mount({})
  t.after(() => app.unmount())
  return { state: instance.$.setupState, envText, changes, calls, unmount: () => app.unmount() }
}

test('local creation wires the picker to resolved preset text, not to a separate overriding store', () => {
  assert.match(read('features/terminal/components/SessionCreator.vue'), /<CodexModelSelector\s+v-if="form\.agent_type === 'codex' && form\.target === 'local'"\s+v-model:env-text="form\.env_text"/)
})

test('model and effort choices preserve preset keys and drop only incompatible effort on an explicit switch', async t => {
  const h = mount(t, 'KEEP=value\nHTTP_PROXY=http://proxy.test\nCODEX_MODEL=gpt-6.2-sol\nCODEX_REASONING_EFFORT=low')
  await flush()
  assert.equal(h.calls[0].url, '/api/codex/models')
  assert.deepEqual(h.changes, [], 'discovery does not rewrite preset configuration')
  h.state.selectModel('gpt-6.2-astra')
  await flush()
  assert.deepEqual(parseLaunchEnv(h.envText.value), { KEEP: 'value', HTTP_PROXY: 'http://proxy.test', CODEX_MODEL: 'gpt-6.2-astra' })
  h.state.updateEnv({ CODEX_REASONING_EFFORT: 'high' })
  await flush()
  h.state.selectModel('gpt-6.2-sol')
  await flush()
  assert.equal(parseLaunchEnv(h.envText.value).CODEX_REASONING_EFFORT, 'high')
  h.state.selectModel('')
  await flush()
  assert.deepEqual(parseLaunchEnv(h.envText.value), { KEEP: 'value', HTTP_PROXY: 'http://proxy.test' })
})

test('empty defaults and custom preset model/effort survive catalog loading and preset changes', async t => {
  const h = mount(t)
  await flush()
  assert.equal(h.state.modelId, '')
  assert.equal(h.state.effortId, '')
  assert.deepEqual(h.changes, [])
  h.envText.value = 'CODEX_MODEL=private-model\nCODEX_REASONING_EFFORT=future-effort\nKEEP=yes'
  await flush()
  assert.equal(h.state.modelId, 'private-model')
  assert.equal(h.state.effortId, 'future-effort')
  assert.equal(h.state.selectedModel, undefined)
  assert.deepEqual(h.changes, [])
  h.state.updateEnv({ CODEX_REASONING_EFFORT: '' })
  await flush()
  assert.deepEqual(parseLaunchEnv(h.envText.value), { CODEX_MODEL: 'private-model', KEEP: 'yes' })
})

test('model overrides preserve unrelated raw preset quotes and comments and replace duplicate exports', async t => {
  const unrelated = `# Keep this preset comment\nexport QUOTED='"literal quotes"'\nSPACED='  padded  '`
  const h = mount(t, `${unrelated}\nCODEX_MODEL=old\nexport CODEX_MODEL=private-model`)
  await flush()
  h.state.selectModel('gpt-6.2-sol')
  await flush()
  assert.equal(h.envText.value, `${unrelated}\nCODEX_MODEL=gpt-6.2-sol`)
  assert.deepEqual(parseLaunchEnv(h.envText.value), {
    QUOTED: '"literal quotes"', SPACED: '  padded  ', CODEX_MODEL: 'gpt-6.2-sol',
  })
})

test('catalog error remains retryable without dropping current choices', async t => {
  let failed = true
  const h = mount(t, 'CODEX_MODEL=private-model', async () => response(models, failed ? 503 : 200))
  await flush()
  assert.match(h.state.error, /retry/)
  assert.equal(h.state.modelId, 'private-model')
  failed = false
  await h.state.loadModels()
  assert.equal(h.state.error, '')
  assert.equal(h.state.models.length, 2)
  assert.deepEqual(h.changes, [])
})

test('replaced and unmounted discovery cannot overwrite a new catalog or mutate env', async t => {
  const first = deferred(), second = deferred(), third = deferred()
  const responses = [first, second, third]
  const h = mount(t, 'KEEP=yes', () => responses.shift().promise)
  const current = h.state.loadModels()
  assert.equal(h.calls[0].init.signal.aborted, true)
  second.resolve(response(models))
  await current
  first.resolve(response([{ id: 'stale-model' }]))
  await flush()
  assert.deepEqual(h.state.models.map(model => model.id), models.map(model => model.id))
  const pending = h.state.loadModels()
  h.unmount()
  assert.equal(h.calls[2].init.signal.aborted, true)
  third.resolve(response([{ id: 'unmounted-model' }]))
  await pending
  assert.deepEqual(h.state.models.map(model => model.id), models.map(model => model.id))
  assert.deepEqual(h.changes, [])
})
