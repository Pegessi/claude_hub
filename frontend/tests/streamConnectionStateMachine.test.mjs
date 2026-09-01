import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/streamConnectionStateMachine.ts', import.meta.url),
  'utf8',
)

const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})

const { StreamConnectionStateMachine } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('start transitions to hydrating and returns a generation id', () => {
  const sm = new StreamConnectionStateMachine()
  const gen = sm.start()
  assert.equal(sm.getState(), 'hydrating')
  assert.equal(typeof gen, 'number')
  assert.ok(gen > 0)
})

test('success on the current generation transitions to live', () => {
  const sm = new StreamConnectionStateMachine()
  const gen = sm.start()
  const ok = sm.success(gen)
  assert.equal(ok, true)
  assert.equal(sm.getState(), 'live')
})

test('fail on the current generation transitions to failed', () => {
  const sm = new StreamConnectionStateMachine()
  const gen = sm.start()
  const ok = sm.fail(gen, 'boom')
  assert.equal(ok, true)
  assert.equal(sm.getState(), 'failed')
  assert.equal(sm.getErrorMessage(), 'boom')
})

test('stop resets to idle', () => {
  const sm = new StreamConnectionStateMachine()
  sm.start()
  sm.stop()
  assert.equal(sm.getState(), 'idle')
  assert.equal(sm.getErrorMessage(), null)
})

// ── Generation ownership (the bug) ────────────────────────────────────────

test('a stale generation cannot flip hydrating to live', () => {
  const sm = new StreamConnectionStateMachine()
  const genA = sm.start() // generation 1, hydrating
  sm.start() // generation 2, still hydrating (supersedes A)

  // A's hydration resolves late. It must NOT move the state to live.
  const ok = sm.success(genA)
  assert.equal(ok, false, 'stale success must be rejected')
  assert.equal(sm.getState(), 'hydrating', 'state must stay hydrating for the current generation')
})

test('a stale generation cannot overwrite live with failed', () => {
  const sm = new StreamConnectionStateMachine()
  const genA = sm.start()
  const genB = sm.start()
  sm.success(genB) // current generation goes live
  assert.equal(sm.getState(), 'live')

  // A's failure arrives late. It must NOT overwrite live.
  const ok = sm.fail(genA, 'stale error')
  assert.equal(ok, false, 'stale fail must be rejected')
  assert.equal(sm.getState(), 'live', 'live must not be overwritten by a stale failure')
  assert.equal(sm.getErrorMessage(), null, 'no error message from a stale failure')
})

test('a stale generation cannot overwrite failed with live', () => {
  const sm = new StreamConnectionStateMachine()
  const genA = sm.start()
  const genB = sm.start()
  sm.fail(genB, 'current error') // current generation fails
  assert.equal(sm.getState(), 'failed')

  // A's success arrives late. It must NOT overwrite failed.
  const ok = sm.success(genA)
  assert.equal(ok, false, 'stale success must be rejected')
  assert.equal(sm.getState(), 'failed', 'failed must not be overwritten by a stale success')
})

test('after stop, a prior generation cannot mutate state', () => {
  const sm = new StreamConnectionStateMachine()
  const gen = sm.start()
  sm.stop() // idle
  assert.equal(sm.getState(), 'idle')

  const ok = sm.success(gen)
  assert.equal(ok, false)
  assert.equal(sm.getState(), 'idle')
})

test('each start produces a strictly increasing generation id', () => {
  const sm = new StreamConnectionStateMachine()
  const a = sm.start()
  const b = sm.start()
  const c = sm.start()
  assert.ok(b > a)
  assert.ok(c > b)
})
