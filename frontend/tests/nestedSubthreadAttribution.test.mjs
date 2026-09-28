import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// Load the timeline the same way agentStreamTimelineReducer.test.mjs does:
// transpile the TS and inline its sibling utils (a data-URL module has no
// resolver). Events below are the exact normalized shapes the backend now
// emits for tab 4ed6b70c (TraeX nested sub-agent / collab sendInput).
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
const subagentSource = await readFile(
  new URL('../src/utils/subagentTool.ts', import.meta.url),
  'utf8',
)
const agentImageSource = await readFile(
  new URL('../src/utils/agentImage.ts', import.meta.url),
  'utf8',
)
const copySource = await readFile(
  new URL('../src/utils/chatTurnCopy.ts', import.meta.url),
  'utf8',
)
const transpileOptions = {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
}
const js = (s) => ts.transpileModule(s, transpileOptions).outputText
const { outputText } = ts.transpileModule(
  source.replace(/^import .* from '@\/utils\/.*$/gm, ''),
  transpileOptions,
)
const bundled = [
  js(durationSource),
  js(questionSource),
  js(subagentSource),
  js(agentImageSource),
  outputText,
  js(copySource),
].join('\n')
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(bundled).toString('base64')}`
)
const { groupEventsIntoTurns, countProcessSteps, buildTurnCopyText } = mod
const { parseSubagent } = await import(
  `data:text/javascript;base64,${Buffer.from(js(subagentSource)).toString('base64')}`
)

// Real receiver thread ids from tab 4ed6b70c (spawnAgent -> F6; sendInput to
// F6 and FEC; nested exec_command items are code-mode-nested:29:…).
const F6 = '01a0e2f6-ef37-7031-b97e-fecf618eef5a'
const FEC = '01a0e2ec-54c9-74f3-bcc0-369634ac31de'
const TURN = '3a836014-ffe2-43a7-b719-cafe4bc3f21c'

let seq = 0
function ev(type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq++,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'traex',
    type,
    payload,
    created_at: '2026-09-27T13:00:00Z',
    redacted: false,
    turn_id: TURN,
    ...overrides,
  }
}
// Stamped exactly as NormalizeContext.event stamps a sub-agent record:
// payload.subagent_thread + a thread-scoped message_id.
function sub(type, threadId, payload = {}, overrides = {}) {
  const role = type === 'thinking_delta' ? 'thinking' : 'assistant'
  return ev(type, { ...payload, subagent_thread: threadId }, {
    message_id: `${TURN}:sub:${threadId}:${role}`,
    ...overrides,
  })
}

function realSequence() {
  seq = 0
  return [
    ev('turn_started', { summary: 'use your own subagent', mode: 'default' },
      { message_id: `${TURN}:user` }),
    // Main agent's own narration stays on the main stream.
    ev('thinking_delta', { text: 'main planning' }, { message_id: `${TURN}:thinking` }),
    ev('text_delta', { text: '我先派内建 worker 去做。' }, { message_id: `${TURN}:assistant` }),
    // spawnAgent launches the F6 worker (main-agent tool call, no thread tag).
    ev('tool_call_started', {
      tool_call_id: 'call_spawn', name: 'spawnAgent',
      args: { prompt: 'develop the triton kernel', receiverThreadIds: [F6] },
    }, { call_id: 'call_spawn' }),
    ev('tool_call_completed', {
      tool_call_id: 'call_spawn', status: 'completed',
      result: JSON.stringify({ [F6]: { status: 'pendingInit' } }),
    }, { call_id: 'call_spawn' }),
    // F6 child: thinking + prose + a nested exec_command, all thread-tagged.
    sub('thinking_delta', F6, { text: 'worker reading skills' }),
    sub('text_delta', F6, { text: 'worker: starting on merlin_dev' }),
    sub('tool_call_started', F6, {
      tool_call_id: 'code-mode-nested:29:call_x:exec-1', name: 'exec_command',
      args: { cmd: 'sed -n 1,240p SKILL.md', cwd: '/tmp' },
    }, { call_id: 'code-mode-nested:29:call_x:exec-1', message_id: null }),
    sub('tool_call_completed', F6, {
      tool_call_id: 'code-mode-nested:29:call_x:exec-1', status: 'completed', result: 'ok',
    }, { call_id: 'code-mode-nested:29:call_x:exec-1', message_id: null }),
    // sendInput follow-up to F6 (main-agent tool call) → instruction in F6.
    ev('tool_call_started', {
      tool_call_id: 'call_si_f6', name: 'sendInput',
      args: { prompt: 'prioritize the one-call API', receiverThreadIds: [F6] },
    }, { call_id: 'call_si_f6' }),
    ev('tool_call_completed', {
      tool_call_id: 'call_si_f6', status: 'completed',
      result: JSON.stringify({ [F6]: { status: 'running' } }),
    }, { call_id: 'call_si_f6' }),
    // A second thread FEC gets its own sendInput + report.
    ev('tool_call_started', {
      tool_call_id: 'call_si_fec', name: 'sendInput',
      args: { prompt: 'fullgraph blocker fixed', receiverThreadIds: [FEC] },
    }, { call_id: 'call_si_fec' }),
    sub('text_delta', FEC, { text: 'fec worker: allclose passes' }),
    // Main agent's final conclusion remains the main bubble.
    ev('text_delta', { text: 'Kernel 已完成，单 launch。' }, { message_id: `${TURN}:assistant` }),
    ev('turn_completed', { status: 'completed' }, { message_id: TURN }),
  ]
}

test('parseSubagent recognizes sendInput as a directive and keeps spawnAgent a spawn', () => {
  const spawn = parseSubagent('spawnAgent', { prompt: 'go', receiverThreadIds: [F6] })
  assert.ok(spawn)
  assert.equal(spawn.provider, 'traex')
  assert.equal(spawn.directive, false)
  assert.deepEqual(spawn.threadIds, [F6])

  const si = parseSubagent('sendInput', { prompt: 'status?', receiverThreadIds: [FEC] })
  assert.ok(si)
  assert.equal(si.directive, true)
  assert.deepEqual(si.threadIds, [FEC])

  // Same name without the required thread ids / prompt fails closed.
  assert.equal(parseSubagent('sendInput', { prompt: 'x' }), null)
  assert.equal(parseSubagent('sendInput', { receiverThreadIds: [F6] }), null)
})

test('subthread deltas/tools group by thread; main bubble keeps only the main agent', () => {
  const [turn] = groupEventsIntoTurns(realSequence())

  // Exactly two nested groups, one per receiver thread, in first-seen order.
  const groups = turn.parts.filter((p) => p.kind === 'subthread')
  assert.deepEqual(groups.map((g) => g.threadId), [F6, FEC])

  const f6 = groups[0]
  const kinds = f6.parts.map((p) => p.kind)
  // thinking + text + tool group + the sendInput directive (the spawn itself
  // keeps its own dedicated sub-agent card, so it is not duplicated here).
  assert.ok(kinds.includes('instruction'))
  assert.ok(kinds.includes('thinking'))
  assert.ok(kinds.includes('text'))
  assert.ok(kinds.includes('tool_group'))
  // The child's exec_command is inside the group, not a main turn tool.
  const childTools = f6.parts.find((p) => p.kind === 'tool_group').tools
  assert.equal(childTools[0].name, 'exec_command')
  assert.equal(childTools[0].status, 'completed')
  // Only the follow-up sendInput is filed as an in-group instruction; the
  // spawnAgent launch renders its own dedicated sub-agent card on the turn.
  const instructions = f6.parts.filter((p) => p.kind === 'instruction')
  assert.deepEqual(instructions.map((i) => i.tool.name), ['sendInput'])

  // The spawn launch keeps its standalone sub-agent card (no regression to the
  // existing card) in arrival order before the child thread region.
  const spawnCard = turn.parts.find((p) => p.kind === 'subagent')
  assert.ok(spawnCard)
  assert.equal(spawnCard.tool.name, 'spawnAgent')
  assert.equal(turn.parts.indexOf(spawnCard), turn.parts.indexOf(groups[0]) - 1)

  // Main assistant text contains only the main agent's two prose lines.
  assert.equal(turn.assistantText, '我先派内建 worker 去做。Kernel 已完成，单 launch。')
  // Child tools never leak into the main turn tool bucket.
  assert.ok(!turn.tools.some((t) => t.name === 'exec_command'))
  // spawnAgent/sendInput stay resolvable as main tools (their completion result
  // updates by call id) even though sendInput renders inside the child group.
  assert.deepEqual(
    turn.tools.map((t) => t.name).sort(),
    ['sendInput', 'sendInput', 'spawnAgent'],
  )
  // All child content is retained (expandable): thinking + report + tool out.
  const f6Text = f6.parts.filter((p) => p.kind === 'text').map((p) => p.text).join('')
  assert.match(f6Text, /starting on merlin_dev/)
})

test('multiple receiver threads are grouped separately and an unattributed event falls back', () => {
  const events = realSequence()
  // Insert a stray main-thread tool with NO subagent_thread between children.
  events.splice(9, 0, ev('tool_call_started', {
    tool_call_id: 'call_mainread', name: 'Read', args: { file_path: '/tmp/a' },
  }, { call_id: 'call_mainread', message_id: null }))
  const [turn] = groupEventsIntoTurns(events)

  const groups = turn.parts.filter((p) => p.kind === 'subthread')
  assert.equal(groups.length, 2)
  assert.equal(groups[1].threadId, FEC)
  assert.match(groups[1].parts.find((p) => p.kind === 'text').text, /allclose/)

  // The unattributed Read stays a normal top-level main tool group.
  const mainGroups = turn.parts.filter((p) => p.kind === 'tool_group')
  assert.ok(mainGroups.some((g) => g.tools.some((t) => t.name === 'Read')))
})

test('empty/whitespace subagent_thread safely degrades to the main stream', () => {
  seq = 0
  const events = [
    ev('turn_started', { summary: 's' }, { message_id: `${TURN}:user` }),
    ev('text_delta', { text: 'child?', subagent_thread: '   ' },
      { message_id: `${TURN}:assistant` }),
    ev('turn_completed', { status: 'completed' }, { message_id: TURN }),
  ]
  const [turn] = groupEventsIntoTurns(events)
  assert.equal(turn.parts.filter((p) => p.kind === 'subthread').length, 0)
  assert.equal(turn.assistantText, 'child?')
})

test('copy text and process counts do not fold child prose into the main answer', () => {
  const [turn] = groupEventsIntoTurns(realSequence())
  const copy = buildTurnCopyText(turn)
  assert.match(copy, /Kernel 已完成/)
  // Child report and directives must not be copied as the assistant's answer.
  assert.ok(!/allclose passes/.test(copy))
  assert.ok(!/starting on merlin_dev/.test(copy))

  // Work steps = the spawnAgent launch card (1) plus the child's one exec tool
  // (1). The sendInput directive is an instruction, not a tool action, and is
  // not double-counted; child prose is never counted.
  const steps = countProcessSteps(turn.parts)
  assert.equal(steps, 2)
})

test('every rendered subthread part key is unique even when kinds repeat', () => {
  // Regression for the :key="sub.kind" bug: a child that emits several text /
  // thinking / tool_group rows produced duplicate keys, so Vue reused the wrong
  // vnodes and dropped rows. Build a child with the same kind repeated.
  seq = 0
  const events = [
    ev('turn_started', { summary: 's' }, { message_id: `${TURN}:user` }),
    sub('text_delta', F6, { text: 'first line' }),
    sub('tool_call_started', F6, {
      tool_call_id: 'c1', name: 'exec_command', args: { cmd: 'echo 1' },
    }, { call_id: 'c1', message_id: null }),
    sub('tool_call_completed', F6, {
      tool_call_id: 'c1', status: 'completed', result: '1',
    }, { call_id: 'c1', message_id: null }),
    // A second text row after a tool can no longer merge with the first and
    // must get its own distinct key.
    sub('text_delta', F6, { text: 'second line' }),
    sub('thinking_delta', F6, { text: 'think a' }),
    sub('thinking_delta', F6, { text: 'then think b' }),
    sub('tool_call_started', F6, {
      tool_call_id: 'c2', name: 'exec_command', args: { cmd: 'echo 2' },
    }, { call_id: 'c2', message_id: null }),
    ev('turn_completed', { status: 'completed' }, { message_id: TURN }),
  ]
  const [turn] = groupEventsIntoTurns(events)
  const groups = turn.parts.filter((p) => p.kind === 'subthread')
  assert.equal(groups.length, 1)
  const keys = groups[0].parts.map((p) => p.key)
  assert.equal(new Set(keys).size, keys.length, `duplicate subthread keys: ${keys}`)
  // Both separated text rows survive (the original bug dropped one).
  const texts = groups[0].parts.filter((p) => p.kind === 'text').map((p) => p.text)
  assert.deepEqual(texts, ['first line', 'second line'])
})

test('four spawned child threads each get their own group with unique keys', () => {
  // Mirrors real tab 4ed6b70c: one F6 + three 01a0e38b… spawns streaming in
  // parallel; each child's tools/deltas must land in its own card, never a
  // shared synthetic bucket.
  const T1 = '01a0e38b-0000-7031-b97e-aaaaaaaaaaaa'
  const T2 = '01a0e38b-1111-7031-b97e-bbbbbbbbbbbb'
  const T3 = '01a0e38b-2222-7031-b97e-cccccccccccc'
  seq = 0
  const events = [
    ev('turn_started', { summary: 's' }, { message_id: `${TURN}:user` }),
    sub('text_delta', F6, { text: 'f6 report' }),
    sub('tool_call_started', T1, {
      tool_call_id: 'nested-t1', name: 'exec_command', args: { cmd: 't1' },
    }, { call_id: 'nested-t1', message_id: null }),
    sub('tool_call_started', T2, {
      tool_call_id: 'nested-t2', name: 'exec_command', args: { cmd: 't2' },
    }, { call_id: 'nested-t2', message_id: null }),
    sub('tool_call_started', T3, {
      tool_call_id: 'nested-t3', name: 'exec_command', args: { cmd: 't3' },
    }, { call_id: 'nested-t3', message_id: null }),
    ev('turn_completed', { status: 'completed' }, { message_id: TURN }),
  ]
  const [turn] = groupEventsIntoTurns(events)
  const groups = turn.parts.filter((p) => p.kind === 'subthread')
  assert.deepEqual(groups.map((g) => g.threadId), [F6, T1, T2, T3])
  // Top-level group keys are unique as well.
  const groupKeys = groups.map((g) => g.key)
  assert.equal(new Set(groupKeys).size, 4)
  // Each child tool is attributed exactly to its own thread.
  for (const [i, cid] of ['nested-t1', 'nested-t2', 'nested-t3'].entries()) {
    const tg = groups[i + 1].parts.find((p) => p.kind === 'tool_group')
    assert.equal(tg.tools[0].callId, cid)
  }
  assert.ok(!turn.tools.some((t) => t.callId?.startsWith('nested-')))
})

test('incremental reducer groups subthread events identically on append', () => {
  const events = realSequence()
  const reducer = new mod.IncrementalTimelineReducer()
  const first = reducer.reduce(events.slice(0, 8))
  // (The reducer mutates turn objects in place, so the streaming snapshot's
  // counts must be captured before the second reduce appends FEC.)
  const firstGroupCount = first[0].parts.filter((p) => p.kind === 'subthread').length
  const full = reducer.reduce(events)
  // Streaming half-way already mounted the F6 group with its first rows.
  assert.equal(firstGroupCount, 1)
  const groups = full[0].parts.filter((p) => p.kind === 'subthread')
  assert.deepEqual(groups.map((g) => g.threadId), [F6, FEC])
  assert.equal(full[0].assistantText, '我先派内建 worker 去做。Kernel 已完成，单 launch。')
})

test('child status with stable snapshot replaces in place, not appended', () => {
  // Regression for review defect (1) frontend: a child's STATUS with
  // snapshot=true + a stable message_id must replace in place.
  const rsId = 'rs_child_status_001'
  const statusEvent = (text) => ev('status', {
    text, snapshot: true, subagent_thread: F6,
  }, { message_id: `reasoning:${rsId}` })

  const events = [
    ev('turn_started', { summary: 'use a child' }, { message_id: `${TURN}:user` }),
    ev('tool_call_started', {
      tool_call_id: 'call_spawn', name: 'spawnAgent',
      args: { prompt: 'go', receiverThreadIds: [F6] },
    }, { call_id: 'call_spawn' }),
    ev('tool_call_completed', {
      tool_call_id: 'call_spawn', status: 'completed', result: '{}',
    }, { call_id: 'call_spawn' }),
    // Child reasoning starts → "Thinking…" status.
    statusEvent('Thinking…'),
    // Child reasoning completes → "Done thinking" replaces in place.
    statusEvent('Done thinking'),
    ev('turn_completed', { status: 'completed' }, { message_id: TURN }),
  ]
  const [turn] = groupEventsIntoTurns(events)
  const group = turn.parts.find((p) => p.kind === 'subthread')
  assert.ok(group, 'F6 subthread group should exist')
  // Only ONE status part (replaced in place), with the final text.
  const statuses = group.parts.filter((p) => p.kind === 'status')
  assert.equal(statuses.length, 1)
  assert.equal(statuses[0].text, 'Done thinking')
  assert.equal(statuses[0].messageId, `reasoning:${rsId}`)
})

test('cancelled turn finalizes in-flight child status (snapshot replay)', () => {
  // Regression for review defect (2): a cancelled turn must not leave a
  // stale "Thinking…" status. The final status replaces the in-flight one.
  const rsId = 'rs_child_cancel_001'
  const events = [
    ev('turn_started', { summary: 'use a child' }, { message_id: `${TURN}:user` }),
    ev('tool_call_started', {
      tool_call_id: 'call_spawn', name: 'spawnAgent',
      args: { prompt: 'go', receiverThreadIds: [F6] },
    }, { call_id: 'call_spawn' }),
    ev('tool_call_completed', {
      tool_call_id: 'call_spawn', status: 'completed', result: '{}',
    }, { call_id: 'call_spawn' }),
    // Child reasoning starts → "Thinking…" (in-flight).
    ev('status', { text: 'Thinking…', snapshot: true, subagent_thread: F6 },
      { message_id: `reasoning:${rsId}` }),
    // Turn cancelled WITHOUT item/completed → final status replaces in place.
    ev('status', { text: 'Thinking interrupted', snapshot: true, subagent_thread: F6 },
      { message_id: `reasoning:${rsId}` }),
    ev('turn_completed', { status: 'cancelled' }, { message_id: TURN }),
  ]
  const [turn] = groupEventsIntoTurns(events)
  const group = turn.parts.find((p) => p.kind === 'subthread')
  assert.ok(group, 'F6 subthread group should exist')
  const statuses = group.parts.filter((p) => p.kind === 'status')
  assert.equal(statuses.length, 1)
  assert.equal(statuses[0].text, 'Thinking interrupted')
  // The in-flight "Thinking…" is gone — no stale indicator.
  assert.ok(!statuses.some((s) => s.text === 'Thinking…'))
})
