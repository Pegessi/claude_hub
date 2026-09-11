import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/chatGroups.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { groupChatsByCwd, cwdLabel } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

function makeTab(overrides) {
  return {
    id: 'tab',
    name: 'Untitled',
    cwd: null,
    created_at: null,
    session_kind: 'chat',
    ...overrides,
  }
}

test('groupChatsByCwd groups tabs by their working directory', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'a', cwd: '/home/user/project-a' }),
    makeTab({ id: 'b', cwd: '/home/user/project-b' }),
    makeTab({ id: 'c', cwd: '/home/user/project-a' }),
  ])
  assert.equal(groups.length, 2)
  const projectA = groups.find(g => g.cwd === '/home/user/project-a')
  assert.equal(projectA.tabs.length, 2)
  assert.deepEqual(projectA.tabs.map(t => t.id), ['a', 'c'])
})

test('groupChatsByCwd sorts tabs within a group newest-first', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'old', cwd: '/repo', created_at: '2026-01-01T00:00:00Z' }),
    makeTab({ id: 'new', cwd: '/repo', created_at: '2026-06-01T00:00:00Z' }),
  ])
  assert.deepEqual(groups[0].tabs.map(t => t.id), ['new', 'old'])
})

test('groupChatsByCwd sorts groups by their most recent tab', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'a', cwd: '/old-repo', created_at: '2026-01-01T00:00:00Z' }),
    makeTab({ id: 'b', cwd: '/new-repo', created_at: '2026-09-01T00:00:00Z' }),
  ])
  assert.deepEqual(groups.map(g => g.cwd), ['/new-repo', '/old-repo'])
})

test('groupChatsByCwd buckets tabs without a cwd into the "No directory" sentinel', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'a', cwd: null }),
    makeTab({ id: 'b', cwd: '' }),
  ])
  const noDir = groups.filter(g => g.cwd === 'No directory')
  assert.equal(noDir.length, 1)
  assert.equal(noDir[0].tabs.length, 2)
})

test('cwdLabel returns the last path segment', () => {
  assert.equal(cwdLabel('/home/user/project-a'), 'project-a')
  assert.equal(cwdLabel('/repo'), 'repo')
})

test('cwdLabel strips trailing slashes before taking the last segment', () => {
  assert.equal(cwdLabel('/home/user/project-a/'), 'project-a')
})

test('cwdLabel returns the sentinel for empty or missing directories', () => {
  assert.equal(cwdLabel(''), 'No directory')
  assert.equal(cwdLabel('No directory'), 'No directory')
})

test('cwdLabel returns the whole path when there is no separator', () => {
  assert.equal(cwdLabel('repo'), 'repo')
})
