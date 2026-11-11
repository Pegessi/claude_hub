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

test('a turn whose children are not readable yet contributes no rows', () => {
  // A lazy historical turn can already carry a stub failed child from the
  // shallow page; the window could only show a bare status, so it is skipped
  // until that turn's execution detail is loaded.
  const observed = []
  const pending = turn({ key: 'turn-2', parts: [region([thread({ lifecycleStatus: 'failed' })])] })
  const rows = nativeSubagentRows([turn(), pending], undefined, candidate => {
    observed.push(candidate.key)
    return candidate.key !== 'turn-2'
  })
  assert.deepEqual(observed, ['turn-2', 'turn-1'])
  assert.deepEqual(rows.map(row => row.turnKey), ['turn-1'])
  // Once readable, the same turn projects normally.
  const ready = nativeSubagentRows([turn(), pending], undefined, () => true)
  assert.equal(ready.length, 2)
})

test('the memo cannot survive a rebuilt turn that reused its revisions', () => {
  const memo = new Map()
  const original = turn()
  nativeSubagentRows([original], memo)
  // `IncrementalTimelineReducer.reset()` rebuilds turns and restarts their
  // revision count, so an equal revision on a different instance is not a hit.
  const rebuilt = turn()
  assert.notEqual(rebuilt, original)
  rebuilt.parts = [region([thread({ lifecycleStatus: 'failed' })])]
  const rows = nativeSubagentRows([rebuilt], memo, undefined)
  assert.equal(rows[0].state, 'failed')
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

test('MASO sub-agents keep readable thread ids, use child SendMessage reports, and deduplicate across turns', () => {
  const spawnTurn = turn({
    key: 'turn-1',
    turnId: 't-1',
    parts: [
      region([
        thread({
          threadId: 'posthoc_reviewer-a38c',
          provider: 'maso',
          lifecycleStatus: 'active',
          launchTool: launchTool({
            provider: 'maso',
            agentType: 'posthoc_reviewer',
            description: 'Post-hoc review of merged main',
            prompt: 'Review merged commits on main.',
            threadIds: ['posthoc_reviewer-a38c'],
            background: true,
            directive: false,
          }),
          parts: [],
        }),
      ]),
    ],
  })
  const followupTurn = turn({
    key: 'turn-2',
    turnId: 't-2',
    parts: [
      region([
        thread({
          threadId: 'posthoc_reviewer-a38c',
          provider: 'maso',
          lifecycleStatus: 'completed',
          launchTool: null,
          parts: [
            {
              kind: 'tool_group',
              key: 'sub-tg-1',
              tools: [
                {
                  key: 'tool-send-1',
                  callId: 'send-1',
                  name: 'SendMessage',
                  status: 'completed',
                  argsText: JSON.stringify({
                    agent_id: 'parent',
                    message: 'Fix review GO: all 4 probes verified.\nDetails follow.',
                  }),
                  resultText: 'ok',
                },
              ],
            },
          ],
        }),
      ], 'subagents-turn-2'),
    ],
  })
  const rows = nativeSubagentRows([spawnTurn, followupTurn])
  assert.equal(rows.length, 1)
  assert.equal(rows[0].key, 'turn-2:posthoc_reviewer-a38c')
  assert.equal(rows[0].name, 'posthoc_reviewer-a38c')
  assert.equal(rows[0].agentType, 'MASO sub-agent')
  assert.equal(rows[0].state, 'completed')
  assert.equal(rows[0].group, 'earlier')
  assert.equal(rows[0].summary, 'Fix review GO: all 4 probes verified.')

  const merged = findNativeSubthread([spawnTurn, followupTurn], 'turn-2', 'posthoc_reviewer-a38c')
  assert.equal(merged?.ordinal, 2)
  assert.equal(merged?.thread.launchTool?.subagent?.prompt, 'Review merged commits on main.')
  assert.equal(merged?.thread.lifecycleStatus, 'completed')
  assert.equal(merged?.thread.parts.length, 1)

  // A standalone Claude Agent part (before nested child events hydrate) also
  // projects into the dock and resolves in findNativeSubthread.
  const standaloneClaudeTurn = turn({
    key: 'turn-claude',
    turnId: 't-claude',
    parts: [
      {
        kind: 'subagent',
        key: 'subagent-call-claude',
        tool: launchTool(),
      },
    ],
  })
  const claudeRows = nativeSubagentRows([standaloneClaudeTurn])
  assert.equal(claudeRows.length, 1)
  assert.equal(claudeRows[0].threadId, 'call-1')
  assert.equal(claudeRows[0].name, 'Explore')
  assert.equal(claudeRows[0].agentType, 'Claude sub-agent')
  assert.equal(
    findNativeSubthread([standaloneClaudeTurn], 'turn-claude', 'call-1')?.thread.launchTool?.name,
    'Agent',
  )
})
