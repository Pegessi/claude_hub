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

test('the action row is in normal flow, not pinned over the message', () => {
  // Absolutely positioned it sat behind the bubble — which is itself
  // positioned — so the buttons could not be clicked at all.
  const rule = structuredPane.match(/\.turn-actions \{[\s\S]*?\n\}/)
  assert.ok(rule, 'the .turn-actions rule must exist')
  assert.doesNotMatch(
    rule[0],
    /position: absolute/,
    'a pinned row lands behind the bubble and swallows clicks',
  )
})

test('the action row reserves room for the buttons and the time', () => {
  const rule = structuredPane.match(/\.turn-actions \{[\s\S]*?\n\}/)
  assert.ok(rule)
  assert.match(rule[0], /display: flex/, 'the row lays its contents out')
  assert.match(rule[0], /gap:/, 'actions and time need a gap so they do not crowd')
  assert.match(rule[0], /min-height:/, 'the row keeps its height so revealing it does not shift the thread')
})

test('the message row carries the edit action and the time', () => {
  const row = structuredPane.match(/class="turn-actions turn-actions--message"[\s\S]*?<\/div>/)
  assert.ok(row, 'the message actions row must exist')
  assert.match(row[0], /class="edit-resend-hover-btn"/, 'the edit action belongs to the message it edits')
  assert.match(row[0], /class="turn-time"/, 'the time sits beside it')
  assert.match(row[0], /messageClockLabel\(turn\)/, 'the time is the message\'s own')
})

test('the turn closes with its own actions row', () => {
  const row = structuredPane.match(/class="turn-actions turn-actions--turn"[\s\S]*?<\/div>/)
  assert.ok(row, 'the turn actions row must exist')
  assert.match(row[0], /class="turn-fork-button"/, 'fork applies to the whole turn')
  assert.match(row[0], /class="turn-time"/, 'the time sits beside it')
  assert.match(row[0], /turnClockLabel\(turn\)/, 'the turn row reports the answer\'s own time')
})

test('inline editing does not balloon the bubble', () => {
  // The form forced ``min-width: 320px`` and drew a bordered dark box inside
  // the blue bubble, so a two-character message opened a wide empty-looking
  // form. The textarea now reads as the message itself.
  const form = structuredPane.match(/\.edit-resend-form \{[\s\S]*?\n\}/)
  assert.ok(form, 'the edit form rule must exist')
  assert.doesNotMatch(form[0], /min-width: min\(320px/, 'the form must not force a wide bubble')

  const textarea = structuredPane.match(/\.edit-resend-textarea \{[\s\S]*?\n\}/)
  assert.ok(textarea, 'the textarea rule must exist')
  assert.match(textarea[0], /border: none/, 'the textarea must not draw a box inside the bubble')
  assert.match(textarea[0], /background: none/, 'the textarea must not draw its own fill')
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
