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
