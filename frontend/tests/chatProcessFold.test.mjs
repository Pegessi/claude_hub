import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { loadChatUtility, loadChatFeature } from './chatPresentationTestHarness.mjs'

const { isTurnFoldable, turnProcessFoldSignature } = await loadChatFeature('presentation/chatTurnPresentation')

// ---------------------------------------------------------------------------
// The fold helpers are pure functions over a finished turn, so they are tested
// directly. StructuredPane's wiring (memo deps, template branches, the sticky
// header's CSS prerequisite) is asserted against the component source, the way
// structuredPaneApprovalMemo.test.mjs guards the approval card.
// ---------------------------------------------------------------------------

const mod = await loadChatUtility('agentStreamTimeline')
const {
  countProcessSteps,
  foldTurnParts,
  getCompletedPlanText,
  groupEventsIntoTurns,
  splitTurnProcess,
  subagentRegionErrorCount,
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
  assert.deepEqual(split.before.map(p => p.kind), ['thinking', 'tool_group'])
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
  assert.deepEqual(split.before.map(p => p.kind), ['text', 'tool_group'])
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

test('an approval card stays visible while tool/thinking process folds', () => {
  // Pending approvals are action-critical history and must not be hidden by the
  // process fold. Tool and thinking detail still fold around the delivered text.
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'approval_required', { tool_call_id: 'q1', kind: 'question', title: 'Pick' }),
    makeEvent(4, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(5, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(6, 'text_delta', { text: 'the answer' }),
    makeEvent(7, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.ok(split, 'a card must not keep the whole working region on screen')
  assert.deepEqual(split.pinned, [], 'approval cards fold with the process')
  assert.deepEqual(split.before.map(p => p.kind), ['thinking', 'approval', 'tool_group'])

  assert.deepEqual(
    foldTurnParts(turn, false).map(p => p.kind),
    ['process', 'approval', 'text'],
    'folded, the approval and answer stay visible',
  )
  assert.deepEqual(
    foldTurnParts(turn, true).map(p => p.kind),
    ['process', 'thinking', 'approval', 'tool_group', 'text'],
    'expanded, the card is still in its place in the record',
  )
})

test('an answered approval card remains visible while process folds', () => {
  // Cards are action-critical evidence; resolved or not, they stay visible in
  // folded history while adjacent thinking/tool detail folds away.
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'approval_required', { tool_call_id: 'q1', kind: 'question', title: 'Pick' }),
    makeEvent(4, 'approval_resolved', { tool_call_id: 'q1' }),
    makeEvent(5, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(6, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(7, 'text_delta', { text: 'the answer' }),
    makeEvent(8, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.ok(split)
  assert.deepEqual(split.pinned, [], 'a card folds with the record, answered or not')
  assert.deepEqual(
    foldTurnParts(turn, false).map(p => p.kind),
    ['process', 'approval', 'text'],
    'approval stays visible while surrounding process folds',
  )
  assert.deepEqual(
    foldTurnParts(turn, true).map(p => p.kind),
    ['process', 'thinking', 'approval', 'tool_group', 'text'],
    'expanded, it is still in its place in the record',
  )
})

test('an error stays visible instead of blocking the fold', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(3, 'error', { message: 'boom' }),
    makeEvent(4, 'text_delta', { text: 'the answer' }),
    makeEvent(5, 'turn_completed', { status: 'failed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.ok(split, 'an error must not keep the whole working region on screen')
  assert.deepEqual(split.pinned.map(p => p.kind), ['error'])
  assert.deepEqual(foldTurnParts(turn, false).map(p => p.kind), ['process', 'error', 'text'])
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

// ── Codex: the plan stream is work, not the answer ──────────────────────
//
// Codex streams its plan through the same ``text_delta`` the answer uses, so
// the two are told apart by the ``plan`` flag the adapter sets. Without it the
// last prose part wins the delivery, and an answer followed by a plan update
// would disappear into the folded process.

test('a Codex plan segment is never mistaken for the delivered answer', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'text_delta', { text: '1. 读代码\n2. 改测试', plan: true }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'shell', args: {} }),
    makeEvent(4, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(5, 'text_delta', { text: 'the answer' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  assert.ok(split)
  assert.deepEqual(split.before.map(p => p.kind), ['text', 'tool_group'])
  assert.equal(split.before[0].fromPlan, true)
  assert.equal(split.delivery[0].text, 'the answer')
})

test('a plan arriving after the answer does not steal the delivery', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'the answer' }),
    makeEvent(4, 'text_delta', { text: 'next: 清理', plan: true }),
    makeEvent(5, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.equal(
    splitTurnProcess(turn),
    null,
    'work follows the answer, so folding would hide the answer and leave the plan',
  )
})

test('a turn of work plus plan text has no answer to deliver', () => {
  // The tool comes first so "nothing to fold" cannot be what rejects this:
  // without the plan flag the plan would be taken as the delivery and the work
  // would fold away around it.
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'tool_call_started', { tool_call_id: 'c1', name: 'shell', args: {} }),
    makeEvent(3, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }),
    makeEvent(4, 'text_delta', { text: '1. 先跑测试', plan: true }),
    makeEvent(5, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.deepEqual(turn.parts.map(p => p.kind), ['tool_group', 'text'])
  assert.equal(splitTurnProcess(turn), null)
  assert.equal(foldTurnParts(turn, false), turn.parts)
})

test('a completed Plan turn exposes its proposal for explicit implementation', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'Plan it', mode: 'plan' }),
    makeEvent(2, 'text_delta', { text: 'Checking files', plan: true, plan_kind: 'progress', snapshot: true }, { message_id: 'plan-progress' }),
    makeEvent(3, 'text_delta', { text: '1. Update adapter\n2. Verify UI', plan: true, plan_kind: 'proposal', snapshot: true }, { message_id: 'plan-final' }),
    makeEvent(4, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.equal(getCompletedPlanText(turn), '1. Update adapter\n2. Verify UI')
})

test('ordinary and failed turns never expose an implementable plan', () => {
  assert.equal(getCompletedPlanText(completedTurn()), null)
  const failedPlan = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'Plan it', mode: 'plan' }),
    makeEvent(2, 'text_delta', { text: 'A plan', plan: true, plan_kind: 'proposal' }),
    makeEvent(3, 'turn_completed', { status: 'failed' }),
  ])[0]
  assert.equal(getCompletedPlanText(failedPlan), null)
})

