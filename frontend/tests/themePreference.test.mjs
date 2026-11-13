import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createPinia, disposePinia } from 'pinia'
import ts from 'typescript'

function moduleUrl(path, imports = {}) {
  let { outputText } = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  })
  for (const [specifier, url] of Object.entries(imports)) {
    outputText = outputText.replaceAll(`from '${specifier}'`, `from ${JSON.stringify(url)}`)
  }
  return `data:text/javascript;base64,${Buffer.from(`${outputText}\n//# sourceURL=${path}`).toString('base64')}`
}
const preferenceUrl = moduleUrl('../src/features/app-shell/themePreference.ts')
const { parseThemePreference, resolveColorScheme, THEME_PALETTES, THEME_STORAGE_KEY, LEGACY_THEME_STORAGE_KEY } = await import(preferenceUrl)
const { useAppStore } = await import(moduleUrl('../src/stores/appStore.ts', {
  vue: import.meta.resolve('vue'),
  pinia: import.meta.resolve('pinia'),
  '@/features/app-shell/themePreference': preferenceUrl,
  '@/utils/serviceRestart': moduleUrl('../src/utils/serviceRestart.ts'),
}))

function fixture(t, stored = {}, systemDark = false) {
  const values = new Map(Object.entries(stored))
  const windowListeners = new Map()
  const systemListeners = new Map()
  const dataset = {}
  let chromeColor = null
  let storageWrites = 0
  const localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => {
      storageWrites++
      values.set(key, value)
    },
  }
  const globals = {
    localStorage,
    sessionStorage: { getItem: () => null },
    window: {
      matchMedia: () => ({
        matches: systemDark,
        addEventListener: (name, cb) => systemListeners.set(name, cb),
        removeEventListener: name => systemListeners.delete(name),
      }),
      addEventListener: (name, cb) => windowListeners.set(name, cb),
      removeEventListener: name => windowListeners.delete(name),
    },
    document: {
      documentElement: { dataset },
      querySelector: () => ({ setAttribute: (_name, value) => { chromeColor = value } }),
    },
    getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
  }
  const pinia = createPinia()
  // Dispose listeners before removing the browser globals they belong to.
  t.after(() => disposePinia(pinia))
  for (const [key, value] of Object.entries(globals)) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key)
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true })
    t.after(() => {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    })
  }
  const store = useAppStore(pinia)
  return { store, values, dataset, windowListeners, systemListeners, localStorage, chromeColor: () => chromeColor, storageWrites: () => storageWrites }
}

test('existing users retain their original light/dark palette; invalid records recover', () => {
  assert.deepEqual(parseThemePreference(null), { mode: 'dark', palette: 'classic' })
  for (const legacy of ['light', 'dark']) {
    for (const input of [null, '', '{invalid', 'null', '[]', '42']) {
      assert.deepEqual(parseThemePreference(input, legacy), { mode: legacy, palette: 'classic' })
    }
  }
  assert.deepEqual(parseThemePreference('{"mode":"system","palette":"deleted"}'), { mode: 'system', palette: 'classic' })
  assert.deepEqual(parseThemePreference('{"mode":"unknown","palette":"zenless"}', 'light'), { mode: 'light', palette: 'zenless' })
})

test('retired palette IDs recover to Classic without discarding the selected mode', () => {
  for (const palette of ['slate', 'sand', 'forest', 'dusk', 'studio', 'carbon']) {
    for (const mode of ['system', 'light', 'dark']) {
      assert.deepEqual(parseThemePreference(JSON.stringify({ mode, palette })), { mode, palette: 'classic' })
    }
  }
})

test('all shipped palettes are selectable independently of system/light/dark mode', () => {
  assert.deepEqual(THEME_PALETTES.map(p => p.id).sort(), [
    'classic', 'contrast', 'mono', 'zenless', 'frontier', 'los-santos',
  ].sort())
  for (const { id: palette } of THEME_PALETTES) {
    for (const mode of ['system', 'light', 'dark']) {
      assert.deepEqual(parseThemePreference(JSON.stringify({ mode, palette })), { mode, palette })
      assert.equal(resolveColorScheme(mode, false), mode === 'system' ? 'light' : mode)
      assert.equal(resolveColorScheme(mode, true), mode === 'system' ? 'dark' : mode)
    }
  }
})

