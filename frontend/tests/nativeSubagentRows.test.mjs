import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const {
  NATIVE_SUBAGENT_ROW_LIMIT,
  nativeSubagentRow,
  nativeSubagentRows,
  findNativeSubthread,
} = await loadChatFeature('presentation/nativeSubagentRows')

function launchTool(overrides = {}) {
  return {
    key: 'tool-1',
    callId: 'call-1',
    name: 'Agent',
    status: 'completed',
    argsText: '',
    resultText: '',
    subagent: {
      provider: 'claude',
      agentType: 'Explore',
      description: 'find the reducer',
      prompt: 'Find where child events are reduced',
      threadIds: [],
      background: false,
      directive: false,
      ...overrides,
    },
  }
}

function thread(overrides = {}) {
  return {
    key: 'subthread-1',
    threadId: 'toolu_01child',
    parts: [{ kind: 'text', key: 'text-1', text: 'Reducer lives in subthreads.ts', at: '2026-11-10T10:00:01Z' }],
    launchTool: launchTool(),
    lifecycleStatus: 'completed',
    renderRevision: 1,
    ...overrides,
  }
}

function region(threads, key = 'subagents-turn-1') {
  return { kind: 'subagents', key, threads }
}

function turn(overrides = {}) {
  return {
    key: 'turn-1',
    turnId: 't-1',
    userText: 'question',
    origin: 'web',
    feishu: null,
    hubMessage: null,
    mode: 'default',
    yieldsToUser: false,
    startedAt: '2026-11-10T10:00:00Z',
    completed: true,
    completionStatus: 'completed',
    completionError: null,
    parts: [region([thread()])],
    errors: [],
    approvals: [],
    attachments: [],
    renderRevision: 3,
    ...overrides,
  }
}

test('a child becomes one row with the dock vocabulary', () => {
  const row = nativeSubagentRow(turn(), thread())
  assert.equal(row.kind, 'native')
  assert.equal(row.key, 'turn-1:toolu_01child')
  assert.equal(row.turnKey, 'turn-1')
  assert.equal(row.turnId, 't-1')
  assert.equal(row.threadId, 'toolu_01child')
  // The provider names the child type; the row keeps it and labels the source.
  assert.equal(row.name, 'Explore')
  assert.equal(row.agentType, 'Claude sub-agent')
  // Collapsed headline is the child's own last report, not the parent's answer.
  assert.equal(row.summary, 'Reducer lives in subthreads.ts')
  assert.equal(row.state, 'completed')
  assert.equal(row.stateLabel, 'Completed')
  assert.equal(row.group, 'earlier')
  assert.equal(row.createdAt, '2026-11-10T10:00:00Z')
  // Native children cannot jump to a Chat tab; they open a detail window.
  assert.equal(row.canJump, false)
  assert.equal(row.canOpen, true)
})

test('running children group as active and unfinished ones keep their own state', () => {
  assert.equal(nativeSubagentRow(turn(), thread({ lifecycleStatus: 'active' })).group, 'active')
  assert.equal(nativeSubagentRow(turn(), thread({ lifecycleStatus: 'active' })).state, 'running')
  assert.equal(nativeSubagentRow(turn(), thread({ lifecycleStatus: 'failed' })).stateLabel, 'Failed')
  assert.equal(nativeSubagentRow(turn(), thread({ lifecycleStatus: 'cancelled' })).stateLabel, 'Cancelled')
  // A provider that has not reported a lifecycle yet is not guessed to be done.
  assert.equal(nativeSubagentRow(turn(), thread({ lifecycleStatus: 'unknown' })).stateLabel, 'Unknown')
})

test('identity falls back to the provider thread id and provider label', () => {
  const bare = nativeSubagentRow(turn(), thread({ launchTool: null, threadId: 'a-very-long-provider-thread-id' }))
  assert.equal(bare.name, 'Thread a-very-l')
  assert.equal(bare.agentType, 'Native sub-agent')
  // A short id stays whole rather than being padded into a fake "Thread".
  assert.equal(nativeSubagentRow(turn(), thread({ launchTool: null, threadId: 'tu-1' })).name, 'tu-1')
  // startableAt is optional in provider data: a row without one shows no clock.
  assert.equal(nativeSubagentRow(turn({ startedAt: null }), thread()).createdAt, '')
})

test('turns are listed newest first and children newest first inside a turn', () => {
  const older = turn({ key: 'turn-1', turnId: 't-1' })
  const newer = turn({
    key: 'turn-2',
    turnId: 't-2',
    parts: [region([
      thread({ threadId: 'child-a', key: 'subthread-a' }),
      thread({ threadId: 'child-b', key: 'subthread-b' }),
    ], 'subagents-turn-2')],
  })
  const rows = nativeSubagentRows([older, newer])
  assert.deepEqual(rows.map(row => row.key), ['turn-2:child-b', 'turn-2:child-a', 'turn-1:toolu_01child'])
})

test('a turn without a sub-agent region contributes no rows', () => {
  const rows = nativeSubagentRows([turn({ parts: [] })])
  assert.deepEqual(rows, [])
})

test('the list is bounded, keeping the newest children', () => {
  const threads = []
  for (let index = 0; index < NATIVE_SUBAGENT_ROW_LIMIT + 5; index += 1) {
    threads.push(thread({ threadId: `child-${index}`, key: `subthread-${index}` }))
  }
  const rows = nativeSubagentRows([turn({ parts: [region(threads)] })])
  assert.equal(rows.length, NATIVE_SUBAGENT_ROW_LIMIT)
  assert.equal(rows[0].threadId, `child-${NATIVE_SUBAGENT_ROW_LIMIT + 4}`)
})

test('the memo skips unchanged turns and refreshes a bumped one', () => {
  const memo = new Map()
  const stable = turn()
  const first = nativeSubagentRows([stable], memo)
  assert.deepEqual(nativeSubagentRows([stable], memo).map(row => row.key), first.map(row => row.key))

  // A visible child mutation bumps the turn revision; the row must follow it.
  const updated = turn({
    parts: [region([thread({ lifecycleStatus: 'active' })])],
    renderRevision: 4,
  })
  const rows = nativeSubagentRows([updated], memo)
  assert.equal(rows[0].state, 'running')
  assert.deepEqual([...memo.keys()], ['turn-1'])

  // Turns that left the pane's history must not pin their memoised rows.
  nativeSubagentRows([], memo)
  assert.equal(memo.size, 0)
})

test('rows resolve back to the live child, with its transcript position', () => {
  const older = turn({ key: 'turn-1' })
  const newer = turn({ key: 'turn-2', parts: [region([thread({ threadId: 'child-b' })], 'subagents-turn-2')] })
  const lookup = findNativeSubthread([older, newer], 'turn-2', 'child-b')
  assert.ok(lookup)
  assert.equal(lookup.thread.threadId, 'child-b')
  assert.equal(lookup.ordinal, 2)
  assert.equal(lookup.turn.key, 'turn-2')
  // A child that left the transcript (or a stale row) resolves to nothing.
  assert.equal(findNativeSubthread([older, newer], 'turn-2', 'child-a'), null)
  assert.equal(findNativeSubthread([older, newer], 'turn-9', 'child-b'), null)
})
