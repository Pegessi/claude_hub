import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')
const composable = readFileSync(new URL('../src/composables/useChatGoal.ts', import.meta.url), 'utf8')

test('StructuredPane renders Goal setup and persistent status independently of provider type', () => {
  assert.match(pane, /<GoalStatusBar[\s\S]*v-if="goal"/)
  assert.match(pane, /<GoalSetupDialog/)
  assert.doesNotMatch(composable, /agent_type|agentType/)
})

test('active Goal Plan guard exists at both visible option and action boundary', () => {
  assert.ok(pane.includes("option.id === 'plan' && Boolean(goalPlanReason)"))
  assert.ok(pane.includes("modeId === 'plan' && goalPlanReason.value"))
})

test('Goal hydration accepts null and 204 and invalidates stale requests', () => {
  assert.match(composable, /response.status === 204/)
  assert.ok(composable.includes('body ?? null'))
  assert.ok(composable.includes('const requestEpoch = ++epoch'))
  assert.match(composable, /requestEpoch !== epoch/)
  assert.ok(composable.includes('hydrationController?.abort()'))
})