test('plan prose and answer prose never merge into one part', () => {
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'text_delta', { text: '计划：', plan: true }),
    makeEvent(3, 'text_delta', { text: '先读代码', plan: true }),
    makeEvent(4, 'text_delta', { text: '答案是 42' }),
    makeEvent(5, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.deepEqual(
    turn.parts.map(p => p.text),
    ['计划：先读代码', '答案是 42'],
    'adjacent plan deltas coalesce with each other, but not across the boundary',
  )
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
  assert.equal(countProcessSteps(split.before), 2)
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
  assert.equal(turnProcessLabel(turn, split.before), '过程 · 1 个工具调用 · 1m')

  const noTools = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'answer' }),
    makeEvent(4, 'turn_completed', { status: 'completed' }, {
      created_at: '2026-01-01T00:00:05Z',
    }),
  ])[0]
  assert.equal(turnProcessLabel(noTools, splitTurnProcess(noTools).before), '过程 · 5s')
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

// ── the fold transition: a finished turn folds when a newer turn starts ──

// Two finished turns in one session. The component's ``latestTurnKey`` is the
// last turn's key, so the first turn is history and folds even though it is
// completed. This is the shape that read as "fold stopped working": the first
// turn stayed expanded for the whole time the second turn was in flight,
// because the old rule kept the newest *completed* turn open.
function twoCompletedTurns() {
  return groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'first' }, { turn_id: 'turn-a' }),
    makeEvent(2, 'thinking_delta', { text: 'working' }, { turn_id: 'turn-a' }),
    makeEvent(3, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }, { turn_id: 'turn-a' }),
    makeEvent(4, 'tool_call_completed', { tool_call_id: 'c1', status: 'completed' }, { turn_id: 'turn-a' }),
    makeEvent(5, 'text_delta', { text: 'first answer' }, { turn_id: 'turn-a' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }, { turn_id: 'turn-a' }),
    makeEvent(7, 'turn_started', { summary: 'second' }, { turn_id: 'turn-b' }),
    makeEvent(8, 'thinking_delta', { text: 'working again' }, { turn_id: 'turn-b' }),
    makeEvent(9, 'text_delta', { text: 'second answer' }, { turn_id: 'turn-b' }),
    makeEvent(10, 'turn_completed', { status: 'completed' }, { turn_id: 'turn-b' }),
  ])
}

