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
  for (const state of ['paused', 'blocked', 'budget_limited', 'complete', 'cancelled', 'failed']) {
    assert.equal(policy.goalBlocksPlan(goal(state)), false, state)
  }
  assert.match(policy.goalPlanLockReason(goal('active')), /Pause or finish/)
  assert.equal(policy.goalPlanLockReason(null), null)
})

test('usage labels never present estimated or unavailable accounting as exact', () => {
  assert.equal(policy.goalUsageLabel(1234, 5000, 'exact'), '1,234 / 5,000 tokens (exact)')
  assert.equal(policy.goalUsageLabel(1234, null, 'estimated'), '1,234 tokens (estimated)')
  assert.equal(policy.goalUsageLabel(null, 5000, 'unavailable'), 'Usage unavailable · 5,000 token budget')
})

test('terminal and display states cover the public Goal contract', () => {
  assert.equal(policy.isGoalTerminal('complete'), true)
  assert.equal(policy.isGoalTerminal('cancelled'), true)
  assert.equal(policy.isGoalTerminal('failed'), true)
  assert.equal(policy.isGoalTerminal('budget_limited'), false)
  assert.equal(policy.goalStatusLabel('budget_limited'), 'Budget limited')
})
