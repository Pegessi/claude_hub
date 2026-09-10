import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

// ---------------------------------------------------------------------------
// Edit-resend guard: while a turn is running (turnInFlight), the hover edit
// button must be disabled with an explanatory tooltip, and startEdit must
// refuse to open the editor. The backend guard (409) is the authoritative
// fallback; this keeps the UX in sync and avoids a pointless round-trip.
// ---------------------------------------------------------------------------

test('edit button is disabled while a turn is running', () => {
  // The hover edit button must bind :disabled to turnInFlight so it cannot be
  // clicked while a turn is streaming.
  const btnMatch = structuredPane.match(
    /class="edit-resend-hover-btn"[\s\S]*?@click="startEdit\(turn\)"/,
  )
  assert.ok(btnMatch, 'edit-resend hover button must exist')
  const btn = btnMatch[0]

  assert.match(btn, /:disabled="turnInFlight"/, 'edit button must be disabled while turnInFlight')
})

test('edit button explains why it is disabled via a tooltip', () => {
  // A :title bound on turnInFlight gives the user a reason for the disabled
  // state (e.g. "A turn is currently running").
  const btnMatch = structuredPane.match(
    /class="edit-resend-hover-btn"[\s\S]*?@click="startEdit\(turn\)"/,
  )
  assert.ok(btnMatch, 'edit-resend hover button must exist')
  const btn = btnMatch[0]

  assert.match(btn, /:title="turnInFlight \?/, 'edit button must show a tooltip when disabled')
  assert.match(btn, /turn is currently running/, 'tooltip must mention a running turn')
})

// ---------------------------------------------------------------------------
// Placement and disabled affordance. The guards above keep the button from
// doing the wrong thing; these keep it visible in the right place and honest
// about when it is inert.
// ---------------------------------------------------------------------------

test('the edit action lives in the turn hover cluster, not on the bubble', () => {
  // Pinned to the bubble it covered the tail of the message, and — anchored
  // separately from the fork button — the two ended up on top of each other.
  const cluster = structuredPane.match(/<div class="turn-actions">[\s\S]*?<\/div>/)
  assert.ok(cluster, 'the turn-actions cluster must exist')
  assert.match(
    cluster[0],
    /class="edit-resend-hover-btn"/,
    'the edit button must sit in the cluster beside the fork button',
  )
})

test('the edit button is not absolutely positioned over the message', () => {
  const rule = structuredPane.match(/\.edit-resend-hover-btn \{[\s\S]*?\n\}/)
  assert.ok(rule, 'the base rule must exist')
  assert.doesNotMatch(
    rule[0],
    /position: absolute/,
    'an absolutely positioned edit button overlapped the bubble text',
  )
})

test('the cluster lays its buttons out instead of stacking them', () => {
  const cluster = structuredPane.match(/\.turn-actions \{[\s\S]*?\n\}/)
  assert.ok(cluster, 'the .turn-actions rule must exist')
  assert.match(cluster[0], /display: flex/, 'the cluster must lay its buttons in a row')
  assert.match(cluster[0], /gap:/, 'the buttons need a gap so they do not touch')
})

test('a disabled edit button looks disabled', () => {
  // The button is disabled whenever any turn is running — most of the time in
  // an active chat. Wired but unstyled it looked enabled and swallowed clicks
  // with no feedback at all.
  const rule = structuredPane.match(/\.edit-resend-hover-btn:disabled \{[\s\S]*?\n\}/)
  assert.ok(rule, 'a :disabled rule must exist for the edit button')
  assert.match(rule[0], /opacity:/, 'disabled must be visibly dimmed')
  assert.match(rule[0], /cursor: not-allowed/, 'disabled must not claim to be clickable')
})

test('startEdit refuses to open the editor while a turn is running', () => {
  // Even if the disabled button is bypassed (e.g. via a11y tooling), startEdit
  // must bail when turnInFlight is true.
  const fnMatch = structuredPane.match(/function startEdit\(turn: TimelineTurn\) \{[\s\S]*?\n\}/)
  assert.ok(fnMatch, 'startEdit function must exist')
  const body = fnMatch[0]

  assert.match(
    body,
    /if \(turnInFlight\.value\) return/,
    'startEdit must return early when a turn is in flight',
  )
})
