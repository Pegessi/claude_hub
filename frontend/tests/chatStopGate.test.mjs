import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

const delivery = read('features/chat/controllers/useChatDelivery.ts')
const composer = read('features/chat/components/ChatComposer.vue')
const pane = read('features/chat/StructuredPane.vue')

test('both Stop surfaces read one gate instead of their own condition', () => {
  // The reconnect/failure banner offered Stop unconditionally while
  // ``cancelActiveTurn`` refused silently, so the button looked armed and every
  // click did nothing. The composer and the banner must agree with the rule the
  // request itself uses.
  const composerStop = composer.match(/class="composer-stop-btn"[\s\S]*?@click="cancelActiveTurn"/)
  assert.ok(composerStop, 'the composer Stop button must exist')
  assert.match(composerStop[0], /:disabled="!canStop"/, 'the composer Stop follows the gate')

  const bannerStops = pane.match(/class="banner-stop"[\s\S]*?@click="cancelActiveTurn"/g) ?? []
  assert.ok(bannerStops.length >= 2, 'both banners offer Stop')
  for (const stop of bannerStops) {
    assert.match(stop, /:disabled="!canStop"/, 'a banner Stop follows the same gate')
  }
})

test('the gate is exactly the state in which a cancel can run', () => {
  // A direct-tab send holds the tab admission lock across provider delivery and
  // ``POST /stream/cancel`` takes that same lock, so a Stop clicked mid-send
  // would only block behind the send. It stays disabled there; the cancel
  // never races an optimistic turn.
  assert.match(
    delivery,
    /const canStop = computed\(\(\) => !isCancelling\.value && stopArmed\.value && !isSending\.value\)/,
  )
  const guard = delivery.match(/async function cancelActiveTurn\(\) \{[\s\S]*?if \(!canStop\.value\) return/)
  assert.ok(guard, 'cancelActiveTurn must guard on the same rule')
  assert.match(guard[0], /if \(!canStop\.value\) return/, 'the request refuses exactly what the button refuses')
})
