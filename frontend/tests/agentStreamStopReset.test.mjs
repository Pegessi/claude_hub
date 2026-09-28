// Manual Stop local-reset contract.
//
// Regression for the reported wedge (tab 896983b0): the backend had already
// persisted ``cancelled`` and released the Hub guard, so a SECOND Stop returned
// ``{cancelled:false}`` ("nothing new to terminalize") while the browser was
// still stuck on the provider "Reconnecting … 1/5" banner and the durable edge
// was trapped behind a wedged long-poll. The old UI gated its local reset on
// ``cancelled !== false``, so that second Stop did nothing — the composer
// stayed locked.
//
// Required behavior under test:
//   - on ANY successful (200) Stop the UI appends one synthetic terminal
//     ``cancelled`` edge for the turn it asked about, regardless of the
//     ``cancelled`` boolean, so the composer unlocks and the round reads
//     "Stopped" immediately;
//   - the synthetic edge uses stream_sequence -1 (never a real sequence) and is
//     idempotent;
//   - the incremental timeline reducer marks the turn completed and the chat
//     lock clears;
//   - when the backend's OWN terminal edge lands (cancelled OR a natural
//     completion that won the race), the optimism is dropped and the reducer
//     still yields exactly one, correctly-statused turn (no double edge);
//   - a composable ``nudge()`` re-issues the in-flight /wait in one RTT without
//     counting as a transport failure.
import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createRenderer, nextTick } from 'vue'
import ts from 'typescript'

const transpileOptions = {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}

async function loadTs(relPath) {
  return import(await moduleUrl(relPath))
}

// Recursive @/-aware transpiling loader (mirrors agentStreamHydration.test.mjs):
// every ``@/`` dependency is itself transpiled to a data URL, and bare
// specifiers (``vue``) resolve against this test's node resolution.
const moduleUrls = new Map()
async function moduleUrl(path) {
  const rel = path.replace(/\.ts$/, '')
  if (moduleUrls.has(rel)) return moduleUrls.get(rel)
  const source = await readFile(new URL(`../src/${rel}.ts`, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, transpileOptions)
  for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
    const url = specifier.startsWith('@/')
      ? await moduleUrl(specifier.slice(2))
      : import.meta.resolve(specifier)
    outputText = outputText
      .replaceAll(`'${specifier}'`, JSON.stringify(url))
      .replaceAll(`"${specifier}"`, JSON.stringify(url))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(rel, url)
  return url
}

// Bundle the timeline reducer with its @/utils deps inlined, mirroring the
// established queue-wedge test harness.
async function loadTimeline() {
  const read = name => readFile(new URL(`../src/utils/${name}`, import.meta.url), 'utf8')
  const [durationSrc, questionSrc, subagentSrc, agentImageSrc, timelineSrc, lifeSrc] =
    await Promise.all([
      read('duration.ts'),
      read('chatQuestionResponse.ts'),
      read('subagentTool.ts'),
      read('agentImage.ts'),
      read('agentStreamTimeline.ts'),
      read('chatTurnLifecycle.ts'),
    ])
  const strip = src => src.replace(/^import .* from '@\/utils\/.*$/gm, '')
  const bundle = [durationSrc, questionSrc, subagentSrc, agentImageSrc, timelineSrc]
    .map(strip)
    .map(src => ts.transpileModule(src, transpileOptions).outputText)
    .join('\n')
  const timeline = await import(
    `data:text/javascript;base64,${Buffer.from(bundle).toString('base64')}`
  )
  const life = await import(
    `data:text/javascript;base64,${Buffer.from(ts.transpileModule(lifeSrc, transpileOptions).outputText).toString('base64')}`
  )
  return { ...timeline, ...life }
}

function makeEvent(seq, type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'traex',
    type,
    payload,
    created_at: `2026-09-27T03:00:${String(seq % 60).padStart(2, '0')}Z`,
    redacted: false,
    ...overrides,
  }
}
const turnStarted = (seq, id = 'turn-1') =>
  makeEvent(seq, 'turn_started', { summary: 'work' }, { turn_id: id })
const textDelta = (seq, id = 'turn-1') =>
  makeEvent(seq, 'text_delta', { text: 'thinking…' }, { turn_id: id })
const turnCompleted = (seq, status, id = 'turn-1') =>
  makeEvent(seq, 'turn_completed', { status }, { turn_id: id })

// ── pure optimistic-edge helpers ───────────────────────────────────────────

const stopReset = await loadTs('utils/agentStreamStopReset.ts')
// The @/types dependency is type-only and erased by the TS transpile, so no
// runtime alias is needed.

test('optimistic edge is a -1 cancelled completion keyed to the stopped turn', () => {
  const evt = stopReset.buildOptimisticCancelledEvent('turn-9', {
    tabId: 't1',
    agentType: 'codex',
    now: '2026-09-27T03:00:00Z',
  })
  assert.equal(evt.stream_sequence, -1)
  assert.equal(evt.turn_id, 'turn-9')
  assert.equal(evt.message_id, 'turn-9')
  assert.equal(evt.type, 'turn_completed')
  assert.equal(evt.payload.status, 'cancelled')
  assert.equal(evt.tab_id, 't1')
  assert.equal(evt.agent_type, 'codex')
})

test('withOptimisticCancelled appends once (idempotent) and inherits agent type', () => {
  const base = [turnStarted(0, 'turn-1'), textDelta(1)]
  const once = stopReset.withOptimisticCancelled(base, 'turn-1', {
    tabId: 't1',
    now: '2026-09-27T03:00:05Z',
  })
  assert.equal(once.length, 3)
  assert.equal(once[2].stream_sequence, -1)
  assert.equal(once[2].agent_type, 'traex') // inherited from base tail
  // Applying again must not stack a second synthetic edge.
  const twice = stopReset.withOptimisticCancelled(once, 'turn-1', {
    tabId: 't1',
    now: '2026-09-27T03:00:06Z',
  })
  assert.equal(twice.length, 3)
  assert.equal(twice, once)
  // The original base array is never mutated.
  assert.equal(base.length, 2)
})

test('hasAuthoritativeTerminalFor matches the real edge in any terminal status', () => {
  assert.equal(stopReset.hasAuthoritativeTerminalFor([turnStarted(0)], 'turn-1'), false)
  assert.equal(
    stopReset.hasAuthoritativeTerminalFor(
      [turnStarted(0), turnCompleted(2, 'cancelled')],
      'turn-1',
    ),
    true,
  )
  assert.equal(
    stopReset.hasAuthoritativeTerminalFor(
      [turnStarted(0, 'a'), turnCompleted(2, 'completed', 'a')],
      'b',
    ),
    false,
  )
})

// ── reducer reconciliation: unlock now, converge when the real edge lands ────

const { groupEventsIntoTurns, IncrementalTimelineReducer, isChatModeLocked } =
  await loadTimeline()

test('optimistic cancelled edge completes the turn and clears the chat lock', () => {
  // The turn is live and locked BEFORE Stop.
  const base = [turnStarted(0), textDelta(1)]
  assert.equal(isChatModeLocked(false, groupEventsIntoTurns(base)), true)

  // Stop returns (even {cancelled:false}) and the UI appends the edge.
  const optimistic = stopReset.withOptimisticCancelled(base, 'turn-1', {
    tabId: 't1',
    now: '2026-09-27T03:00:05Z',
  })
  const turns = groupEventsIntoTurns(optimistic)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].completed, true)
  assert.equal(turns[0].completionStatus, 'cancelled')
  assert.equal(isChatModeLocked(false, turns), false) // composer unlocked
})

