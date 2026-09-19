import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/timelineWindow.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { TIMELINE_PAGE_SIZE, selectTimelineWindow, timelineWindowStart } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('a long conversation initially renders only the latest page with absolute ordinals', () => {
  const turns = Array.from({ length: 10_000 }, (_, key) => ({ key }))
  const selected = selectTimelineWindow(turns, timelineWindowStart(turns.length, null), () => false)
  assert.equal(selected.length, TIMELINE_PAGE_SIZE)
  assert.equal(selected[0].ordinal, 10_000 - TIMELINE_PAGE_SIZE)
  assert.equal(selected.at(-1).ordinal, 9_999)
  assert.equal(selected[0].turn, turns[10_000 - TIMELINE_PAGE_SIZE])
  assert.equal(turns.length, 10_000, 'durable history remains complete')
})

test('old pending approval/editor rows remain accessible without duplicating tail rows', () => {
  const turns = Array.from({ length: 100 }, (_, key) => ({ key, retained: [2, 5, 99].includes(key) }))
  const selected = selectTimelineWindow(turns, 60, turn => turn.retained)
  assert.deepEqual(selected.map(row => row.ordinal), [2, 5, ...Array.from({ length: 40 }, (_, i) => 60 + i)])
})

test('detached and expanded windows do not move their start as new turns arrive', () => {
  assert.equal(timelineWindowStart(100, 60), 60)
  assert.equal(timelineWindowStart(101, 60), 60)
  assert.equal(timelineWindowStart(101, 20), 20)
  assert.equal(timelineWindowStart(101, null), 61, 'a fresh visit resets to the latest page')
})

test('empty, short, and truncated histories stay in range', () => {
  assert.equal(timelineWindowStart(0, null), 0)
  assert.equal(timelineWindowStart(12, null), 0)
  assert.equal(timelineWindowStart(3, 80), 2)
})
