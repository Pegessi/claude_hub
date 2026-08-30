import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/boardPagination.ts', import.meta.url),
  'utf8',
)
const runtimeSource = helperSource.replace(
  /import type \{[\s\S]*?\} from '@\/types'\n?/,
  '',
)
const { outputText } = ts.transpileModule(runtimeSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const helperUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const {
  applyBoardPayloadState,
  BOARD_TASKS_PAGE_SIZE,
  boardOlderRemainingCount,
  boardTasksLimitForPoll,
  loadedDoneTaskCount,
  isStaleBoardGeneration,
  MAX_BOARD_TASKS_LIMIT,
  mergeBoardPollPagination,
  mergeBoardPollReports,
  mergeBoardPollTasks,
  mergeBoardReports,
  mergeBoardTasks,
  nextBoardFetchGeneration,
  runBoardLoadMoreAttempt,
  shouldAcceptBoardPayload,
  shouldCoalesceBoardFetch,
} = await import(helperUrl)

function board(workspaceId, tasks, pagination = null) {
  return {
    workspace: { id: workspaceId },
    tasks,
    reports: [],
    tasks_pagination: pagination,
  }
}

function taskIds(tasks) {
  return tasks.map(task => task.id)
}

test('BOARD_TASKS_PAGE_SIZE defaults to 15', () => {
  assert.equal(BOARD_TASKS_PAGE_SIZE, 15)
})

test('boardTasksLimitForPoll keeps expanded loaded window up to server max', () => {
  assert.equal(boardTasksLimitForPoll(15), 15)
  assert.equal(boardTasksLimitForPoll(30), 30)
  assert.equal(boardTasksLimitForPoll(105), MAX_BOARD_TASKS_LIMIT)
  assert.equal(MAX_BOARD_TASKS_LIMIT, 100)
})

test('shouldCoalesceBoardFetch reuses in-flight poll but not reset', () => {
  assert.equal(shouldCoalesceBoardFetch(false, true), true)
  assert.equal(shouldCoalesceBoardFetch(false, false), false)
  assert.equal(shouldCoalesceBoardFetch(true, true), false)
})

test('mergeBoardTasks replaces on initial fetch and appends older pages', () => {
  const first = [{ id: 'a' }, { id: 'b' }]
  const second = [{ id: 'c' }]
  assert.deepEqual(mergeBoardTasks(first, second, false), second)
  assert.deepEqual(mergeBoardTasks(first, second, true), [
    { id: 'a' },
    { id: 'b' },
    { id: 'c' },
  ])
})

test('mergeBoardTasks dedupes when appending', () => {
  const existing = [{ id: 'a' }, { id: 'b' }]
  const incoming = [{ id: 'b' }, { id: 'c' }]
  assert.deepEqual(mergeBoardTasks(existing, incoming, true), [
    { id: 'a' },
    { id: 'b' },
    { id: 'c' },
  ])
})

test('mergeBoardPollTasks preserves expanded tail beyond bounded poll window', () => {
  const existing = Array.from({ length: 105 }, (_, index) => ({
    id: `t${index}`,
    status: 'done',
  }))
  const incoming = Array.from({ length: 100 }, (_, index) => ({
    id: `t${index}`,
    status: 'done',
  }))
  const merged = mergeBoardPollTasks(existing, incoming)
  assert.equal(merged.length, 105)
  assert.deepEqual(taskIds(merged.slice(-5)), ['t100', 't101', 't102', 't103', 't104'])
})

test('mergeBoardPollPagination keeps older cursor when expanded done history remains', () => {
  const merged = mergeBoardPollPagination(
    {
      total_count: 120,
      has_more: true,
      limit: 100,
      next_cursor: 'older-cursor',
    },
    {
      total_count: 120,
      has_more: true,
      limit: 100,
      next_cursor: 'poll-cursor',
    },
    105,
  )
  assert.equal(merged.has_more, true)
  assert.equal(merged.next_cursor, 'older-cursor')
})

test('applyBoardPayloadState poll refresh preserves expanded done history and cursor', () => {
  const existingTasks = Array.from({ length: 105 }, (_, index) => ({
    id: `t${index}`,
    status: 'done',
  }))
  const incomingTasks = Array.from({ length: 100 }, (_, index) => ({
    id: `t${index}`,
    status: 'done',
  }))
  const current = board('ws-a', existingTasks, {
    total_count: 120,
    has_more: true,
    limit: 15,
    next_cursor: 'older-cursor',
  })
  const payload = board('ws-a', incomingTasks, {
    total_count: 120,
    has_more: true,
    limit: 100,
    next_cursor: 'poll-cursor',
  })

  const merged = applyBoardPayloadState('ws-a', 'ws-a', current, payload, false, true)

  assert.equal(merged.tasks.length, 105)
  assert.equal(merged.tasks_pagination.next_cursor, 'older-cursor')
  assert.equal(merged.tasks_pagination.has_more, true)
})

test('mergeBoardPollReports replaces refreshed task reports by task_id', () => {
  const existing = [
    { id: 'old-r1', task_id: 't1', message: 'stale' },
    { id: 'old-r2', task_id: 't2', message: 'keep' },
  ]
  const incoming = [{ id: 'new-r1', task_id: 't1', message: 'fresh' }]
  const merged = mergeBoardPollReports(existing, incoming, [{ id: 't1' }, { id: 't2' }])
  assert.equal(merged.length, 2)
  assert.deepEqual(
    merged.map(report => report.id).sort(),
    ['new-r1', 'old-r2'],
  )
  assert.equal(merged.find(report => report.task_id === 't1')?.message, 'fresh')
})

test('A→B→A reset refuses coalesce and stale ws-a generation rejects late payload', () => {
  let wsAGeneration = 0
  let wsBGeneration = 0
  const inFlightPollGeneration = wsAGeneration

  assert.equal(shouldCoalesceBoardFetch(false, true), true)

  let activeWorkspace = 'ws-b'
  wsBGeneration = nextBoardFetchGeneration(wsBGeneration, true)
  assert.equal(shouldCoalesceBoardFetch(true, true), false)
  assert.deepEqual(
    applyBoardPayloadState(activeWorkspace, 'ws-a', board('ws-b', [{ id: 'b1' }]), board('ws-a', [{ id: 'stale-a' }]), false),
    board('ws-b', [{ id: 'b1' }]),
  )

  activeWorkspace = 'ws-a'
  wsAGeneration = nextBoardFetchGeneration(wsAGeneration, true)
  assert.equal(shouldCoalesceBoardFetch(true, true), false)
  assert.equal(isStaleBoardGeneration(wsAGeneration, inFlightPollGeneration), true)

  const currentBoardA = board('ws-a', [{ id: 'fresh-a' }])
  const staleBoardA = board('ws-a', [{ id: 'stale-a' }])
  const kept = isStaleBoardGeneration(wsAGeneration, inFlightPollGeneration)
    ? currentBoardA
    : applyBoardPayloadState(activeWorkspace, 'ws-a', currentBoardA, staleBoardA, false)

  assert.deepEqual(kept.tasks, [{ id: 'fresh-a' }])
  assert.equal(wsBGeneration, 1)
  assert.equal(wsAGeneration, 1)
})

test('mergeBoardReports merges by report id', () => {
  const merged = mergeBoardReports(
    [{ id: 'r1', task_id: 'a' }],
    [{ id: 'r2', task_id: 'b' }, { id: 'r1', task_id: 'a', message: 'updated' }],
  )
  assert.equal(merged.length, 2)
  assert.equal(merged.find(report => report.id === 'r1')?.message, 'updated')
})

test('boardOlderRemainingCount uses done total minus loaded done tasks', () => {
  assert.equal(
    boardOlderRemainingCount(
      [
        { id: '1', status: 'todo' },
        { id: '2', status: 'done' },
        { id: '3', status: 'done' },
      ],
      { total_count: 20, has_more: true, limit: 15 },
    ),
    18,
  )
})

test('loadedDoneTaskCount counts only done tasks', () => {
  assert.equal(
    loadedDoneTaskCount([
      { id: '1', status: 'todo' },
      { id: '2', status: 'done' },
    ]),
    1,
  )
})

test('boardTasksLimitForPoll uses loaded done window', () => {
  assert.equal(boardTasksLimitForPoll(15), 15)
  assert.equal(boardTasksLimitForPoll(30), 30)
})

test('shouldAcceptBoardPayload rejects inactive workspace payloads', () => {
  assert.equal(shouldAcceptBoardPayload('ws-b', 'ws-a'), false)
  assert.equal(shouldAcceptBoardPayload('ws-b', 'ws-b'), true)
  assert.equal(shouldAcceptBoardPayload(null, 'ws-a'), false)
})

test('isStaleBoardGeneration ignores superseded fetch generations', () => {
  assert.equal(isStaleBoardGeneration(2, 1), true)
  assert.equal(isStaleBoardGeneration(2, 2), false)
  assert.equal(isStaleBoardGeneration(undefined, 0), false)
})

test('applyBoardPayloadState keeps workspace B when A response arrives late', () => {
  const boardB = board('ws-b', [{ id: 'b1' }], { total_count: 1, has_more: false, limit: 15 })
  const payloadA = board('ws-a', [{ id: 'a1', title: 'invader' }], {
    total_count: 99,
    has_more: true,
    limit: 15,
  })

  const result = applyBoardPayloadState('ws-b', 'ws-a', boardB, payloadA, false)

  assert.deepEqual(result, boardB)
  assert.equal(result.tasks[0].id, 'b1')
  assert.notEqual(result.tasks[0].id, 'a1')
})

test('applyBoardPayloadState ignores stale same-workspace generation via caller guard', () => {
  const current = board('ws-a', [{ id: 'fresh' }])
  const stalePayload = board('ws-a', [{ id: 'stale' }])

  assert.equal(isStaleBoardGeneration(2, 1), true)
  const kept = isStaleBoardGeneration(2, 1)
    ? current
    : applyBoardPayloadState('ws-a', 'ws-a', current, stalePayload, false)

  assert.deepEqual(kept.tasks, [{ id: 'fresh' }])
})

test('applyBoardPayloadState reset fetch drops expanded done tail ghost after delete', () => {
  const expanded = board(
    'ws-a',
    Array.from({ length: 20 }, (_, index) => ({ id: `t${index}`, status: 'done' })),
    { total_count: 20, has_more: true, limit: 15, next_cursor: 'older' },
  )
  const refreshed = board(
    'ws-a',
    Array.from({ length: 15 }, (_, index) => ({ id: `t${index}`, status: 'done' })),
    { total_count: 19, has_more: true, limit: 15, next_cursor: 'fresh' },
  )

  const merged = applyBoardPayloadState('ws-a', 'ws-a', expanded, refreshed, false)

  assert.equal(merged.tasks.length, 15)
  assert.deepEqual(taskIds(merged.tasks), Array.from({ length: 15 }, (_, index) => `t${index}`))
  assert.ok(!taskIds(merged.tasks).includes('t19'))
})

test('runBoardLoadMoreAttempt sets error on failure and clears loading', async () => {
  const state = { loading: false, error: null }
  await assert.rejects(
    () =>
      runBoardLoadMoreAttempt(
        state,
        async () => {
          throw new Error('network down')
        },
        () => {},
      ),
    /network down/,
  )
  assert.equal(state.loading, false)
  assert.equal(state.error, 'network down')
})

test('runBoardLoadMoreAttempt retry clears error and appends page', async () => {
  const state = { loading: false, error: null }
  let attempt = 0
  const mergedTasks = [{ id: '1' }, { id: '2' }]

  await assert.rejects(
    () =>
      runBoardLoadMoreAttempt(
        state,
        async () => {
          attempt += 1
          if (attempt === 1) {
            throw new Error('temporary failure')
          }
          return [{ id: '3' }]
        },
        page => {
          mergedTasks.push(...page)
        },
      ),
    /temporary failure/,
  )
  assert.equal(state.error, 'temporary failure')
  assert.equal(state.loading, false)

  await runBoardLoadMoreAttempt(
    state,
    async () => [{ id: '3' }],
    page => {
      mergedTasks.push(...page)
    },
  )
  assert.equal(state.error, null)
  assert.equal(state.loading, false)
  assert.deepEqual(mergedTasks.map(task => task.id), ['1', '2', '3'])
})

test('applyBoardPayloadState appends load-more done page for active workspace', () => {
  const current = board(
    'ws-a',
    [
      { id: 'open', status: 'todo' },
      { id: '1', status: 'done' },
      { id: '2', status: 'done' },
    ],
    {
      total_count: 3,
      has_more: true,
      limit: 15,
      next_cursor: 'cursor',
    },
  )
  const olderPage = board('ws-a', [{ id: '3', status: 'done' }], {
    total_count: 3,
    has_more: false,
    limit: 15,
  })

  const merged = applyBoardPayloadState('ws-a', 'ws-a', current, olderPage, true)

  assert.deepEqual(merged.tasks.map(task => task.id), ['open', '1', '2', '3'])
  assert.equal(merged.tasks_pagination.has_more, false)
})

const viewSource = await readFile(
  new URL('../src/components/AgentWorkspaceView.vue', import.meta.url),
  'utf8',
)

test('AgentWorkspaceView exposes load-more control for older done tasks', () => {
  assert.match(viewSource, /board-history-load-more/)
  assert.match(viewSource, /Show older done/)
  assert.match(viewSource, /handleLoadOlderTasks/)
})

test('AgentWorkspaceView column counts prefer server status totals', () => {
  assert.match(viewSource, /taskCountByStatus/)
  assert.match(viewSource, /status_counts/)
})

const storeSource = await readFile(
  new URL('../src/stores/workspaceStore.ts', import.meta.url),
  'utf8',
)

test('workspaceStore fetchBoard uses tasks_limit query param', () => {
  assert.match(storeSource, /tasks_limit/)
  assert.match(storeSource, /loadMoreBoardTasks/)
  assert.match(storeSource, /boardFetchGeneration/)
})

test('workspaceStore reset supersedes in-flight fetch instead of coalescing', () => {
  assert.match(storeSource, /shouldCoalesceBoardFetch/)
  assert.match(storeSource, /boardFetches\.get\(workspaceId\) === request/)
})

test('workspaceStore delegates payload acceptance to boardPagination helpers', () => {
  assert.match(storeSource, /applyBoardPayloadState/)
  assert.match(storeSource, /isStaleBoardGeneration/)
})

test('workspaceStore deleteTask refreshes board with reset', () => {
  assert.match(storeSource, /async function deleteTask[\s\S]*fetchBoard\([^)]*\{\s*reset:\s*true\s*\}/)
})

test('workspaceStore load-more exposes error retry state', () => {
  assert.match(storeSource, /runBoardLoadMoreAttempt/)
  assert.match(storeSource, /boardLoadMoreError/)
  assert.match(viewSource, /board-history-retry/)
})