test('incremental reducer rebuilds on the -1 edge and converges to one cancelled turn', () => {
  const reducer = new IncrementalTimelineReducer()
  reducer.reduce([turnStarted(0), textDelta(1)])
  // Optimistic Stop.
  const optTurns = reducer.reduce(
    stopReset.withOptimisticCancelled([turnStarted(0), textDelta(1)], 'turn-1', {
      tabId: 't1',
      now: '2026-09-27T03:00:05Z',
    }),
  )
  assert.equal(optTurns[0].completed, true)
  assert.equal(isChatModeLocked(false, optTurns), false)

  // Real edge lands → optimism dropped; reducer sees a plain durable list.
  const real = [turnStarted(0), textDelta(1), turnCompleted(2, 'cancelled')]
  assert.equal(stopReset.hasAuthoritativeTerminalFor(real, 'turn-1'), true)
  const finalTurns = reducer.reduce(real)
  assert.equal(finalTurns.length, 1)
  assert.equal(finalTurns[0].completed, true)
  assert.equal(finalTurns[0].completionStatus, 'cancelled')
  assert.equal(isChatModeLocked(false, finalTurns), false)
})

test('natural completion winning the Stop race converges to status=completed', () => {
  const reducer = new IncrementalTimelineReducer()
  reducer.reduce([turnStarted(0), textDelta(1)])
  reducer.reduce(
    stopReset.withOptimisticCancelled([turnStarted(0), textDelta(1)], 'turn-1', {
      tabId: 't1',
      now: '2026-09-27T03:00:05Z',
    }),
  )
  // The provider actually finished normally at the same time the user pressed
  // Stop. The authoritative edge is status=completed.
  const real = [turnStarted(0), textDelta(1), turnCompleted(2, 'completed')]
  assert.equal(stopReset.hasAuthoritativeTerminalFor(real, 'turn-1'), true)
  const turns = reducer.reduce(real)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].completionStatus, 'completed')
})

