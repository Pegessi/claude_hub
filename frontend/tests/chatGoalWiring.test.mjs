import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')
const composable = readFileSync(new URL('../src/composables/useChatGoal.ts', import.meta.url), 'utf8')
const statusBar = readFileSync(new URL('../src/components/GoalStatusBar.vue', import.meta.url), 'utf8')

test('StructuredPane renders Goal setup and persistent status independently of provider type', () => {
  assert.match(pane, /<GoalStatusBar[\s\S]*v-if="goal"/)
  assert.match(pane, /<GoalSetupDialog/)
  assert.doesNotMatch(composable, /agent_type|agentType/)
})

test('Goal details expose the latest checkpoint and invalid-update warning', () => {
  assert.ok(statusBar.includes('Latest checkpoint'))
  assert.ok(statusBar.includes('goal.checkpoint.next_step'))
  assert.ok(statusBar.includes('goal.checkpoint.remaining'))
  assert.ok(statusBar.includes('goal.checkpoint.verified_progress'))
  assert.ok(statusBar.includes('goal.checkpoint_warning'))
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

test('Goal hydration cannot invalidate a mutation busy state', () => {
  assert.ok(composable.includes('if (isMutating.value) return'))
  assert.ok(composable.includes('const requestMutationEpoch = ++mutationEpoch'))
  assert.ok(composable.includes('requestMutationEpoch === mutationEpoch'))
})

test('active Goal guards manual history edits and start waits for an idle turn', () => {
  assert.ok(pane.includes('!isGoalHydrated || Boolean(goalError) || isGoalHydrating || isGoalMutating || turnInFlight'))
  assert.ok(pane.includes('goalEditReason'))
})

test('Goal errors remain visible and setup traps focus', () => {
  assert.ok(statusBar.includes('error && !expanded'))
  const dialog = readFileSync(new URL('../src/components/GoalSetupDialog.vue', import.meta.url), 'utf8')
  assert.ok(dialog.includes('@keydown.tab="trapFocus"'))
  assert.ok(dialog.includes("if (!props.busy) emit('close')"))
})