test('the real store owns root appearance and migrates only when the user chooses', t => {
  const f = fixture(t, { [LEGACY_THEME_STORAGE_KEY]: 'light' })
  assert.deepEqual(f.dataset, { theme: 'light', palette: 'classic' })
  assert.equal(f.values.has(THEME_STORAGE_KEY), false)
  f.store.setAppearance('light', 'mono')
  assert.deepEqual(JSON.parse(f.values.get(THEME_STORAGE_KEY)), { mode: 'light', palette: 'mono' })
  assert.deepEqual(f.dataset, { theme: 'light', palette: 'mono' })
  assert.equal(f.store.themePaletteName, 'Mono')
  assert.equal(f.chromeColor(), '#123456')
})

test('system changes follow only System mode, without rewriting preference or palette', t => {
  const f = fixture(t)
  f.store.setAppearance('system', 'mono')
  assert.equal(f.store.colorScheme, 'light')
  const record = f.values.get(THEME_STORAGE_KEY)
  f.systemListeners.get('change')({ matches: true })
  assert.equal(f.store.colorScheme, 'dark')
  assert.equal(f.store.themePalette, 'mono')
  assert.equal(f.values.get(THEME_STORAGE_KEY), record)
  f.store.setAppearance('light')
  f.systemListeners.get('change')({ matches: false })
  f.systemListeners.get('change')({ matches: true })
  assert.equal(f.store.colorScheme, 'light')
})

test('cross-tab updates and clear apply without writeback; listeners are disposed', t => {
  const f = fixture(t)
  f.values.set(THEME_STORAGE_KEY, JSON.stringify({ mode: 'system', palette: 'contrast' }))
  const event = { key: THEME_STORAGE_KEY, storageArea: f.localStorage }
  f.windowListeners.get('storage')({ ...event, storageArea: {} })
  assert.equal(f.store.themePalette, 'classic')
  f.windowListeners.get('storage')(event)
  assert.deepEqual(f.dataset, { theme: 'light', palette: 'contrast' })
  f.values.clear()
  f.windowListeners.get('storage')({ ...event, key: null })
  assert.deepEqual(f.dataset, { theme: 'dark', palette: 'classic' })
  f.store.$dispose()
  assert.equal(f.windowListeners.size, 0)
  assert.equal(f.systemListeners.size, 0)
})

test('blocked persistence leaves the chosen palette usable and reports the limitation', t => {
  const f = fixture(t)
  f.localStorage.setItem = () => { throw new Error('quota or permission') }
  f.store.setAppearance('light', 'zenless')
  assert.deepEqual(f.dataset, { theme: 'light', palette: 'zenless' })
  assert.equal(f.store.themePersistenceError, true)
  f.localStorage.setItem = () => {}
  f.store.setAppearance('dark')
  assert.equal(f.store.themePersistenceError, false)
})

for (const palette of ['studio', 'carbon']) {
  for (const mode of ['system', 'light', 'dark']) {
    test(`stored ${palette}/${mode} boots in Classic without rewriting the saved preference`, t => {
      const record = JSON.stringify({ mode, palette })
      const f = fixture(t, { [THEME_STORAGE_KEY]: record }, true)
      assert.equal(f.store.appearanceMode, mode)
      assert.equal(f.store.themePaletteName, 'Classic')
      assert.deepEqual(f.dataset, { theme: mode === 'system' ? 'dark' : mode, palette: 'classic' })
      f.systemListeners.get('change')({ matches: false })
      assert.deepEqual(f.dataset, { theme: mode === 'system' ? 'light' : mode, palette: 'classic' })
      assert.equal(f.values.get(THEME_STORAGE_KEY), record)
      assert.equal(f.storageWrites(), 0)
    })
  }
}

test('retired palette storage events retain their mode without writeback', t => {
  const f = fixture(t)
  f.store.setAppearance('dark', 'zenless')
  const writes = f.storageWrites()
  for (const palette of ['studio', 'carbon']) {
    for (const mode of ['system', 'light', 'dark']) {
      const record = JSON.stringify({ mode, palette })
      f.values.set(THEME_STORAGE_KEY, record)
      f.windowListeners.get('storage')({ key: THEME_STORAGE_KEY, storageArea: f.localStorage })
      assert.equal(f.store.appearanceMode, mode)
      assert.equal(f.store.themePaletteName, 'Classic')
      assert.deepEqual(f.dataset, { theme: mode === 'system' ? 'light' : mode, palette: 'classic' })
      assert.equal(f.values.get(THEME_STORAGE_KEY), record)
      assert.equal(f.storageWrites(), writes)
    }
  }
})
