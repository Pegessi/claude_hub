import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/chatGoalPolicy.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const policy = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

const goal = status => ({ status })

test('only an active Goal prevents switching into Plan mode', () => {
  assert.equal(policy.goalBlocksPlan(goal('active')), true)
  for (const state of ['paused', 'blocked', 'usageLimited', 'budgetLimited', 'complete', 'cancelled', 'failed']) {
    assert.equal(policy.goalBlocksPlan(goal(state)), false, state)
  }
  assert.match(policy.goalPlanLockReason(goal('active')), /Pause or finish/)
  assert.equal(policy.goalPlanLockReason(null), null)
})

test('unconfirmed cancellation prevents starting a competing turn or switching Plan', () => {
  assert.equal(policy.goalBlocksPlan({ status: 'cancelled', dispatch_state: 'uncertain' }), true)
  assert.equal(policy.goalBlocksPlan({ status: 'paused', dispatch_state: 'pending' }), true)
})

test('native pending stop retries interruption without clearing a completed Goal', () => {
  for (const status of ['active', 'paused', 'complete', 'cancelled', 'failed']) {
    assert.equal(policy.goalStopRetryAction({
      status, dispatch_state: 'uncertain', execution_owner: 'provider_native', provider_stop_pending: true,
    }), 'pause', status)
  }
})

test('legacy retry-stop routing is unchanged without native pending interruption', () => {
  for (const execution_owner of [undefined, 'hub_managed', 'provider_native']) {
    for (const status of ['complete', 'cancelled', 'failed']) {
      assert.equal(policy.goalStopRetryAction({ status, execution_owner, provider_stop_pending: false }), 'clear')
    }
    for (const status of ['active', 'paused', 'blocked', 'usageLimited', 'budgetLimited']) {
      assert.equal(policy.goalStopRetryAction({ status, execution_owner }), 'pause')
    }
  }
  assert.equal(policy.goalStopRetryAction({
    status: 'complete', execution_owner: 'hub_managed', provider_stop_pending: true,
  }), 'clear')
})

test('only a trailing Goal control envelope is hidden from assistant text', () => {
  const envelope = '\n<goal-checkpoint>\n{"remaining":[]}\n</goal-checkpoint>\n<goal-status state="complete">done</goal-status>\n'
  assert.equal(policy.stripTrailingGoalProtocol(`Done.${envelope}`), 'Done.')
  assert.equal(
    policy.stripTrailingGoalProtocol('Done.\n<goal-status state="continue">next</goal-status>'),
    'Done.',
  )
  const prose = 'Use `<goal-checkpoint>` then </goal-checkpoint> in docs.'
  assert.equal(policy.stripTrailingGoalProtocol(`${prose}${envelope}`), prose)
  const quoted = 'Example: <goal-status state="complete">done</goal-status> appears mid-answer.'
  assert.equal(policy.stripTrailingGoalProtocol(quoted), quoted)
  const opener = 'Parse <goal-status state="complete"> openers carefully.'
  assert.equal(policy.stripTrailingGoalProtocol(`${opener}${envelope}`), opener)
  assert.equal(policy.stripTrailingGoalProtocol('Plain answer'), 'Plain answer')
})

test('terminal and display states cover the public Goal contract', () => {
  assert.equal(policy.isGoalTerminal('complete'), true)
  assert.equal(policy.isGoalTerminal('cancelled'), true)
  assert.equal(policy.isGoalTerminal('failed'), true)
  assert.equal(policy.isGoalTerminal('paused'), false)
  assert.equal(policy.goalStatusLabel('paused'), 'Paused')
  for (const [status, label] of [['usageLimited', 'Usage limited'], ['budgetLimited', 'Budget limited']]) {
    assert.equal(policy.isGoalTerminal(status), false)
    assert.equal(policy.goalStatusLabel(status), label)
  }
})
