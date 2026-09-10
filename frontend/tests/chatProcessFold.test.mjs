import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// ---------------------------------------------------------------------------
// The fold helpers are pure functions over a finished turn, so they are tested
// directly. StructuredPane's wiring (memo deps, template branches, the sticky
// header's CSS prerequisite) is asserted against the component source, the way
// structuredPaneApprovalMemo.test.mjs guards the approval card.
// ---------------------------------------------------------------------------

const source = await readFile(
  new URL('../src/utils/agentStreamTimeline.ts', import.meta.url),
  'utf8',
)
const questionSource = await readFile(
  new URL('../src/utils/chatQuestionResponse.ts', import.meta.url),
  'utf8',
)
const durationSource = await readFile(
  new URL('../src/utils/duration.ts', import.meta.url),
  'utf8',
)
const transpileOptions = {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
}
const questionJs = ts.transpileModule(questionSource, transpileOptions).outputText
const durationJs = ts.transpileModule(durationSource, transpileOptions).outputText
const timelineJs = ts.transpileModule(
  source.replace(/^import .* from '@\/utils\/.*$/gm, ''),
  transpileOptions,
).outputText
const bundled = `${durationJs}\n${questionJs}\n${timelineJs}`
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(bundled).toString('base64')}`
)
const {
  countProcessSteps,
  foldTurnParts,
  groupEventsIntoTurns,
  splitTurnProcess,
  turnElapsedMs,
  turnProcessLabel,
} = mod

function makeEvent(seq, type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'claude',
    type,
    payload,
    created_at: '2026-01-01T00:00:00Z',
    redacted: false,
    ...overrides,
  }
}

/** thinking -> one tool call -> delivered text, taking 83s. */
function completedTurn(overrides = {}) {
  return groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'do it' }),
    makeEvent(2, 'thinking_delta', { text: 'working' }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(4, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(5, 'text_delta', { text: 'the answer' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }, {
      created_at: '2026-01-01T00:01:23Z',
      ...overrides,
    }),
  ])[0]
}

// ── splitTurnProcess ────────────────────────────────────────────────────

test('an unfinished turn is never splittable', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'partial' }),
  ])[0]
  assert.equal(splitTurnProcess(turn), null)
})

test('a completed turn splits into working process and delivered answer', () => {
  const split = splitTurnProcess(completedTurn())
  assert.ok(split)
  assert.deepEqual(split.process.map(p => p.kind), ['thinking', 'tool_group'])
  assert.deepEqual(split.delivery.map(p => p.kind), ['text'])
  // The delivery is the LAST text part: narration before the answer is process.
  assert.equal(split.delivery[0].text, 'the answer')
})

test('intermediate narration folds with the process, not into the delivery', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'text_delta', { text: 'let me check' }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'Read', args: {} }),
    makeEvent(4, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(5, 'text_delta', { text: 'the answer' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.deepEqual(split.process.map(p => p.kind), ['text', 'tool_group'])
  assert.deepEqual(split.delivery.map(p => p.text), ['the answer'])
})

test('a turn with no delivered text is not splittable', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.equal(splitTurnProcess(turn), null)
})

test('a process holding an approval card is left whole', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'approval_required', { tool_call_id: 'q1', kind: 'question', title: 'Pick' }),
    makeEvent(3, 'text_delta', { text: 'the answer' }),
    makeEvent(4, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.equal(splitTurnProcess(turn), null, 'folding would hide a card the user must click')
})

test('a process holding an error is left whole', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'error', { message: 'boom' }),
    makeEvent(3, 'text_delta', { text: 'the answer' }),
    makeEvent(4, 'turn_completed', { status: 'failed' }),
  ])[0]
  assert.equal(splitTurnProcess(turn), null, 'folding would hide why the turn failed')
})

test('a turn cancelled mid-work has no delivered answer to fold around', () => {
  // The shape that shipped broken: the last text is working narration and the
  // turn is cut off while work is still going, so "the last text and everything
  // after it" is not an answer. Folding it left the tools and thinking on screen
  // under a header claiming to have hidden them.
  //
  // The text has to sit AFTER a tool for this to test anything: with the text
  // first, the older "nothing to fold" guard (``index <= 0``) returns null on
  // its own and this case passes even with the rule removed.
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(4, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(5, 'text_delta', { text: '停在这里，先确认一下' }),
    makeEvent(6, 'thinking_delta', { text: 'still working' }),
    makeEvent(7, 'turn_completed', { status: 'cancelled' }),
  ])[0]
  assert.deepEqual(
    turn.parts.map(p => p.kind),
    ['thinking', 'tool_group', 'text', 'thinking'],
    'fixture sanity: the last text must not be the first part',
  )
  assert.equal(splitTurnProcess(turn), null)
  assert.equal(foldTurnParts(turn, false), turn.parts)
})

test('a delivered answer followed only by a status still folds', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'the answer' }),
    makeEvent(4, 'status', { text: 'usage: 12k tokens' }),
    makeEvent(5, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.ok(split, 'a status notice is not work, so it does not disqualify the answer')
  assert.deepEqual(split.delivery.map(p => p.kind), ['text', 'status'])
})

// ── step count, elapsed time, label ─────────────────────────────────────

test('steps count actions, not render blocks', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'a' }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'Read', args: {} }),
    makeEvent(4, 'tool_call_started', { tool_call_id: 'c2', name: 'Grep', args: {} }),
    makeEvent(5, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(6, 'tool_call_completed', { tool_call_id: 'c2', status: 'completed' }),
    makeEvent(7, 'text_delta', { text: 'answer' }),
    makeEvent(8, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.equal(countProcessSteps(split.process), 2)
})

test('elapsed time is omitted when it cannot be trusted', () => {
  const turn = completedTurn()
  assert.equal(turnElapsedMs(turn), 83_000)

  // Sub-second spans come from history replayed with one import-time stamp on
  // every event; labelling a long turn "0s" would be worse than saying nothing.
  assert.equal(turnElapsedMs({ ...turn, completedAt: '2026-01-01T00:00:00Z' }), null)
  assert.equal(turnElapsedMs({ ...turn, startedAt: null }), null)
  assert.equal(turnElapsedMs({ ...turn, completedAt: null }), null)
})

test('the folded label reads as process, work done, and time taken', () => {
  const turn = completedTurn()
  const split = splitTurnProcess(turn)
  // 83s floors to "1m": the shared formatter is deliberately coarse so the
  // Chat timeline and the workspace progress timeline read the same way.
  assert.equal(turnProcessLabel(turn, split.process), '过程 · 1 个工具调用 · 1m')

  const noTools = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'answer' }),
    makeEvent(4, 'turn_completed', { status: 'completed' }, {
      created_at: '2026-01-01T00:00:05Z',
    }),
  ])[0]
  assert.equal(turnProcessLabel(noTools, splitTurnProcess(noTools).process), '过程 · 5s')
})

// ── foldTurnParts ───────────────────────────────────────────────────────

test('folding replaces the process with one line ahead of the answer', () => {
  const turn = completedTurn()
  const folded = foldTurnParts(turn, false)
  assert.equal(folded.length, 2)
  assert.equal(folded[0].kind, 'process')
  assert.equal(folded[0].meta, '过程 · 1 个工具调用 · 1m')
  assert.equal(folded[1].kind, 'text')
  assert.equal(folded[1].text, 'the answer')
})

test('expanding grows the detail underneath the header, not the header itself', () => {
  const turn = completedTurn()
  const folded = foldTurnParts(turn, false)
  const expanded = foldTurnParts(turn, true)
  assert.deepEqual(
    expanded.map(p => p.kind),
    ['process', 'thinking', 'tool_group', 'text'],
    'the header must lead in both states: moving it to the bottom would shift '
      + 'the control out from under the pointer and force a scroll to re-fold',
  )
  assert.equal(expanded[0].expanded, true)
  assert.equal(folded[0].expanded, false)
  assert.equal(
    expanded[0].key,
    folded[0].key,
    'the header keeps its identity across states so the DOM node is reused '
      + 'and the pointer stays on it',
  )
})

test('a turn with nothing to fold renders its parts untouched', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'text_delta', { text: 'answer' }),
    makeEvent(3, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.equal(foldTurnParts(turn, false), turn.parts)
  assert.equal(foldTurnParts(turn, true), turn.parts)
})

test('a running turn renders its parts untouched', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'partial' }),
  ])[0]
  assert.equal(foldTurnParts(turn, false), turn.parts)
})

// ── StructuredPane wiring ───────────────────────────────────────────────

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

test('turn v-memo deps include the fold signature', () => {
  const memoMatch = structuredPane.match(/v-memo="\[([^\]]*)\]"/)
  assert.ok(memoMatch, 'a v-memo directive must exist on the turn loop')
  assert.match(
    memoMatch[1],
    /turnFoldSignature\(turn\)/,
    'v-memo deps must include turnFoldSignature(turn) or toggling the process '
      + 'updates the reactive state but v-memo skips re-rendering the turn',
  )
})

test('the part loop renders the folded view, not the raw parts', () => {
  assert.match(structuredPane, /v-for="part in turnPartsFor\(turn\)"/)
})

test('toggleTurnProcess returns an empty signature for unfolded turns', () => {
  const fnMatch = structuredPane.match(
    /function turnFoldSignature\(turn: TimelineTurn\): string \{[\s\S]*?\n\}/,
  )
  assert.ok(fnMatch, 'turnFoldSignature function must exist')
  // Turns with nothing foldable — including the active one — stay memoized.
  assert.match(fnMatch[0], /if \(!isTurnFoldable\(turn\)\) return ''/)
})

test('the newest completed turn is excluded from folding', () => {
  const fnMatch = structuredPane.match(
    /function isTurnFoldable\(turn: TimelineTurn\): boolean \{[\s\S]*?\n\}/,
  )
  assert.ok(fnMatch, 'isTurnFoldable function must exist')
  assert.match(
    fnMatch[0],
    /turn\.key !== latestCompletedTurnKey\.value/,
    'the turn being read must stay open until a newer one finishes',
  )
  assert.match(fnMatch[0], /splitTurnProcess\(turn\) !== null/)
})

test('fold state is per-turn and replaced on toggle so the memo sees it', () => {
  assert.match(structuredPane, /const processExpandedOverrides = ref\(new Map<string, boolean>\(\)\)/)
  const fnMatch = structuredPane.match(
    /function toggleTurnProcess\(turn: TimelineTurn\): void \{[\s\S]*?\n\}/,
  )
  assert.ok(fnMatch, 'toggleTurnProcess function must exist')
  assert.match(fnMatch[0], /new Map\(processExpandedOverrides\.value\)/)
})

test('the process header is the single toggle for its own detail', () => {
  assert.match(structuredPane, /v-else-if="part\.kind === 'process'"/)
  assert.doesNotMatch(
    structuredPane,
    /process_end/,
    'the collapse control must be the header itself, not a footer that scrolls '
      + 'out of reach exactly when the detail is long',
  )
  const toggles = structuredPane.match(/@click="toggleTurnProcess\(turn\)"/g) ?? []
  assert.equal(toggles.length, 1, 'one control, in the place the reader clicked')
  assert.match(structuredPane, /:aria-expanded="part\.expanded"/)
  assert.match(structuredPane, /part\.expanded \? '▾' : '▸'/)
})

test('an open process header stays reachable from any scroll depth', () => {
  const openRule = structuredPane.match(/\.process-fold--open \{[\s\S]*?\n\}/)
  assert.ok(openRule, 'the open-state rule must exist')
  assert.match(
    openRule[0],
    /position: sticky/,
    'a working region can be taller than the viewport, so the toggle must pin '
      + 'to the top of the timeline like the thinking and tool summaries do',
  )
})

test('a long thinking or tool card can be collapsed from its own footer', () => {
  const collapses = structuredPane.match(/class="details-collapse"/g) ?? []
  assert.equal(collapses.length, 2, 'thinking and tool cards each need the footer')
  assert.match(structuredPane, /@click="collapseDetails"/)
  const fnMatch = structuredPane.match(
    /function collapseDetails\(event: MouseEvent\): void \{[\s\S]*?\n\}/,
  )
  assert.ok(fnMatch, 'collapseDetails function must exist')
  assert.match(fnMatch[0], /closest\('details'\)/)
})

test('the sticky summary keeps its scrolling ancestor intact', () => {
  // ``position: sticky`` is ignored inside an ancestor with ``overflow: hidden``
  // because that ancestor becomes the scrollport. The tool card clips its
  // children to the rounded corners, so it must clip without scrolling.
  assert.match(structuredPane, /position: sticky/)
  const toolCard = structuredPane.match(/\.tool-card \{[\s\S]*?\n\}/)
  assert.ok(toolCard)
  assert.doesNotMatch(toolCard[0], /overflow: hidden/)
  assert.match(toolCard[0], /overflow: clip/)
})
