import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/features/chat/presentation/subagentJump.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const {
  SUBAGENT_JUMP_MAX_CHIPS,
  subagentPollIntervalMs,
  shortSubagentId,
  toSubagentJumpChip,
  buildSubagentJumpChips,
  cleanSubagentSummary,
  relativeSubagentTime,
  subagentDockCounts,
  subagentDisplayName,
  hubSubagentIdFromTool,
} = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

function summary(overrides = {}) {
  return {
    id: 'sa-0123456789ab',
    tab_id: 'tab-1',
    parent_tab_id: 'parent',
    agent_type: 'codex',
    name: 'codex subagent: summarize X',
    keep: false,
    closed: false,
    close_reason: null,
    created_at: '2026-11-03T00:00:00Z',
    turn_count: 1,
    last_turn_id: 'turn-1',
    last_status: 'running',
    last_summary: null,
    ...overrides,
  }
}

function tab(overrides = {}) {
  return { id: 'tab-1', name: 'child', session_kind: 'chat', ...overrides }
}

const liveSets = (extraTabs = []) => ({
  liveTabIds: new Set(['tab-1', ...extraTabs]),
  archivedTabIds: new Set(),
  statusByTabId: {},
})

test('shortSubagentId keeps prefix and stable tail', () => {
  assert.equal(shortSubagentId('sa-0123456789ab'), 'sa-…89ab')
  assert.equal(shortSubagentId('sa-1'), 'sa-1')
})

test('running summary gets a working placeholder and stays jumpable', () => {
  const chip = toSubagentJumpChip(summary(), liveSets())
  assert.equal(chip.state, 'running')
  assert.equal(chip.stateLabel, 'Running')
  assert.equal(chip.summary, 'Working…')
  assert.equal(chip.canJump, true)
  assert.equal(chip.archived, false)
})

test('completed summary exposes the bounded final answer', () => {
  const chip = toSubagentJumpChip(
    summary({
      last_status: 'completed',
      last_summary: 'Done with the analysis.',
    }),
    liveSets(),
  )
  assert.equal(chip.state, 'completed')
  assert.equal(chip.summary, 'Done with the analysis.')
})

test('archived non-kept child reports closed while still jumpable (restore)', () => {
  const chip = toSubagentJumpChip(summary({ closed: true, keep: false }), {
    liveTabIds: new Set(),
    archivedTabIds: new Set(['tab-1']),
    statusByTabId: {},
  })
  assert.equal(chip.state, 'closed')
  assert.equal(chip.archived, true)
  assert.equal(chip.canJump, true)
})

test('kept closed child with a live tab is closed', () => {
  const chip = toSubagentJumpChip(summary({ closed: true, keep: true }), liveSets())
  assert.equal(chip.state, 'closed')
  assert.equal(chip.archived, false)
})

test('finished child missing from the live tab list is treated as closed', () => {
  const chip = toSubagentJumpChip(summary({ last_status: 'completed' }), {
    liveTabIds: new Set(),
    archivedTabIds: new Set(['tab-1']),
    statusByTabId: {},
  })
  assert.equal(chip.state, 'closed')
  assert.equal(chip.archived, true)
  assert.equal(chip.canOpen, true)
})

test('auto-archived child is marked archived even before archivedTabs is loaded', () => {
  const chip = toSubagentJumpChip(summary({ closed: true, last_status: 'completed' }), {
    liveTabIds: new Set(),
    archivedTabIds: new Set(),
    statusByTabId: {},
  })
  assert.equal(chip.state, 'closed')
  assert.equal(chip.archived, true)
  assert.equal(chip.canJump, true)
  assert.equal(chip.canOpen, true)
})

test('running record stays running even before its tab reaches the store', () => {
  const chip = toSubagentJumpChip(summary({ last_status: 'running' }), {
    liveTabIds: new Set(),
    archivedTabIds: new Set(),
    statusByTabId: {},
  })
  assert.equal(chip.state, 'running')
})