test('a finished turn folds as soon as a newer turn starts', () => {
  const turns = twoCompletedTurns()
  assert.equal(turns.length, 2)
  // The component's latestTurnKey is the last turn's key.
  const latestKey = turns[turns.length - 1].key
  const isFoldable = (turn) => turn.key !== latestKey && splitTurnProcess(turn) !== null
  assert.equal(isFoldable(turns[0]), true, 'the first turn is history once a newer turn exists')
  assert.equal(isFoldable(turns[1]), false, 'the newest turn stays open')
  // The first turn folds to its header + answer.
  assert.deepEqual(
    foldTurnParts(turns[0], false).map(p => p.kind),
    ['process', 'text'],
  )
})

// ── Feature wiring ─────────────────────────────────────────────────────

const readChat = path => readFileSync(new URL(`../src/features/chat/${path}`, import.meta.url), 'utf8')
const timelineComponent = readChat('components/ChatTimeline.vue')
const turnComponent = readChat('components/ChatTurn.vue')
const turnPartsComponent = readChat('components/ChatTurnParts.vue')
const thinkingComponent = readChat('components/ChatThinkingCard.vue')
const toolComponent = readChat('components/ChatToolGroup.vue')
const subagentComponent = readChat('components/ChatSubagentCard.vue')
const timelineController = readChat('controllers/useChatTimeline.ts')

test('turn v-memo deps include the fold signature', () => {
  const memoMatch = timelineComponent.match(/v-memo="\[([^\]]*)\]"/)
  assert.ok(memoMatch)
  assert.match(memoMatch[1], /turnFoldSignature\(turn\)/)
})

test('the part loop renders the folded view, not raw parts', () => {
  assert.match(turnComponent, /<ChatTurnParts[\s\S]*?:turn="turn"/)
  assert.match(turnComponent, /v-for="part in turnPartsFor\(turn\)"/)
})

test('fold signature is present for foldable turns and empty for unsafe turns', () => {
  const turn = completedTurn()
  const overrides = new Map([[turn.key, false]])
  assert.match(turnProcessFoldSignature(turn, turn.key, overrides), /^folded\|/)
  assert.equal(turnProcessFoldSignature({ ...turn, completed: false }, 'newest', overrides), '')
  assert.equal(turnProcessFoldSignature({ ...turn, parts: [] }, 'newest', overrides), '')
})

test('completed turns with process are foldable even when newest', () => {
  const turn = completedTurn()
  assert.ok(splitTurnProcess(turn))
  assert.equal(isTurnFoldable(turn, turn.key), true)
  assert.equal(isTurnFoldable(turn, 'newest'), true)
  assert.equal(isTurnFoldable({ ...turn, completed: false }, 'newest'), false)
})

