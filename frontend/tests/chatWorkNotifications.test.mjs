import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/chatWorkNotifications.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { createWorkResultTracker } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const item = (kind, report = 'report-1') => ({
  id: 'work-1', latest_result: { kind, report_id: report, task_id: 'task-1', created_at: 'now', summary: 'Private source text' },
})

test('quiet progress never notifies; meaningful results dedupe across refresh and reload', () => {
  let saved
  const storage = { getItem: () => saved, setItem: (_key, value) => { saved = value } }
  const track = createWorkResultTracker(storage)
  assert.deepEqual(track([item('progress')]), [])
  assert.equal(saved, undefined)
  assert.equal(track([item('anomaly')]).length, 1)
  assert.deepEqual(track([item('anomaly')]), [])
  assert.deepEqual(createWorkResultTracker(storage)([item('anomaly')]), [])
  assert.equal(track([item('decision')]).length, 1)
  assert.equal(track([item('completed', 'report-2')]).length, 1)
  assert.ok(!saved.includes('Private source text'))
})

test('notification identity storage is bounded and tolerates unavailable browser storage', () => {
  let saved
  const track = createWorkResultTracker({ getItem: () => 'not JSON', setItem: (_key, value) => { saved = value } })
  for (let i = 0; i < 250; i++) track([item('failed', `report-${i}`)])
  assert.equal(JSON.parse(saved).length, 200)
  const unavailable = createWorkResultTracker({ getItem: () => { throw Error('denied') }, setItem: () => { throw Error('denied') } })
  assert.equal(unavailable([item('failed')]).length, 1)
  assert.deepEqual(unavailable([item('failed')]), [])
})

test('cached Chat panes merge shared dedupe state rather than overwriting each other', () => {
  let saved
  const storage = { getItem: () => saved, setItem: (_key, value) => { saved = value } }
  const first = createWorkResultTracker(storage)
  const second = createWorkResultTracker(storage)
  first([item('anomaly', 'first-report')])
  second([item('decision', 'second-report')])
  first([item('completed', 'third-report')])
  const reloaded = createWorkResultTracker(storage)
  assert.deepEqual(reloaded([item('anomaly', 'first-report'), item('decision', 'second-report'), item('completed', 'third-report')]), [])
})
