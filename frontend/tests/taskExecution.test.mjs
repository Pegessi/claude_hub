import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/taskExecution.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const helper = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    clear: () => values.clear(),
    key: index => [...values.keys()][index] ?? null,
    get length() { return values.size },
  }
}

test('legacy tasks default to workspace control without consulting source', () => {
  assert.equal(helper.executionControlForTask({}), 'workspace')
  assert.equal(helper.isWorkspaceControlledTask({ source: { kind: 'chat' } }), true)
  assert.equal(helper.isWorkspaceControlledTask({ execution_control: 'initiator', source: { kind: 'human' } }), false)
})

test('source labels are display-only and completed is labelled Done', () => {
  assert.equal(helper.taskSourceLabel({ kind: 'human', tab_id: null, agent_id: null }), 'Created manually')
  assert.equal(helper.taskSourceLabel({ kind: 'chat', tab_id: 'tab-1', agent_id: null }), 'From Chat')
  assert.equal(helper.taskSourceLabel({ kind: 'agent', tab_id: null, agent_id: 'agent-1' }), 'From Agent')
  assert.equal(helper.progressStateLabel('completed'), 'Done')
  assert.notEqual(helper.progressStateLabel('completed'), 'Human accepted')
})

test('manual progress excludes released even when capability advertises it', () => {
  assert.deepEqual(helper.manualProgressStates({
    progress_states: ['started', 'working', 'blocked', 'needs_input', 'completed', 'failed', 'released'],
  }), ['started', 'working', 'blocked', 'needs_input', 'completed', 'failed'])
  assert.deepEqual(helper.manualProgressStates(null), [])
})

test('reporter keys contain at least 32 random characters', () => {
  const first = helper.generateTaskReporterKey()
  const second = helper.generateTaskReporterKey()
  assert.match(first, /^[0-9a-f]{64}$/)
  assert.match(second, /^[0-9a-f]{64}$/)
  assert.notEqual(first, second)
})

test('reporter credentials stay session-scoped and isolate origin workspace task epoch and source', () => {
  const storage = memoryStorage()
  const base = { id: 'task-1', execution_epoch: 1, source: { kind: 'chat', tab_id: 'tab-1', agent_id: null } }
  helper.saveTaskReporterKey('ws-1', base, 'a'.repeat(64), storage)
  assert.equal(helper.loadTaskReporterKey('ws-1', base, storage), 'a'.repeat(64))
  assert.equal(helper.loadTaskReporterKey('ws-2', base, storage), null)
  assert.equal(helper.loadTaskReporterKey('ws-1', { ...base, execution_epoch: 2 }, storage), null)
  assert.equal(helper.loadTaskReporterKey('ws-1', { ...base, source: { ...base.source, tab_id: 'tab-2' } }, storage), null)
  helper.removeTaskReporterKey('ws-1', base, storage)
  assert.equal(helper.loadTaskReporterKey('ws-1', base, storage), null)
})

test('short reporter credentials are rejected before storage', () => {
  assert.throws(
    () => helper.saveTaskReporterKey('ws-1', { id: 'task-1', execution_epoch: 1, source: null }, 'short', memoryStorage()),
    /at least 32/,
  )
})

test('create attempts persist the exact payload and both initiator keys', () => {
  const storage = memoryStorage()
  const attempt = { workspace_id:'ws-1', payload:{ title:'T', prompt:'P', execution_control:'initiator', request_key:'request-1', reporter_key:'r'.repeat(64) } }
  helper.saveTaskCreateAttempt(attempt, storage)
  assert.deepEqual(helper.loadTaskCreateAttempt('ws-1', storage), attempt)
  assert.equal(helper.loadTaskCreateAttempt('ws-2', storage), null)
  helper.removeTaskCreateAttempt('ws-1', { storage, expectedRequestKey: attempt.payload.request_key })
  assert.equal(helper.loadTaskCreateAttempt('ws-1', storage), null)
})
test('create attempts without request_key are rejected before POST', () => {
  assert.throws(() => helper.saveTaskCreateAttempt({ workspace_id:'ws-1', payload:{ title:'T', prompt:'P' } }, memoryStorage()), /Invalid Task create attempt/)
})


