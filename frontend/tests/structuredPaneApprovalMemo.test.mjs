import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

// ---------------------------------------------------------------------------
// AskUserQuestion approval cards render inside a v-memo'd turn. The memo deps
// MUST include a signature of the per-approval selection/resolved state, or a
// click that toggles an option updates the reactive refs but v-memo sees
// unchanged deps and skips re-rendering the turn — the chip shows no selected
// state and the submit button stays disabled. This is the regression guard for
// that exact bug.
// ---------------------------------------------------------------------------

test('turn v-memo deps include the approval interaction signature', () => {
  const memoMatch = structuredPane.match(/v-memo="\[([^\]]*)\]"/)
  assert.ok(memoMatch, 'a v-memo directive must exist on the turn loop')
  const deps = memoMatch[1]
  assert.match(
    deps,
    /turnApprovalSignature\(turn\)/,
    'v-memo deps must include turnApprovalSignature(turn) so a selection toggle re-renders the turn',
  )
})

test('turnApprovalSignature folds in answers, resolved keys, and isSending', () => {
  const fnMatch = structuredPane.match(
    /function turnApprovalSignature\(turn: TimelineTurn\): string \{[\s\S]*?\n\}/,
  )
  assert.ok(fnMatch, 'turnApprovalSignature function must exist')
  const body = fnMatch[0]
  // Non-approval turns short-circuit so they stay cheaply memoized.
  assert.match(body, /turn\.approvals\.length === 0/)
  // Each approval's state is folded in via the pure signature helper.
  assert.match(body, /approvalStateSignature\(/)
  // isSending gates the disabled state inside the card, so it must invalidate
  // the memo for approval-bearing turns too.
  assert.match(body, /isSending\.value/)
})

test('approval card options are wired to the composable selection helpers', () => {
  // The option chip must reflect selected state and toggle on click. These
  // bindings are formatted across multiple lines in the template, so the
  // regexes tolerate whitespace/newlines between arguments.
  assert.match(structuredPane, /isQuestionOptionSelected\(\s*part\.approval\.key/)
  assert.match(
    structuredPane,
    /toggleQuestionOption\(\s*part\.approval\.key,\s*question\.id,\s*option\.id,\s*question\.allowMultiple,?\s*\)/,
  )
  // The submit button must be gated by canSubmitQuestion.
  assert.match(structuredPane, /canSubmitQuestion\(part\.approval\)/)
})
