import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature } from './workspaceFeatureTestHarness.mjs'

const { workspaceDeleteConfirmMessage } = await loadWorkspaceFeature(
  'presentation/workspaceDeletion.ts'
)

function task(id, status) {
  return { id, status }
}

function session(id, runtime_status) {
  return { id, runtime_status }
}

test('non-active workspaces get the generic irreversible wording', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-2', name: 'Stale worktrees' },
    { isActive: false, openTasks: [task('t1', 'working')], sessions: [session('s1', 'working')] }
  )
  assert.equal(message, 'Delete workspace "Stale worktrees"? This cannot be undone.')
})

test('the active workspace without open resources uses the same base wording', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    {
      isActive: true,
      openTasks: [task('d1', 'done'), task('f1', 'failed')],
      sessions: [session('o1', 'offline')],
    }
  )
  assert.equal(message, 'Delete workspace "Main"? This cannot be undone.')
})

test('done and failed tasks plus offline sessions do not inflate the warning', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, openTasks: [task('d1', 'done')], sessions: [session('o1', 'offline')] }
  )
  assert.doesNotMatch(message, /still has/)
})

test('open tasks produce singular and plural wording', () => {
  const one = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    { isActive: true, openTasks: [task('a', 'review')] }
  )
  assert.equal(
    one,
    'Delete workspace "Main"? This cannot be undone. This workspace still has 1 open task; its sessions will be stopped and everything will be permanently deleted.'
  )
  const many = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    {
      isActive: true,
      openTasks: [task('a', 'todo'), task('b', 'queued'), task('c', 'working')],
    }
  )
  assert.match(many, /3 open tasks/)
})

test('live sessions combine with open tasks in one factual warning', () => {
  const message = workspaceDeleteConfirmMessage(
    { id: 'ws-1', name: 'Main' },
    {
      isActive: true,
      openTasks: [task('a', 'working'), task('z', 'done')],
      sessions: [
        session('s1', 'working'),
        session('s2', 'idle'),
        session('o1', 'offline'),
      ],
    }
  )
  assert.equal(
    message,
    'Delete workspace "Main"? This cannot be undone. This workspace still has 1 open task and 2 live sessions; its sessions will be stopped and everything will be permanently deleted.'
  )
})

test('blank names fall back to "this workspace" and missing collections are safe', () => {
  assert.equal(
    workspaceDeleteConfirmMessage({ id: 'ws-9', name: '   ' }, { isActive: true }),
    'Delete workspace "this workspace"? This cannot be undone.'
  )
  assert.equal(
    workspaceDeleteConfirmMessage({ id: 'ws-9', name: '' }),
    'Delete workspace "this workspace"? This cannot be undone.'
  )
})
