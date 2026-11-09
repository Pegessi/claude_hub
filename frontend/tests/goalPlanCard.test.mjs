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

test('a running planning turn prepares, and an answered plan stops offering approval', () => {
  const dispatched = goal({ plan_first: true, plan_turn_id: 'plan-turn', dispatch_state: 'dispatched' })
  assert.equal(planCard.goalPlanCardMode(dispatched, 'plan-turn'), 'preparing')
  const parsed = goal({ plan_first: true, plan_turn_id: 'plan-turn', plan: PLAN, dispatch_state: 'dispatched' })
  // The plan exists but the Goal has not parked for approval yet.
  assert.equal(planCard.goalPlanCardMode(parsed, 'plan-turn'), 'waiting')
  // A Goal that kept working instead of parking shows the plan without actions.
  assert.equal(planCard.goalPlanCardMode(goal({ plan_turn_id: 'plan-turn', plan: PLAN }), 'plan-turn'), 'waiting')
  assert.equal(
    planCard.goalPlanCardMode(goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_answered: true, status: 'blocked' }), 'plan-turn'),
    'answered',
  )
})

test('an answered plan only claims the Goal continued once its turn settled', () => {
  // A revise request answers the plan too, so the turn it dispatched can still
  // be running; saying "continued" while it runs claims more than happened.
  const running = goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_answered: true, dispatch_state: 'dispatched' })
  assert.equal(planCard.goalPlanCardMode(running, 'plan-turn'), 'continuing')
  assert.equal(
    planCard.goalPlanCardMode({ ...running, dispatch_state: 'pending' }, 'plan-turn'),
    'continuing',
  )
  assert.equal(
    planCard.goalPlanCardMode({ ...running, dispatch_state: 'uncertain' }, 'plan-turn'),
    'continuing',
  )
  const settled = goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_answered: true, dispatch_state: 'idle' })
  assert.equal(planCard.goalPlanCardMode(settled, 'plan-turn'), 'answered')
  // The snapshot has to re-render when the turn settles, so the memo differs.
  assert.notEqual(
    planCard.goalPlanSignature(running, 'plan-turn'),
    planCard.goalPlanSignature(settled, 'plan-turn'),
  )
})

test('approval is refused with a reason while the Goal cannot accept it', () => {
  const blocked = goal({ plan_turn_id: 'plan-turn', plan: PLAN, status: 'blocked' })
  assert.equal(planCard.goalPlanApprovalDisabledReason(blocked, 'review', { sending: false, turnInFlight: false }), null)
  assert.match(
    planCard.goalPlanApprovalDisabledReason(blocked, 'answered', { sending: false, turnInFlight: false }),
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
    planCard.goalPlanSignature(goal({ plan_turn_id: 'plan-turn', plan: PLAN, plan_answered: true }), 'plan-turn'),
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

test('the heading id is unique per turn so two cards cannot share one label', () => {
  const first = planCard.goalPlanHeadingId('turn-aaa')
  const second = planCard.goalPlanHeadingId('turn-bbb')
  assert.notEqual(first, second)
  assert.equal(first, 'goal-plan-heading-turn-aaa')
  // Provider turn ids are opaque: whitespace and punctuation must not leak into
  // an id attribute, and two ids that differ only by such characters stay distinct
  // enough to avoid sharing one accessible name.
  assert.equal(planCard.goalPlanHeadingId('a b/c'), 'goal-plan-heading-a-b-c')
  assert.equal(planCard.goalPlanHeadingId(null), 'goal-plan-heading')
  assert.equal(planCard.goalPlanHeadingId('   '), 'goal-plan-heading')
})

test('a provider-native Chat cannot overwrite the remembered preference', () => {
  const native = { requested: true, disabledReason: 'This provider runs Goals itself' }
  assert.deepEqual(preference.planFirstChoice(native), { effective: false, remember: null })
  const hubManaged = { requested: true, disabledReason: null }
  assert.deepEqual(preference.planFirstChoice(hubManaged), { effective: true, remember: true })
  assert.deepEqual(preference.planFirstChoice({ requested: false, disabledReason: null }), {
    effective: false,
    remember: false,
  })
})

test('the remembered choice survives a native Goal and a storage failure', () => {
  const store = new Map([['claude-hub.goal-plan-first', 'true']])
  const storage = {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, value),
  }
  // A native Goal asks for plan_first but must not be remembered as "off".
  const native = preference.planFirstChoice({ requested: true, disabledReason: 'native' })
  if (native.remember !== null) preference.writePlanFirstPreference(storage, native.remember)
  assert.equal(preference.readPlanFirstPreference(storage), true)
  // A remembered "off" from a Chat that can offer it still persists.
  preference.writePlanFirstPreference(storage, preference.planFirstChoice({
    requested: false,
    disabledReason: null,
  }).remember)
  assert.equal(preference.readPlanFirstPreference(storage), false)
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