test('timeline owner passes current timeline and viewer state to pure projection', () => {
  assert.match(timelineController, /presentTurnParts\(turn, latestTurnKey\.value, processExpandedOverrides\.value, detail\)/)
  assert.match(timelineController, /turnProcessFoldSignature\(turn, latestTurnKey\.value, processExpandedOverrides\.value, detail\)/)
  assert.match(timelineController, /isTurnProcessExpanded\(turn, latestTurnKey\.value, processExpandedOverrides\.value\)/)
  assert.match(timelineController, /const latestTurnKey = computed\(\(\) => \{[\s\S]*?const list = turns\.value/)
})

test('fold state is per-turn and replaced on toggle so memo sees it', () => {
  assert.match(timelineController, /const processExpandedOverrides = ref\(new Map<string, boolean>\(\)\)/)
  assert.match(timelineController, /function toggleTurnProcess\(turn: TimelineTurn\): void \{[\s\S]*?new Map\(processExpandedOverrides\.value\)/)
})

test('the process header is the single toggle for its detail', () => {
  assert.match(turnPartsComponent, /v-else-if="part\.kind === 'process'"/)
  assert.doesNotMatch(turnPartsComponent, /process_end/)
  assert.equal((turnPartsComponent.match(/@click="toggleTurnProcess\(turn\)"/g) ?? []).length, 1)
  assert.match(turnPartsComponent, /:aria-expanded="part\.expanded"/)
  assert.match(turnPartsComponent, /part\.expanded \? '▾' : '▸'/)
})

test('an open process header stays reachable from any scroll depth', () => {
  const openRule = turnPartsComponent.match(/\.process-fold--open \{[\s\S]*?\n\}/)
  assert.ok(openRule)
  assert.match(openRule[0], /position: sticky/)
})

test('long thinking, tool, and sub-agent cards expose their own collapse footer', () => {
  for (const component of [thinkingComponent, toolComponent, subagentComponent]) {
    assert.match(component, /class="details-collapse"/)
    assert.match(component, /@click="collapseDetails"/)
  }
  assert.match(timelineController, /function collapseDetails\(event: MouseEvent\): void \{[\s\S]*?closest\('details'\)/)
})

test('sticky summary keeps its scrolling ancestor intact', () => {
  assert.match(toolComponent, /position: sticky/)
  const toolCard = toolComponent.match(/\.tool-card \{[\s\S]*?\n\}/)
  assert.ok(toolCard)
  assert.doesNotMatch(toolCard[0], /overflow: hidden/)
  assert.match(toolCard[0], /overflow: clip/)
})


test('the folded header says when a card is folded in with it', () => {
  // The header is the only thing left on screen, so a card inside must not
  // vanish without a trace.
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'approval_required', { tool_call_id: 'q1', kind: 'question', title: 'Pick' }),
    makeEvent(4, 'text_delta', { text: 'the answer' }),
    makeEvent(5, 'turn_completed', { status: 'completed' }),
  ])[0]
  const split = splitTurnProcess(turn)
  const folded = foldTurnParts(turn, false)
  assert.equal(folded[0].kind, 'process')
  assert.match(folded[0].meta, /1 张审批卡/, 'the header must count the card inside')

  const plain = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'thinking_delta', { text: 'hmm' }),
    makeEvent(3, 'text_delta', { text: 'the answer' }),
    makeEvent(4, 'turn_completed', { status: 'completed' }),
  ])[0]
  assert.doesNotMatch(
    foldTurnParts(plain, false)[0].meta,
    /审批卡/,
    'a turn without one must not claim to have one',
  )
  assert.ok(split)
})

test('a subagent region with dispatch or child errors stays pinned when process folds', () => {
  const childId = '01a0e2f6-ef37-7031-b97e-fecf618eef5a'
  const turn = groupEventsIntoTurns([
    makeEvent(1, 'turn_started', { summary: 'delegate' }),
    makeEvent(2, 'tool_call_started', {
      tool_call_id: 'spawn-1', name: 'spawnAgent',
      args: { prompt: 'inspect', receiverThreadIds: [childId] },
    }, { call_id: 'spawn-1' }),
    makeEvent(3, 'tool_call_completed', {
      tool_call_id: 'spawn-1', status: 'failed', result: 'dispatch failed',
    }, { call_id: 'spawn-1' }),
    makeEvent(4, 'status', {
      text: 'child connection failed', provider_status: 'error',
      subagent_thread: childId,
    }, { message_id: 'child-error-1' }),
    makeEvent(5, 'text_delta', { text: 'fallback answer' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }),
  ])[0]

  const region = turn.parts.find(part => part.kind === 'subagents')
  assert.ok(region)
  assert.equal(subagentRegionErrorCount(region), 2)
  assert.equal(region.threads[0].lifecycleStatus, 'unknown', 'errors are not lifecycle evidence')
  assert.deepEqual(splitTurnProcess(turn).pinned.map(part => part.kind), ['subagents'])
  assert.deepEqual(
    foldTurnParts(turn, false).map(part => part.kind),
    ['process', 'subagents', 'text'],
  )
})
