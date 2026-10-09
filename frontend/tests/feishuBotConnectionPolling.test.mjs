import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { bot, flush, harness, mountSettings, pool, response } from './feishuBotUiTestHarness.mjs'

const dialog = readFileSync(
  new URL('../src/components/FeishuBotSettingsDialog.vue', import.meta.url),
  'utf8',
)

test('connection retries are refreshed at a bounded cadence', () => {
  assert.match(dialog, /CONNECTION_REFRESH_MS\s*=\s*[1-9][0-9_]{3,}/)
  assert.match(dialog, /connection_status\s*===\s*'connecting'/)
  assert.match(dialog, /connection_status\s*===\s*'failed'/)
  assert.match(dialog, /setTimeout\([^,]+,\s*CONNECTION_REFRESH_MS\)/s)
  assert.match(dialog, /await store\.refresh\([^)]*signal[^)]*\)/)
})

test('connection refresh stops after terminal status or dialog cleanup', () => {
  assert.match(dialog, /clearTimeout\(connectionRefreshTimer\)/)
  assert.match(dialog, /connectionRefreshController\?\.abort\(\)/)
  assert.match(dialog, /onUnmounted\([\s\S]*clearLocalState/)
  assert.match(dialog, /connected|stopped/)
})

test('connecting Bot refreshes until the server reports connected', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const h = harness(t)
  const connecting = pool(2, [bot('a', { connection_status: 'connecting' })])
  const connected = pool(2, [bot('a', { connection_status: 'connected' })])
  h.store.applyPool(connecting)
  h.setServer(connecting)
  h.setTransport(async () => response(h.server()))
  const state = await mountSettings(t, h)
  const reads = () => h.calls.filter(call => (call.init.method ?? 'GET') === 'GET').length
  const initialReads = reads()

  t.mock.timers.tick(1_999)
  await flush()
  assert.equal(reads(), initialReads)

  h.setServer(connected)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(reads(), initialReads + 1)
  assert.equal(h.store.botById('a').connection_status, 'connected')

  t.mock.timers.tick(10_000)
  await flush()
  assert.equal(reads(), initialReads + 1)
  state.close()
})

test('closing aborts an in-flight connection refresh', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const h = harness(t)
  const connecting = pool(2, [bot('a', { connection_status: 'connecting' })])
  h.store.applyPool(connecting)
  h.setServer(connecting)
  let aborted = false
  let reads = 0
  h.setTransport(async (_url, init) => {
    reads += 1
    if (reads === 1) return response(connecting)
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => {
        aborted = true
        reject(new DOMException('aborted', 'AbortError'))
      })
    })
  })
  const state = await mountSettings(t, h)

  t.mock.timers.tick(2_000)
  await flush()
  assert.equal(reads, 2)
  state.close()
  await flush()

  assert.equal(aborted, true)
})