test('all pending removals preserve a newer request in the same slot', () => {
  const storage = memoryStorage()
  const source = { kind: 'human', tab_id: null, agent_id: null }
  const create = key => ({ workspace_id: 'ws-cas', payload: {
    title: 'T', prompt: 'P', execution_control: 'workspace', request_key: key,
  } })
  helper.saveTaskCreateAttempt(create('first'), storage)
  helper.saveTaskCreateAttempt(create('second'), storage)
  assert.equal(helper.removeTaskCreateAttempt('ws-cas', { storage, expectedRequestKey: 'first' }), false)
  assert.equal(helper.loadTaskCreateAttempt('ws-cas', storage).payload.request_key, 'second')
  assert.equal(helper.removeTaskCreateAttempt('ws-cas', { storage, expectedRequestKey: 'second' }), true)

  for (const kind of ['Handoff', 'Progress']) {
    const attempt = callId => ({
      workspace_id: 'ws-cas', task_id: 'task-cas', source,
      request: kind === 'Handoff'
        ? { call_id: callId, expected_execution_epoch: 1, expected_progress_revision: 0,
            execution_control: 'initiator', new_reporter_key: 'r'.repeat(64) }
        : { call_id: callId, expected_execution_epoch: 1, expected_progress_revision: 0,
            state: 'working', summary: 'one update', artifact_refs: [] },
    })
    helper[`saveTask${kind}Attempt`](attempt('first'), storage)
    helper[`saveTask${kind}Attempt`](attempt('second'), storage)
    assert.equal(helper[`removeTask${kind}Attempt`](
      'ws-cas', 'task-cas', { storage, expectedCallId: 'first' }), false)
    assert.equal(helper[`loadTask${kind}Attempt`]('ws-cas', 'task-cas', storage).request.call_id, 'second')
    assert.equal(helper[`removeTask${kind}Attempt`](
      'ws-cas', 'task-cas', { storage, expectedCallId: 'second' }), true)
  }
})

test('progress attempts reject invalid shape and restore the exact scoped request', () => {
  const storage = memoryStorage()
  const attempt = {
    workspace_id: 'ws-progress', task_id: 'task-progress',
    request: { call_id: 'call-progress', expected_execution_epoch: 4,
      expected_progress_revision: 7, state: 'completed', summary: 'verified', artifact_refs: ['artifact'] },
  }
  helper.saveTaskProgressAttempt(attempt, storage)
  assert.deepEqual(helper.loadTaskProgressAttempt('ws-progress', 'task-progress', storage), attempt)
  assert.equal(helper.loadTaskProgressAttempt('ws-progress', 'other-task', storage), null)
  assert.equal(helper.loadTaskProgressAttempt('other-ws', 'task-progress', storage), null)
  for (const change of [
    { state: 'released' }, { expected_execution_epoch: 0 },
    { expected_progress_revision: -1 }, { execution_ref: {} },
    { summary: '' }, { artifact_refs: Array(33).fill('x') },
  ]) {
    storage.setItem(helper.taskProgressAttemptStorageKey('ws-progress', 'task-progress'),
      JSON.stringify({ ...attempt, request: { ...attempt.request, ...change } }))
    assert.equal(helper.loadTaskProgressAttempt('ws-progress', 'task-progress', storage), null)
  }
})

test('reporter fallback survives storage failure and clears after a successful write', () => {
  const storage = memoryStorage()
  const denied = { ...storage, setItem() { throw new Error('denied') } }
  const task = { id: 'fallback-task', execution_epoch: 2, source: null }
  const key = 'f'.repeat(64)
  assert.equal(helper.saveTaskReporterKey('ws-fallback', task, key, denied), false)
  assert.equal(helper.loadTaskReporterKey('ws-fallback', task, denied), key)
  assert.equal(helper.saveTaskReporterKey('ws-fallback', task, key, storage), true)
  storage.removeItem(helper.taskReporterStorageKey('ws-fallback', task))
  assert.equal(helper.loadTaskReporterKey('ws-fallback', task, storage), null)
})

test('unavailable sessionStorage getter stays inside safe error boundaries', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const task = { id: 'getter-task', execution_epoch: 2, source: null }
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true, get() { throw new Error('sensitive storage exception') },
  })
  try {
    assert.equal(helper.loadTaskReporterKey('ws-getter', task), null)
    assert.equal(helper.saveTaskReporterKey('ws-getter', task, 'g'.repeat(64)), false)
    assert.equal(helper.loadTaskReporterKey('ws-getter', task), 'g'.repeat(64))
    helper.removeTaskReporterKey('ws-getter', task)
    assert.equal(helper.loadTaskReporterKey('ws-getter', task), null)
    assert.throws(() => helper.saveTaskCreateAttempt({ workspace_id: 'ws-getter', payload: {
      title: 'T', prompt: 'P', execution_control: 'workspace', request_key: 'request',
    } }), error => error.message === 'The Task create attempt could not be stored safely')
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor)
    else delete globalThis.sessionStorage
  }
})

test('old reporter credentials never attach to a different workspace or later epoch', () => {
  const attempt = { workspace_id: 'ws-epoch', task_id: 'task-epoch', source: null,
    request: { execution_control: 'initiator', expected_execution_epoch: 2 } }
  const task = { id: 'task-epoch', workspace_id: 'ws-epoch', execution_control: 'initiator', execution_epoch: 3 }
  assert.equal(helper.handoffReporterKeyApplies(attempt, task), true)
  assert.equal(helper.handoffReporterKeyApplies(attempt, { ...task, execution_epoch: 5 }), false)
  assert.equal(helper.handoffReporterKeyApplies(attempt, { ...task, workspace_id: 'other' }), false)
  assert.equal(helper.createReporterKeyApplies('ws-epoch', { ...task, execution_epoch: 1 }), true)
  assert.equal(helper.createReporterKeyApplies('other', { ...task, execution_epoch: 1 }), false)
  assert.equal(helper.createReporterKeyApplies('ws-epoch', task), false)
})
