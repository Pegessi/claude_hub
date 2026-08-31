import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const composable = readFileSync(
  new URL('../src/composables/useAgentStream.ts', import.meta.url),
  'utf8',
)

test('long-poll reconciliation starts even when EventSource is available', () => {
  const startIndex = composable.indexOf('longPollAbort = new AbortController()')
  const loopIndex = composable.indexOf('void longPollLoop(sourceId, streamPath)', startIndex)
  const sseIndex = composable.indexOf("if (typeof EventSource !== 'undefined')", loopIndex)

  assert.ok(startIndex >= 0)
  assert.ok(loopIndex > startIndex)
  assert.ok(sseIndex > loopIndex)
})

test('SSE errors retire only SSE while long-poll owns failure reporting', () => {
  const errorHandler = composable.match(
    /eventSource\.addEventListener\('error',[\s\S]*?\n {4}\}\)/,
  )?.[0]

  assert.ok(errorHandler)
  assert.match(errorHandler, /closeSse\(\)/)
  assert.doesNotMatch(errorHandler, /connectionState\.value = 'failed'/)
  assert.doesNotMatch(errorHandler, /stop\(\)/)
})
