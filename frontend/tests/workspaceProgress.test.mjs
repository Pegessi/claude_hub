import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceProjection } from './workspaceProjectionTestHarness.mjs'

const {
  progressTimeline, latestReportAgeLabel, taskTotalElapsedLabel, taskWorkingElapsedLabel,
  reportElapsedLabel, reportElapsedTitle,
} = await loadWorkspaceProjection('progress')

const epoch = Date.parse('2026-10-17T10:00:00Z')
const at = seconds => new Date(epoch + seconds * 1000).toISOString()
const now = epoch + 180_000
const task = overrides => ({ id: 'task', status: 'working', created_at: at(0), ...overrides })
const report = (id, seconds, state = 'working') => ({ id, created_at: at(seconds), state })

test('progress timeline sorts timestamped records stably and retains elapsed/delta labels', () => {
  const reports = Object.freeze([report('later', 120, 'ready_for_review'), report('early', 30)])
  const rows = progressTimeline(task({ queued_at: at(30), started_at: at(60), reviewed_at: 'invalid' }), reports, now)
  assert.deepEqual(rows.map(row => [row.id, row.label, row.tone, row.elapsedLabel, row.deltaLabel]), [
    ['task-created', 'Created', 'task', '0s', ''],
    ['task-queued', 'Queued', 'task', '30s', '30s'],
    ['report-early', 'working', 'report', '30s', '0s'],
    ['task-started', 'Started', 'task', '1m', '30s'],
    ['report-later', 'ready for review', 'report', '2m', '1m'],
    ['task-now', 'Now', 'live', '3m', '1m'],
  ])
  assert.deepEqual(reports.map(value => value.id), ['later', 'early'])
  assert.deepEqual(progressTimeline(null, reports, now), [])
})

test('timeline renders terminal markers and includes Now only for live statuses', () => {
  const rows = progressTimeline(task({ status: 'done', reviewed_at: at(90), failed_at: at(120), completed_at: at(150), manual_aborted_at: at(160) }), [], now)
  assert.deepEqual(rows.slice(1).map(row => [row.id, row.tone]), [
    ['task-reviewed', 'task'], ['task-failed', 'terminal'], ['task-done', 'terminal'], ['task-aborted', 'terminal'],
  ])
  for (const status of ['queued', 'working', 'review', 'failed', 'todo', 'done']) {
    assert.equal(progressTimeline(task({ status, created_at: 'invalid' }), [], now).some(row => row.id === 'task-now'), ['queued', 'working', 'review', 'failed'].includes(status))
  }
})

test('report age uses the final supplied report, handles invalid dates, and clamps future clocks', () => {
  assert.equal(latestReportAgeLabel([], now), 'none')
  assert.equal(latestReportAgeLabel([{ created_at: 'invalid' }], now), 'unknown')
  assert.equal(latestReportAgeLabel([report('late', 120), report('early', 30)], now), '2m ago')
  assert.equal(latestReportAgeLabel([report('future', 300)], now), '0s ago')
})

test('total elapsed prefers completion then abort; otherwise uses live clock or update time', () => {
  assert.equal(taskTotalElapsedLabel(task({ completed_at: at(60), manual_aborted_at: at(90) }), now), '1m')
  assert.equal(taskTotalElapsedLabel(task({ completed_at: 'invalid', manual_aborted_at: at(90) }), now), '1m')
  for (const status of ['queued', 'working', 'review', 'failed']) {
    assert.equal(taskTotalElapsedLabel(task({ status, updated_at: at(30) }), now), '3m')
  }
  assert.equal(taskTotalElapsedLabel(task({ status: 'todo', updated_at: at(30) }), now), '30s')
  assert.equal(taskTotalElapsedLabel(task({ status: 'done', updated_at: 'invalid' }), now), '3m')
  assert.equal(taskTotalElapsedLabel(task({ created_at: null }), now), 'unknown')
})

test('working elapsed start precedence is task, first iteration, then first supplied report', () => {
  const reports = [report('first', 120), report('second', 90)]
  const autonomous = { iterations: [{ started_at: at(60) }, { started_at: at(30) }] }
  assert.equal(taskWorkingElapsedLabel(task({ started_at: at(30), autonomous_run: autonomous }), reports, now), '2m')
  assert.equal(taskWorkingElapsedLabel(task({ started_at: 'invalid', autonomous_run: autonomous }), reports, now), '2m')
  assert.equal(taskWorkingElapsedLabel(task({ autonomous_run: { iterations: [] } }), reports, now), '1m')
  assert.equal(taskWorkingElapsedLabel(task({}), [], now), 'not started')
})

test('report elapsed reference prefers previous report then eligible start, queue, and creation', () => {
  const reports = [report('first', 60), report('second', 90)]
  assert.equal(reportElapsedLabel(reports[0], 0, task({ started_at: at(30) }), reports), 'start +30s')
  assert.equal(reportElapsedLabel(reports[0], 0, task({ started_at: at(70), queued_at: at(20) }), reports), 'queue +40s')
  assert.equal(reportElapsedLabel(reports[0], 0, task({ started_at: at(70), queued_at: at(65) }), reports), 'creation +1m')
  assert.equal(reportElapsedLabel(reports[1], 1, task({ started_at: at(0) }), reports), '+30s')
  assert.equal(reportElapsedTitle(reports[1], 1, null, reports), 'Elapsed since previous report: 30s')
  assert.equal(reportElapsedTitle(reports[0], 0, task({ created_at: at(90) }), reports), 'Elapsed since creation: 0s')
})

test('report timing returns explicit unavailable labels with no valid reference', () => {
  const invalid = { created_at: 'invalid' }
  assert.equal(reportElapsedLabel(invalid, 0, task({}), [invalid]), '')
  assert.equal(reportElapsedTitle(invalid, 0, task({}), [invalid]), 'Elapsed time unavailable')
  const valid = report('valid', 90)
  assert.equal(reportElapsedLabel(valid, 0, null, [valid]), '')
  assert.equal(reportElapsedTitle(valid, 0, task({ created_at: null }), [valid]), 'Elapsed time unavailable')
  assert.equal(reportElapsedLabel(valid, 1, task({}), [invalid, valid]), '+1m')
})