test('failure states map to pill states with fallbacks', () => {
  for (const [status, state, fallback] of [
    ['failed', 'failed', 'Turn failed'],
    ['timeout', 'timeout', 'Timed out'],
    ['cancelled', 'cancelled', 'Cancelled'],
  ]) {
    const chip = toSubagentJumpChip(summary({ last_status: status }), liveSets())
    assert.equal(chip.state, state)
    assert.equal(chip.summary, fallback)
  }
})

test('chip without a tab id cannot jump', () => {
  const chip = toSubagentJumpChip(summary({ tab_id: null }), {
    liveTabIds: new Set(),
    archivedTabIds: new Set(),
    statusByTabId: {},
  })
  assert.equal(chip.canJump, false)
})

test('buildSubagentJumpChips caps rows and passes summaries through', () => {
  const rows = Array.from({ length: SUBAGENT_JUMP_MAX_CHIPS + 4 }, (_, index) =>
    summary({
      id: `sa-${String(index).padStart(12, '0')}`,
      tab_id: `tab-${index}`,
      last_status: 'completed',
      // Bounding/whitespace collapse is the backend serializer's contract.
      last_summary: `answer ${index}`,
    }),
  )
  const tabs = rows.map(row => tab({ id: row.tab_id }))
  const chips = buildSubagentJumpChips(rows, tabs, [])
  assert.equal(chips.length, SUBAGENT_JUMP_MAX_CHIPS)
  assert.equal(chips[0].key, rows[0].id)
  assert.equal(chips[0].summary, 'answer 0')
})

test('polling interval is 5 seconds', () => {
  assert.equal(subagentPollIntervalMs(), 5000)
})

test('chips group running as active and the rest as earlier', () => {
  const chips = buildSubagentJumpChips(
    [summary(), summary({ id: 'sa-b', last_status: 'completed' }), summary({ id: 'sa-c', closed: true })],
    [tab()],
    [],
  )
  assert.deepEqual(chips.map(chip => chip.group), ['active', 'earlier', 'earlier'])
  assert.deepEqual(subagentDockCounts(chips), { running: 1, history: 2, total: 3 })
})

test('cleanSubagentSummary flattens markdown to one plain line', () => {
  assert.equal(cleanSubagentSummary('### **Result**\nmore'), 'Result')
  assert.equal(cleanSubagentSummary('\n\n- see [docs](http://x) `now`'), 'see docs now')
  assert.equal(cleanSubagentSummary('---\n'), '')
  assert.equal(cleanSubagentSummary(null), '')
})

test('relativeSubagentTime buckets and tolerates bad dates', () => {
  const now = Date.parse('2026-11-03T12:00:00Z')
  assert.equal(relativeSubagentTime('2026-11-03T11:59:50Z', now), 'just now')
  assert.equal(relativeSubagentTime('2026-11-03T11:55:00Z', now), '5m ago')
  assert.equal(relativeSubagentTime('2026-11-03T09:00:00Z', now), '3h ago')
  assert.equal(relativeSubagentTime('2026-11-01T12:00:00Z', now), '2d ago')
  assert.equal(relativeSubagentTime('nope', now), '')
})

test('hubSubagentIdFromTool needs an agent run command and an id in the output', () => {
  const args = JSON.stringify({ command: 'claude-hub agent run codex --json "x"' })
  assert.equal(hubSubagentIdFromTool(args, '{"subagent_id":"sa-0123456789ab"}'), 'sa-0123456789ab')
  assert.equal(hubSubagentIdFromTool(args, 'finished'), null)
  assert.equal(hubSubagentIdFromTool('{"command":"ls sa-0123456789ab"}', 'sa-0123456789ab'), null)
})

test('subagentDisplayName drops the redundant type prefix', () => {
  assert.equal(subagentDisplayName('codex subagent: analyze parser'), 'analyze parser')
  assert.equal(subagentDisplayName('my helper'), 'my helper')
})
