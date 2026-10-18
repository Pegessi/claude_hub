import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceProjection } from './workspaceProjectionTestHarness.mjs'

const {
  sanitizePeriodicTasks, enabledPeriodicTaskCount, residentUpdatePayload,
  residentLastRunLabel, residentNextRunLabel, latestResidentReport,
} = await loadWorkspaceProjection('resident')

const now = Date.parse('2026-10-17T12:00:00Z')
const at = seconds => new Date(now + seconds * 1000).toISOString()

function draft(overrides = {}) {
  return {
    resident_agent_enabled: true, resident_agent_paused: false, resident_agent_master_mode: true,
    resident_agent_interval_minutes: 60, resident_agent_directive: '  maintain  ',
    resident_agent_periodic_tasks: [{ id: 'one', text: '  check  ', enabled: true }],
    resident_agent_type: 'claude', resident_agent_solo_mode: true, resident_agent_title: '  resident  ',
    resident_agent_target: 'remote', resident_agent_remote_profile_id: 'remote-id',
    resident_agent_cwd: '  /workspace  ', resident_agent_remote_reconnect: true,
    ...overrides,
  }
}

test('periodic-task normalization preserves ids, disabled rows, order, and input objects', () => {
  const rows = Object.freeze([
    Object.freeze({ id: 'one', text: '  first  ', enabled: false }),
    Object.freeze({ id: 'two', text: ' \n ', enabled: true }),
    Object.freeze({ id: 'three', text: 'second', enabled: true }),
    Object.freeze({ id: 'four', text: null, enabled: true }),
  ])
  const normalized = sanitizePeriodicTasks(rows)
  assert.deepEqual(normalized, [
    { id: 'one', text: 'first', enabled: false }, { id: 'three', text: 'second', enabled: true },
  ])
  assert.notEqual(normalized[0], rows[0])
  assert.equal(rows[0].text, '  first  ')
  assert.equal(enabledPeriodicTaskCount(rows), 1)
  assert.deepEqual(sanitizePeriodicTasks([]), [])
  assert.equal(enabledPeriodicTaskCount([]), 0)
})

test('resident payload projects trimmed draft values without changing draft or environment', () => {
  const form = draft()
  const snapshot = structuredClone(form)
  const env = Object.freeze({ MODEL: 'custom' })
  const payload = residentUpdatePayload(form, env)
  assert.deepEqual(payload, {
    resident_agent_enabled: true, resident_agent_paused: false, resident_agent_master_mode: true,
    resident_agent_interval_minutes: 60, resident_agent_directive: 'maintain',
    resident_agent_periodic_tasks: [{ id: 'one', text: 'check', enabled: true }],
    resident_agent_type: 'claude', resident_agent_env: env, resident_agent_solo_mode: true,
    resident_agent_title: 'resident', resident_agent_target: 'remote', resident_agent_remote_profile_id: 'remote-id',
    resident_agent_cwd: '/workspace', resident_agent_remote_reconnect: true,
  })
  assert.deepEqual(form, snapshot)
  assert.equal(payload.resident_agent_env, env)
  assert.notEqual(payload.resident_agent_periodic_tasks, form.resident_agent_periodic_tasks)
})

test('resident payload distinguishes omitted directive from cleared placement and applies overrides last', () => {
  const form = draft({
    resident_agent_directive: ' ', resident_agent_title: ' ', resident_agent_cwd: '',
    resident_agent_target: 'local',
  })
  const payload = residentUpdatePayload(form, {})
  assert.equal(payload.resident_agent_directive, undefined)
  assert.equal(payload.resident_agent_title, null)
  assert.equal(payload.resident_agent_cwd, null)
  assert.equal(payload.resident_agent_remote_profile_id, null)
  assert.equal(residentUpdatePayload(draft({ resident_agent_remote_profile_id: '' }), {}).resident_agent_remote_profile_id, null)
  assert.equal(residentUpdatePayload(form, {}, { resident_agent_title: 'override', resident_agent_enabled: false }).resident_agent_title, 'override')
  assert.equal(residentUpdatePayload(form, {}, { resident_agent_enabled: false }).resident_agent_enabled, false)
})

test('resident run labels retain absent, paused, requested, disabled, and due priority', () => {
  const workspace = draft({ resident_agent_next_run_at: at(90), resident_agent_last_run_at: at(-120) })
  assert.equal(residentLastRunLabel(workspace, now), '2m ago')
  assert.equal(residentLastRunLabel(null, now), 'never')
  assert.equal(residentLastRunLabel({ resident_agent_last_run_at: 'invalid' }, now), 'never')
  assert.equal(residentLastRunLabel({ resident_agent_last_run_at: at(30) }, now), '0s ago')
  assert.equal(residentNextRunLabel(workspace, now), 'in 1m')
  assert.equal(residentNextRunLabel({ ...workspace, resident_agent_next_run_at: at(0) }, now), 'due now')
  assert.equal(residentNextRunLabel({ ...workspace, resident_agent_paused: true }, now), 'paused')
  assert.equal(residentNextRunLabel({ ...workspace, resident_agent_paused: true, resident_agent_run_requested_at: at(-10) }, now), 'queued')
  assert.equal(residentNextRunLabel({ ...workspace, resident_agent_enabled: false, resident_agent_run_requested_at: at(-10) }, now), '')
  assert.equal(residentNextRunLabel({ ...workspace, resident_agent_next_run_at: 'invalid' }, now), '')
  assert.equal(residentNextRunLabel(null, now), '')
})

test('latest resident report filters by session and sorts copies with stable timestamp ties', () => {
  const earlier = { id: 'earlier', session_id: 'resident', created_at: at(-60) }
  const latest = { id: 'latest', session_id: 'resident', created_at: at(-30) }
  const tied = { id: 'tied', session_id: 'resident', created_at: at(-30) }
  const reports = Object.freeze([
    earlier, latest, tied,
    { id: 'other', session_id: 'other', created_at: at(0) },
    { id: 'invalid', session_id: 'resident', created_at: 'invalid' },
  ])
  assert.equal(latestResidentReport('resident', reports), latest)
  assert.equal(latestResidentReport('', reports), null)
  assert.equal(latestResidentReport('absent', reports), null)
  assert.deepEqual(reports.map(report => report.id), ['earlier', 'latest', 'tied', 'other', 'invalid'])
})
