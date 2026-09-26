import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// edgeSwipe.ts is framework-free; transpile it and import as a data URL
// (same harness pattern as sidebarStore.test.mjs — no DOM needed).
const ts = (await import('typescript')).default
const source = readFileSync(
  new URL('../src/utils/edgeSwipe.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  EDGE_BAND_PX,
  isWithinEdgeBand,
  classifyIntent,
  dragTranslatePx,
  dragBackdropOpacity,
  resolveEdgeSwipe,
  drawerOpenReducer,
} = mod

// ---- edge band ----------------------------------------------------------

test('edge band: touches inside the 20px band (and off-screen x<=0) qualify', () => {
  assert.equal(isWithinEdgeBand(0), true)
  assert.equal(isWithinEdgeBand(EDGE_BAND_PX), true)
  assert.equal(isWithinEdgeBand(10), true)
  assert.equal(isWithinEdgeBand(-3), true)
})

test('edge band: touches beyond the band are ignored', () => {
  assert.equal(isWithinEdgeBand(EDGE_BAND_PX + 1), false)
  assert.equal(isWithinEdgeBand(120), false)
  assert.equal(isWithinEdgeBand(Number.POSITIVE_INFINITY), false)
  assert.equal(isWithinEdgeBand(Number.NaN), false)
})

test('edge band width is configurable within the suggested 16-24px range', () => {
  assert.equal(isWithinEdgeBand(24, 24), true)
  assert.equal(isWithinEdgeBand(25, 24), false)
  assert.equal(isWithinEdgeBand(16, 16), true)
})

// ---- direction classification ------------------------------------------

test('intent is undecided until the slop is crossed in either axis', () => {
  assert.equal(classifyIntent(3, 2), 'undecided')
  assert.equal(classifyIntent(0, 0), 'undecided')
})

test('horizontal movement past the slop captures the gesture', () => {
  assert.equal(classifyIntent(20, 4), 'horizontal')
  assert.equal(classifyIntent(-20, 4), 'horizontal')
})

test('clearly vertical movement is handed back to scroll/selection', () => {
  assert.equal(classifyIntent(4, 20), 'vertical')
  assert.equal(classifyIntent(-4, -20), 'vertical')
})

test('exact diagonals resolve to horizontal so an edge fling is never eaten', () => {
  assert.equal(classifyIntent(12, 12), 'horizontal')
})

// ---- follow-finger geometry --------------------------------------------

test('panel translate: closed drawer eases in from -width and clamps', () => {
  assert.equal(dragTranslatePx(false, 0, 320), -320)
  assert.equal(dragTranslatePx(false, 80, 320), -240)
  assert.equal(dragTranslatePx(false, 400, 320), 0)
  assert.equal(dragTranslatePx(false, -50, 320), -320)
})

test('panel translate: open drawer eases out from 0 and clamps', () => {
  assert.equal(dragTranslatePx(true, 0, 320), 0)
  assert.equal(dragTranslatePx(true, -80, 320), -80)
  assert.equal(dragTranslatePx(true, -400, 320), -320)
  assert.equal(dragTranslatePx(true, 90, 320), 0)
})

test('backdrop opacity tracks the visible fraction of the panel', () => {
  assert.equal(dragBackdropOpacity(-320, 320), 0)
  assert.equal(dragBackdropOpacity(0, 320), 1)
  assert.equal(dragBackdropOpacity(-160, 320), 0.5)
  assert.equal(dragBackdropOpacity(-80, 320), 0.75)
})

// ---- lift resolution ----------------------------------------------------

test('closed drawer opens past the distance threshold', () => {
  // default threshold = 320 / 3 ≈ 107
  assert.equal(
    resolveEdgeSwipe({ startOpen: false, dx: 107, velocityX: 0, drawerWidth: 320 }),
    'open',
  )
})

test('closed drawer springs back on a short, slow drag', () => {
  assert.equal(
    resolveEdgeSwipe({ startOpen: false, dx: 60, velocityX: 0.1, drawerWidth: 320 }),
    'revert',
  )
})

test('a fast rightward fling opens even with little travel', () => {
  assert.equal(
    resolveEdgeSwipe({ startOpen: false, dx: 30, velocityX: 0.8, drawerWidth: 320 }),
    'open',
  )
})

test('open drawer closes past the distance threshold or on a left fling', () => {
  assert.equal(
    resolveEdgeSwipe({ startOpen: true, dx: -110, velocityX: 0, drawerWidth: 320 }),
    'close',
  )
  assert.equal(
    resolveEdgeSwipe({ startOpen: true, dx: -25, velocityX: -0.7, drawerWidth: 320 }),
    'close',
  )
})

test('open drawer reverts when dragged left weakly or pushed right', () => {
  assert.equal(
    resolveEdgeSwipe({ startOpen: true, dx: -40, velocityX: -0.1, drawerWidth: 320 }),
    'revert',
  )
  assert.equal(
    resolveEdgeSwipe({ startOpen: true, dx: 60, velocityX: 0.8, drawerWidth: 320 }),
    'revert',
  )
})

test('fling must agree with travel direction: flick-right never closes', () => {
  assert.equal(
    resolveEdgeSwipe({ startOpen: true, dx: -20, velocityX: 0.9, drawerWidth: 320 }),
    'revert',
  )
})

// ---- open/close reducer -------------------------------------------------

test('reducer: open/close are idempotent', () => {
  const closed = { open: false }
  const opened = { open: true }
  assert.deepEqual(drawerOpenReducer(closed, { type: 'open' }), { open: true })
  assert.equal(drawerOpenReducer(opened, { type: 'open' }), opened)
  assert.deepEqual(drawerOpenReducer(opened, { type: 'close' }), { open: false })
  assert.equal(drawerOpenReducer(closed, { type: 'close' }), closed)
})

test('reducer: toggle flips; selecting a session always dismisses', () => {
  assert.deepEqual(drawerOpenReducer({ open: false }, { type: 'toggle' }), { open: true })
  assert.deepEqual(drawerOpenReducer({ open: true }, { type: 'toggle' }), { open: false })
  assert.deepEqual(drawerOpenReducer({ open: true }, { type: 'select' }), { open: false })
  // select while closed is a no-op that keeps the same state reference.
  const closed = { open: false }
  assert.equal(drawerOpenReducer(closed, { type: 'select' }), closed)
})
