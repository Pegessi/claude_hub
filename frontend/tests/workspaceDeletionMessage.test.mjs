import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature } from './workspaceFeatureTestHarness.mjs'

const {
  workspaceDeleteConfirmMessage,
  boardHasOpenTasks,
  boardHasLiveSessions,
} = await loadWorkspaceFeature('presentation/workspaceDeletion.ts')

const STOP = 'Any sessions running in this workspace will be stopped.'

function task(id, status) {
  return { id, status }
}

function session(id, runtime_status) {
  return { id, runtime_status }
}

test('non-active workspaces always get the stop clause but no resource detail', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-2', name: 'Stale worktrees' },
    { isActive: false, hasOpenTasks: true, hasLiveSessions: true }
  )
  assert.equal(
    message,
    `Delete workspace "Stale worktrees"? ${STOP} This cannot be undone.`
  )
})

test('the active workspace without open resources gets the base stop wording', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, hasOpenTasks: false, hasLiveSessions: false }
  )
  assert.equal(message, `Delete workspace "Main"? ${STOP} This cannot be undone.`)
})

test('only open tasks produce a qualitative task warning with no count', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, hasOpenTasks: true, hasLiveSessions: false }
  )
  assert.equal(
    message,
    `Delete workspace "Main"? ${STOP} This cannot be undone. This workspace still has open tasks.`
  )
  assert.doesNotMatch(message, /\d+ open tasks?/)
})

test('only live sessions produce a qualitative session warning with no count', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, hasOpenTasks: false, hasLiveSessions: true }
  )
  assert.equal(
    message,
    `Delete workspace "Main"? ${STOP} This cannot be undone. This workspace still has live sessions.`
  )
})

test('open tasks and live sessions are both named, still without exact counts', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, hasOpenTasks: true, hasLiveSessions: true }
  )
  assert.equal(
    message,
    `Delete workspace "Main"? ${STOP} This cannot be undone. This workspace still has open tasks and live sessions.`
  )
})

test('blank names fall back to "this workspace" and default context is safe', () => {
  assert.equal(
    workspaceDeleteConfirmMessage({ id: 'ws-9', name: '   ' }, { isActive: true }),
    `Delete workspace "this workspace"? ${STOP} This cannot be undone.`
  )
  assert.equal(
    workspaceDeleteConfirmMessage({ id: 'ws-9', name: '' }),
    `Delete workspace "this workspace"? ${STOP} This cannot be undone.`
  )
})

test('boardHasOpenTasks treats done/failed as closed and all other statuses as open', () => {
  assert.equal(boardHasOpenTasks([task('a', 'done'), task('b', 'failed')]), false)
  assert.equal(boardHasOpenTasks([]), false)
  for (const status of ['todo', 'queued', 'working', 'review']) {
    assert.equal(boardHasOpenTasks([task('a', 'done'), task('b', status)]), true, status)
  }
})

test('boardHasOpenTasks uses authoritative status_counts when present, covering unloaded pages', () => {
  // The loaded page only carries done tasks, but the counts reveal open work
  // on later pages: the warning must still appear.
  assert.equal(
    boardHasOpenTasks([task('a', 'done')], { done: 14, failed: 1, review: 1 }),
    true
  )
  assert.equal(
    boardHasOpenTasks([task('a', 'done')], { done: 20, failed: 2 }),
    false
  )
  assert.equal(boardHasOpenTasks([], { done: 0 }), false)
  // Null/absent counts fall back to scanning.
  assert.equal(boardHasOpenTasks([task('a', 'queued')], null), true)
})

test('boardHasLiveSessions treats only offline as stopped', () => {
  assert.equal(boardHasLiveSessions([session('o1', 'offline')]), false)
  assert.equal(boardHasLiveSessions([]), false)
  for (const status of ['working', 'idle', 'starting']) {
    assert.equal(boardHasLiveSessions([session('o1', 'offline'), session('l1', status)]), true, status)
  }
})