// ── component guard: the {cancelled:false} wedge must never gate the reset ──

test('StructuredPane Stop resets on a 200 regardless of the cancelled boolean', async () => {
  const pane = await readFile(
    new URL('../src/components/StructuredPane.vue', import.meta.url),
    'utf8',
  )
  // The old gating predicate that caused the wedge must be gone.
  assert.doesNotMatch(
    pane,
    /cancelled\s*!==?\s*false/,
    'Stop must not gate its local reset on the backend cancelled boolean',
  )
  // A successful response must still arm the optimistic terminal edge and the
  // one-RTT wait nudge.
  assert.match(pane, /optimisticCancelledTurnId\.value\s*=\s*cancelledTurnId/)
  assert.match(pane, /\bnudge\(\)/)
})

test('Stop is mounted in BOTH the reconnecting and FAILED banners (5/5 retry case)', async () => {
  const pane = await readFile(
    new URL('../src/components/StructuredPane.vue', import.meta.url),
    'utf8',
  )
  // The reconnecting/loading banner Stop.
  const reconnectingBanner = pane.match(
    /<template v-if="timelineLoadingMessage">[\s\S]*?<\/template>/,
  )
  assert.ok(reconnectingBanner, 'reconnecting banner template must exist')
  assert.match(reconnectingBanner[0], /cancelActiveTurn/, 'reconnecting banner must offer Stop')
  // The hard-FAILED banner (after the provider exhausts its own 5/5 retries)
  // must offer BOTH Stop and Retry.
  const failedBanner = pane.match(
    /<template v-else-if="connectionState === 'failed'">[\s\S]*?<\/template>/,
  )
  assert.ok(failedBanner, 'failed banner template must exist')
  assert.match(failedBanner[0], /cancelActiveTurn/, 'failed banner must offer Stop')
  assert.match(failedBanner[0], /banner-retry/, 'failed banner must still offer Retry')
  // Both banner Stops share the same unconditional gate as the composer Stop.
  const stopGateCount = (pane.match(/v-if="stopArmed"/g) || []).length
  assert.ok(stopGateCount >= 3, `stopArmed should gate composer + 2 banners, got ${stopGateCount}`)
})

// ── composable nudge: re-issue /wait immediately without failing ────────────

test('nudge() re-issues the in-flight /wait at once and stays live', async () => {
  const { useAgentStream } = await loadTs('composables/useAgentStream')
  let waitCalls = 0
  const waitInFlight = []
  const renderer = createRenderer({
    createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
  })
  let stream
  const app = renderer.createApp({
    setup() {
      stream = useAgentStream()
      return () => null
    },
  })
  app.mount({})

  const fetchImpl = async (url, options) => {
    if (url.endsWith('/capabilities')) {
      return { ok: true, json: async () => ({ structured: true }) }
    }
    if (url.includes('/events?')) {
      return {
        ok: true,
        json: async () => ({ events: [], next_sequence: 0, has_more: false }),
      }
    }
    if (url.endsWith('/wait')) {
      waitCalls += 1
      const seq = waitCalls
      waitInFlight.push(seq)
      // Hang until aborted; the loop treats abort as a nudge and re-issues.
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => {
          waitInFlight.splice(waitInFlight.indexOf(seq), 1)
          reject(new Error('aborted'))
        })
      })
    }
    return { ok: true, json: async () => ({}) }
  }
  const originalFetch = globalThis.fetch
  globalThis.fetch = fetchImpl
  const waitFor = async (predicate, message) => {
    const deadline = Date.now() + 3000
    while (Date.now() < deadline) {
      if (predicate()) return
      await new Promise(resolve => setTimeout(resolve, 5))
    }
    throw new Error(message)
  }
  try {
    void stream.start('nudge-test', 'terminal-tab')
    // Wait until live and the first /wait is hanging.
    await waitFor(() => stream.connectionState.value === 'live', 'did not reach live')
    await waitFor(() => waitCalls === 1, 'first /wait not issued')
    assert.equal(waitCalls, 1)

    stream.nudge()

    // The aborted /wait re-issues in one tick; poll briefly without advancing
    // the failure budget.
    await waitFor(() => waitCalls >= 2, 'nudge did not re-issue /wait')
    await nextTick()
    assert.equal(stream.connectionState.value, 'live')
    assert.equal(stream.errorMessage.value, null)
  } finally {
    globalThis.fetch = originalFetch
    app.unmount()
  }
})
