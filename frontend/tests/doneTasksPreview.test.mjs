import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/doneTasksPreview.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(helperSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const helperModule = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  DONE_TASKS_PREVIEW_LIMIT,
  previewDoneTasks,
  hiddenDoneTaskCount,
  sortDoneTasksByRecency,
  parseTimestampMs,
} = helperModule

function doneTask(overrides = {}) {
  return {
    id: overrides.id ?? 't',
    status: 'done',
    completed_at: overrides.completed_at ?? null,
    created_at: overrides.created_at ?? '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

test('DONE_TASKS_PREVIEW_LIMIT is 15', () => {
  assert.equal(DONE_TASKS_PREVIEW_LIMIT, 15)
})

test('parseTimestampMs returns null for empty values', () => {
  assert.equal(parseTimestampMs(null), null)
  assert.equal(parseTimestampMs(undefined), null)
  assert.equal(parseTimestampMs(''), null)
})

test('parseTimestampMs parses ISO timestamps', () => {
  assert.equal(parseTimestampMs('2026-01-01T00:00:00Z'), Date.parse('2026-01-01T00:00:00Z'))
})

test('sortDoneTasksByRecency sorts by completed_at descending', () => {
  const tasks = [
    doneTask({ id: 'a', completed_at: '2026-01-01T00:00:00Z' }),
    doneTask({ id: 'c', completed_at: '2026-01-03T00:00:00Z' }),
    doneTask({ id: 'b', completed_at: '2026-01-02T00:00:00Z' }),
  ]
  const sorted = sortDoneTasksByRecency(tasks)
  assert.deepEqual(
    sorted.map((t) => t.id),
    ['c', 'b', 'a'],
  )
})

test('sortDoneTasksByRecency falls back to created_at when completed_at is absent', () => {
  const tasks = [
    doneTask({ id: 'a', completed_at: null, created_at: '2026-01-01T00:00:00Z' }),
    doneTask({ id: 'b', completed_at: null, created_at: '2026-01-02T00:00:00Z' }),
  ]
  const sorted = sortDoneTasksByRecency(tasks)
  assert.deepEqual(sorted.map((t) => t.id), ['b', 'a'])
})

test('previewDoneTasks returns all tasks when showAll is true', () => {
  const tasks = Array.from({ length: 30 }, (_, i) =>
    doneTask({ id: String(i), completed_at: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` }),
  )
  const result = previewDoneTasks(tasks, true)
  assert.equal(result.length, 30)
})

test('previewDoneTasks returns at most DONE_TASKS_PREVIEW_LIMIT tasks when showAll is false', () => {
  const tasks = Array.from({ length: 30 }, (_, i) =>
    doneTask({ id: String(i), completed_at: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` }),
  )
  const result = previewDoneTasks(tasks, false)
  assert.equal(result.length, DONE_TASKS_PREVIEW_LIMIT)
})

test('previewDoneTasks boundary: exactly 15 tasks returns all 15 when showAll is false', () => {
  const tasks = Array.from({ length: 15 }, (_, i) =>
    doneTask({ id: String(i), completed_at: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` }),
  )
  const result = previewDoneTasks(tasks, false)
  assert.equal(result.length, 15)
})

test('previewDoneTasks boundary: 16 tasks returns 15 when showAll is false', () => {
  const tasks = Array.from({ length: 16 }, (_, i) =>
    doneTask({ id: String(i), completed_at: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` }),
  )
  const result = previewDoneTasks(tasks, false)
  assert.equal(result.length, 15)
})

test('previewDoneTasks returns the most recent 15 tasks', () => {
  const tasks = Array.from({ length: 20 }, (_, i) =>
    doneTask({ id: String(i), completed_at: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` }),
  )
  const result = previewDoneTasks(tasks, false)
  // Most recent first: ids 19 down to 5 (20 - 15 = 5)
  const expectedIds = Array.from({ length: 15 }, (_, i) => String(19 - i))
  assert.deepEqual(
    result.map((t) => t.id),
    expectedIds,
  )
})

test('hiddenDoneTaskCount is 0 when showAll is true', () => {
  assert.equal(hiddenDoneTaskCount(100, true), 0)
})

test('hiddenDoneTaskCount is 0 when total <= limit', () => {
  assert.equal(hiddenDoneTaskCount(15, false), 0)
  assert.equal(hiddenDoneTaskCount(0, false), 0)
})

test('hiddenDoneTaskCount returns total - limit when total > limit', () => {
  assert.equal(hiddenDoneTaskCount(16, false), 1)
  assert.equal(hiddenDoneTaskCount(100, false), 85)
})
