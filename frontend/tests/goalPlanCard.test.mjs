import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const load = async name => {
  const source = readFileSync(new URL(`../src/features/chat/goal-plan/${name}.ts`, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
}

const planCard = await load('planCard')
const preference = await load('planPreference')

const PLAN = {
  summary: 'Add a bounded cache with tests',
  steps: [
    { title: 'Extract the cache', detail: 'Move the lookup into a helper' },
    { title: 'Verify', acceptance: 'targeted tests pass' },
  ],
  budget: {
    helpers: [{ provider: 'claude', model: 'opus', role: 'implementation' }],
    max_parallelism: 2,
    duration_estimate: 'about 12 minutes',
  },
}

const goal = overrides => ({
  id: 'goal-1',
  tab_id: 'tab-1',
  objective: 'Add a bounded cache',
  status: 'active',
  token_usage: null,
  usage_quality: 'unavailable',
  turns_completed: 0,
  dispatch_state: 'idle',
  created_at: '2026-11-09T00:00:00Z',
  updated_at: '2026-11-09T00:00:00Z',
  ...overrides,
})

test('only the Goal-owned planning turn becomes a plan card', () => {
  const blocked = goal({ plan_first: true, plan_turn_id: 'plan-turn', plan: PLAN, status: 'blocked' })
  assert.equal(planCard.goalPlanCardMode(blocked, 'plan-turn'), 'review')
  // Any other turn — including a later blocked turn — stays ordinary.
  assert.equal(planCard.goalPlanCardMode(blocked, 'other-turn'), null)
  assert.equal(planCard.goalPlanCardMode(blocked, null), null)
  assert.equal(planCard.goalPlanCardMode(blocked, undefined), null)
  assert.equal(planCard.goalPlanCardMode(null, 'plan-turn'), null)
  assert.equal(planCard.goalPlanCardMode(goal({ objective: 'plain' }), 'plan-turn'), null)
})

test('a running planning turn prepares, and an approved plan is decided', () => {
  const dispatched = goal({ plan_first: true, plan_turn_id: 'plan-turn', dispatch_state: 'dispatched' })
  assert.equal(planCard.goalPlanCardMode(dispatched, 'plan-turn'), 'preparing')
  const parsed = goal({ plan_first: true, plan_turn_id: 'plan-turn', plan: PLAN, dispatch_state: 'dispatched' })
  // The plan exists but the Goal has not parked for approval yet.
  assert.equal(planCard.goalPlanCardMode(parsed, 'plan-turn'), 'waiting')
  // A Goal that kept working instead of parking shows the plan without actions.
  assert.equal(planCard.goalPlanCardMode(goal({ plan_turn_id: 'plan-turn', plan: PLAN }), 'plan-turn'), 'waiting')
  assert.equal(
    planCard.goalPlanCardMode(goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_approved: true, status: 'blocked' }), 'plan-turn'),
    'decided',
  )
})

test('approval is refused with a reason while the Goal cannot accept it', () => {
  const blocked = goal({ plan_turn_id: 'plan-turn', plan: PLAN, status: 'blocked' })
  assert.equal(planCard.goalPlanApprovalDisabledReason(blocked, 'review', { sending: false, turnInFlight: false }), null)
  assert.match(
    planCard.goalPlanApprovalDisabledReason(blocked, 'decided', { sending: false, turnInFlight: false }),
    /not awaiting approval/,
  )
  assert.match(
    planCard.goalPlanApprovalDisabledReason({ ...blocked, dispatch_state: 'uncertain' }, 'review', { sending: false, turnInFlight: false }),
    /settle/,
  )
  assert.match(
    planCard.goalPlanApprovalDisabledReason({ ...blocked, pending_image_count: 1 }, 'review', { sending: false, turnInFlight: false }),
    /Reattach images/,
  )
  assert.match(
    planCard.goalPlanApprovalDisabledReason(blocked, 'review', { sending: true, turnInFlight: false }),
    /Sending/,
  )
  assert.match(
    planCard.goalPlanApprovalDisabledReason(blocked, 'review', { sending: false, turnInFlight: true }),
    /current turn/,
  )
})

test('approval and revision messages are the exact protocol text', () => {
  assert.equal(planCard.GOAL_PLAN_APPROVAL_MESSAGE, 'Approve the plan. Start implementing it now.')
  assert.equal(planCard.GOAL_PLAN_REVISE_PREFIX, 'Revise the plan: ')
})

test('the memo signature changes only for the planning turn', () => {
  const blocked = goal({ plan_turn_id: 'plan-turn', status: 'blocked' })
  assert.equal(planCard.goalPlanSignature(blocked, 'other-turn'), '')
  assert.equal(planCard.goalPlanSignature(blocked, 'plan-turn'), 'review|none|')
  assert.equal(
    planCard.goalPlanSignature(goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_warning: 'bad' }), 'plan-turn'),
    'waiting|plan|warn',
  )
  assert.notEqual(
    planCard.goalPlanSignature(goal({ plan_turn_id: 'plan-turn', plan: PLAN }), 'plan-turn'),
    planCard.goalPlanSignature(goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_approved: true }), 'plan-turn'),
  )
})

test('the budget summary names helpers, models, parallelism and duration', () => {
  assert.equal(
    planCard.goalPlanBudgetSummary(PLAN),
    '1 helper · claude/opus · max 2 in parallel · about 12 minutes',
  )
  assert.equal(
    planCard.goalPlanBudgetSummary({ ...PLAN, budget: { helpers: [], max_parallelism: 1, duration_estimate: null } }),
    'no helpers · max 1 in parallel',
  )
  assert.equal(planCard.goalPlanBudgetSummary(null), '')
})

test('plan-first defaults on and survives a round trip, including storage failures', () => {
  const store = new Map()
  const storage = {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, value),
  }
  assert.equal(preference.readPlanFirstPreference(storage), true)
  preference.writePlanFirstPreference(storage, false)
  assert.equal(preference.readPlanFirstPreference(storage), false)
  preference.writePlanFirstPreference(storage, true)
  assert.equal(preference.readPlanFirstPreference(storage), true)
  // A blocked or absent store must not break Goal setup.
  assert.equal(preference.readPlanFirstPreference({ getItem: () => { throw new Error('denied') } }), true)
  preference.writePlanFirstPreference({ setItem: () => { throw new Error('denied') } }, false)
  assert.equal(preference.readPlanFirstPreference(null), true)
})
