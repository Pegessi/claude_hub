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
const { groupChatsByCwd, cwdLabel, buildChatSidebarGroups, moveTabById, canDropChatInGroup, parsePinnedChatIds } = await import(
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

test('groupChatsByCwd preserves persisted tab order even when timestamps disagree', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'old', cwd: '/repo', created_at: '2026-01-01T00:00:00Z' }),
    makeTab({ id: 'new', cwd: '/repo', created_at: '2026-06-01T00:00:00Z' }),
  ])
  assert.deepEqual(groups[0].tabs.map(t => t.id), ['old', 'new'])
})

test('groupChatsByCwd preserves first directory appearance', () => {
  const groups = groupChatsByCwd([
    makeTab({ id: 'a', cwd: '/old-repo', created_at: '2026-01-01T00:00:00Z' }),
    makeTab({ id: 'b', cwd: '/new-repo', created_at: '2026-09-01T00:00:00Z' }),
  ])
  assert.deepEqual(groups.map(g => g.cwd), ['/old-repo', '/new-repo'])
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

const sidebarOptions = (overrides = {}) => ({
  query: '', activeTabId: null, collapsed: new Set(), expanded: new Set(), ...overrides,
})
const sessions = (count, cwd = '/repo') => Array.from({ length: count }, (_, index) =>
  makeTab({ id: `tab-${index}`, name: `Session ${index}`, cwd }))
const ids = tabs => tabs.map(tab => tab.id)

test('directories preview exactly six sessions; expanding and folding preserve full counts', () => {
  const tabs = sessions(9)
  const preview = buildChatSidebarGroups(tabs, new Set(), sidebarOptions())[0]
  assert.equal(preview.tabs.length, 9)
  assert.deepEqual(ids(preview.visibleTabs), ids(tabs.slice(0, 6)))
  assert.equal(preview.hiddenCount, 3)
  const expanded = buildChatSidebarGroups(tabs, new Set(), sidebarOptions({ expanded: new Set(['/repo']) }))[0]
  assert.deepEqual(ids(expanded.visibleTabs), ids(tabs))
  assert.equal(expanded.hiddenCount, 0)
  const folded = buildChatSidebarGroups(tabs, new Set(), sidebarOptions({ collapsed: new Set(['/repo']) }))[0]
  assert.deepEqual(folded.visibleTabs, [])
  assert.equal(folded.hiddenCount, 9)
})

test('active session beyond the preview replaces the sixth row and remains visible in a folded directory', () => {
  const tabs = sessions(9)
  const options = sidebarOptions({ activeTabId: 'tab-8' })
  const preview = buildChatSidebarGroups(tabs, new Set(), options)[0]
  assert.deepEqual(ids(preview.visibleTabs), ['tab-0', 'tab-1', 'tab-2', 'tab-3', 'tab-4', 'tab-8'])
  assert.equal(preview.hiddenCount, 3)
  const folded = buildChatSidebarGroups(tabs, new Set(), { ...options, collapsed: new Set(['/repo']) })[0]
  assert.deepEqual(ids(folded.visibleTabs), ['tab-8'])
  assert.equal(folded.collapsed, true)
})

test('pinned sessions are always expanded, excluded from normal groups, and keep persisted order', () => {
  const tabs = sessions(12)
  const pins = new Set(ids(tabs.slice(2, 10)).reverse())
  const groups = buildChatSidebarGroups(tabs, pins, sidebarOptions({ collapsed: new Set(['/repo']) }))
  assert.equal(groups[0].pinned, true)
  assert.deepEqual(ids(groups[0].visibleTabs), ids(tabs.slice(2, 10)))
  assert.equal(groups[0].hiddenCount, 0)
  assert.deepEqual(ids(groups[1].tabs), ['tab-0', 'tab-1', 'tab-10', 'tab-11'])
})

test('search reaches hidden and folded sessions by name and full directory path', () => {
  const tabs = sessions(12, '/work/deep/repo')
  const options = sidebarOptions({ query: 'SESSION 11', collapsed: new Set(['/work/deep/repo']) })
  assert.deepEqual(ids(buildChatSidebarGroups(tabs, new Set(), options)[0].visibleTabs), ['tab-11'])
  const all = buildChatSidebarGroups(tabs, new Set(), { ...options, query: ' /WORK/DEEP ' })[0]
  assert.equal(all.visibleTabs.length, 12)
  assert.equal(all.collapsed, false)
  assert.equal(buildChatSidebarGroups(tabs, new Set(), { ...options, query: 'missing' }).length, 0)
})

test('stale pinned IDs do not create empty sections and matching pinned search has no duplicates', () => {
  const tabs = sessions(3)
  assert.equal(buildChatSidebarGroups(tabs, new Set(['archived']), sidebarOptions()).length, 1)
  const found = buildChatSidebarGroups(tabs, new Set(['tab-2']), sidebarOptions({ query: 'Session 2' }))
  assert.equal(found.length, 1)
  assert.equal(found[0].pinned, true)
  assert.deepEqual(ids(found[0].visibleTabs), ['tab-2'])
})

test('moveTabById respects before and after in either direction without mutating the input', () => {
  const tabs = sessions(4)
  assert.deepEqual(ids(moveTabById(tabs, 'tab-0', 'tab-2', 'before')), ['tab-1', 'tab-0', 'tab-2', 'tab-3'])
  assert.deepEqual(ids(moveTabById(tabs, 'tab-0', 'tab-2', 'after')), ['tab-1', 'tab-2', 'tab-0', 'tab-3'])
  assert.deepEqual(ids(moveTabById(tabs, 'tab-3', 'tab-1', 'before')), ['tab-0', 'tab-3', 'tab-1', 'tab-2'])
  assert.deepEqual(ids(moveTabById(tabs, 'tab-3', 'tab-1', 'after')), ['tab-0', 'tab-1', 'tab-3', 'tab-2'])
  assert.deepEqual(ids(tabs), ['tab-0', 'tab-1', 'tab-2', 'tab-3'])
})

test('moveTabById ignores deleted sources, deleted targets, self drops, and unchanged positions', () => {
  const tabs = sessions(3)
  for (const [source, target, position] of [
    ['missing', 'tab-1', 'before'], ['tab-1', 'missing', 'after'],
    ['tab-1', 'tab-1', 'after'], ['tab-0', 'tab-1', 'before'],
  ]) assert.equal(moveTabById(tabs, source, target, position), tabs)
})

test('filtered drag IDs retain unrelated sessions and resolve against concurrently inserted rows', () => {
  const tabs = [makeTab({ id: 'a', cwd: '/one' }), makeTab({ id: 'new', cwd: '/other' }), makeTab({ id: 'b', cwd: '/one' })]
  const moved = moveTabById(tabs, 'b', 'a', 'before')
  assert.deepEqual(ids(moved), ['b', 'a', 'new'])
  assert.equal(moved.find(tab => tab.id === 'new'), tabs[1])
  assert.equal(moved[0].cwd, '/one')
})

test('directory drops preserve cwd while pinned groups accept sessions from any directory', () => {
  const tab = makeTab({ cwd: '/work/a' })
  assert.equal(canDropChatInGroup(tab, { pinned: false, cwd: '/work/a' }), true)
  assert.equal(canDropChatInGroup(tab, { pinned: false, cwd: '/work/b' }), false)
  assert.equal(canDropChatInGroup(tab, { pinned: true, cwd: '' }), true)
  assert.equal(canDropChatInGroup({ ...tab, workspace_id: 'managed' }, { pinned: true, cwd: '' }), false)
  assert.equal(canDropChatInGroup({ ...tab, session_kind: 'terminal' }, { pinned: true, cwd: '' }), false)
  assert.equal(canDropChatInGroup(makeTab({}), { pinned: false, cwd: 'No directory' }), true)
})

test('pin preference parsing tolerates malformed storage and accepts only unique nonempty IDs', () => {
  for (const raw of [null, '', '{broken', 'null', '{}', '42']) assert.deepEqual([...parsePinnedChatIds(raw)], [])
  assert.deepEqual([...parsePinnedChatIds('["a",3,null,"","a","b"]')], ['a', 'b'])
})
