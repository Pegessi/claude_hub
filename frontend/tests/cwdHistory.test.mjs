import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// Each load gets a fresh module instance so the module-wide revision,
// storageBroken flag and dismissal map never leak between tests.
const vueUrl = import.meta.resolve('vue')
const { ref } = await import(vueUrl)
let moduleNonce = 0
async function loadCwdHistory() {
  const source = await readFile(
    new URL('../src/composables/useCwdHistory.ts', import.meta.url),
    'utf8',
  )
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  const rewritten = outputText
    .replaceAll(`'vue'`, JSON.stringify(vueUrl))
    .replaceAll(`"vue"`, JSON.stringify(vueUrl))
  // A unique comment makes each load a distinct module record so the
  // module-wide revision and dismissal state never leaks between tests.
  const unique = `${rewritten}\n//module-instance-${moduleNonce++}\n`
  return import(`data:text/javascript;base64,${Buffer.from(unique).toString('base64')}`)
}

const STORAGE_KEY = 'claude-hub:cwd-history:local'

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    map,
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => void map.set(key, String(value)),
    removeItem: key => void map.delete(key),
    clear: () => map.clear(),
  }
}

async function useHistory(storage = fakeStorage()) {
  globalThis.localStorage = storage
  const { useCwdHistory } = await loadCwdHistory()
  const scope = ref('local')
  return { storage, scope, ...useCwdHistory(scope) }
}

test.afterEach(() => {
  delete globalThis.localStorage
})

test('addCwd prepends, dedupes, caps at 8 and persists JSON per scope', async () => {
  const { storage, recentCwds, addCwd } = await useHistory()
  for (const path of ['/a', '/b', '/c', '/d', '/e', '/f', '/g', '/h', '/i']) addCwd(path)
  assert.deepEqual(recentCwds.value, ['/i', '/h', '/g', '/f', '/e', '/d', '/c', '/b'])

  addCwd('/d')
  assert.deepEqual(recentCwds.value, ['/d', '/i', '/h', '/g', '/f', '/e', '/c', '/b'])
  assert.deepEqual(JSON.parse(storage.getItem(STORAGE_KEY)), recentCwds.value)

  addCwd('   ')
  addCwd('~')
  assert.deepEqual(recentCwds.value.length, 8)
})

test('removeCwd persists removal, dismisses the path and deletes the empty key', async () => {
  const history = await useHistory(
    fakeStorage({ [STORAGE_KEY]: JSON.stringify(['/drop', '/keep']) }),
  )
  assert.equal(history.mostRecentCwd.value, '/drop')

  history.removeCwd('/drop')
  assert.deepEqual(history.recentCwds.value, ['/keep'])
  assert.deepEqual(history.dismissedCwds.value, ['/drop'])
  assert.equal(history.mostRecentCwd.value, '/keep')
  assert.deepEqual(JSON.parse(history.storage.getItem(STORAGE_KEY)), ['/keep'])

  history.removeCwd('/keep')
  assert.deepEqual(history.recentCwds.value, [])
  assert.deepEqual([...history.dismissedCwds.value].sort(), ['/drop', '/keep'])
  assert.equal(history.storage.getItem(STORAGE_KEY), null)

  // Removing a path absent from history (e.g. one contributed by an open tab)
  // is a dismissal-only operation and must not throw.
  history.removeCwd('/open-tab-only')
  assert.ok(history.dismissedCwds.value.includes('/open-tab-only'))
  assert.equal(history.storage.getItem(STORAGE_KEY), null)
})

test('removeCwd ignores blank input', async () => {
  const history = await useHistory(fakeStorage({ [STORAGE_KEY]: JSON.stringify(['/one']) }))
  history.removeCwd('   ')
  history.removeCwd(undefined)
  assert.deepEqual(history.recentCwds.value, ['/one'])
  assert.deepEqual(history.dismissedCwds.value, [])
})

test('addCwd after removal clears the dismissal and restores the path first', async () => {
  const storage = fakeStorage({ [STORAGE_KEY]: JSON.stringify(['/one', '/two']) })
  const { recentCwds, dismissedCwds, addCwd, removeCwd } = await useHistory(storage)
  removeCwd('/one')
  assert.deepEqual(recentCwds.value, ['/two'])
  // A caller merging in open tabs hides this path via dismissedCwds.
  assert.deepEqual(dismissedCwds.value, ['/one'])

  addCwd('/one')
  assert.deepEqual(recentCwds.value, ['/one', '/two'])
  assert.deepEqual(dismissedCwds.value, [])
  assert.deepEqual(JSON.parse(storage.getItem(STORAGE_KEY)), ['/one', '/two'])
})

test('history and dismissal stay isolated per scope', async () => {
  const { scope, recentCwds, dismissedCwds, addCwd, removeCwd } = await useHistory()
  addCwd('/local-a')
  removeCwd('/local-a')

  scope.value = 'remote:box-1'
  assert.deepEqual(recentCwds.value, [])
  addCwd('/remote-a')
  assert.deepEqual(recentCwds.value, ['/remote-a'])
  assert.deepEqual(dismissedCwds.value, [])

  scope.value = 'local'
  assert.deepEqual(recentCwds.value, [])
  assert.deepEqual(dismissedCwds.value, ['/local-a'])

  scope.value = 'remote:box-2'
  assert.deepEqual(recentCwds.value, [])
  assert.deepEqual(dismissedCwds.value, [])
})

test('two composable instances share revision-driven updates', async () => {
  const { useCwdHistory } = await loadCwdHistory()
  const first = useCwdHistory(ref('local'))
  const second = useCwdHistory(ref('local'))
  first.addCwd('/shared')
  assert.deepEqual(second.recentCwds.value, ['/shared'])
  first.removeCwd('/shared')
  assert.deepEqual(second.recentCwds.value, [])
  assert.deepEqual(second.dismissedCwds.value, ['/shared'])
})

test('storage fallback keeps add/remove/dismissal consistent for the session', async () => {
  class GetFailsStorage {
    constructor() { this.map = new Map() }
    getItem() { throw new Error('disabled') }
    setItem(key, value) { this.map.set(key, value) }
    removeItem(key) { this.map.delete(key) }
  }
  globalThis.localStorage = new GetFailsStorage()
  const { useCwdHistory } = await loadCwdHistory()
  const { recentCwds, addCwd, removeCwd } = useCwdHistory(ref('local'))
  addCwd('/ephemeral')
  assert.deepEqual(recentCwds.value, ['/ephemeral'])
  removeCwd('/ephemeral')
  assert.deepEqual(recentCwds.value, [])
  addCwd('/ephemeral')
  assert.deepEqual(recentCwds.value, ['/ephemeral'])
})
