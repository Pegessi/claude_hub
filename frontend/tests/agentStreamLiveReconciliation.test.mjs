import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const composable = readFileSync(
  new URL('../src/composables/useAgentStream.ts', import.meta.url),
  'utf8',
)

const sequenceSource = await readFile(
  new URL('../src/utils/agentStreamSequence.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(sequenceSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { createContiguousEventBuffer } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('SSE futures wait until long-poll fills the sequence gap', () => {
  const buffer = createContiguousEventBuffer()
  assert.deepEqual(buffer.push([{ stream_sequence: 1 }]), [])
  assert.equal(buffer.cursor, -1)
  assert.deepEqual(buffer.push([{ stream_sequence: 0 }]).map(event => event.stream_sequence), [0, 1])
  assert.equal(buffer.cursor, 1)
})

test('duplicate paths commit each sequence exactly once', () => {
  const buffer = createContiguousEventBuffer()
  assert.deepEqual(
    buffer.push([{ stream_sequence: 0 }, { stream_sequence: 0 }, { stream_sequence: 2 }])
      .map(event => event.stream_sequence),
    [0],
  )
  assert.deepEqual(
    buffer.push([{ stream_sequence: 1 }, { stream_sequence: 2 }]).map(event => event.stream_sequence),
    [1, 2],
  )
  assert.deepEqual(buffer.push([{ stream_sequence: 1 }]), [])
})

test('long-poll reconciliation starts even when EventSource is available', () => {
  const startIndex = composable.indexOf('longPollAbort = new AbortController()')
  const loopIndex = composable.indexOf('void longPollLoop(', startIndex)
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

test('explicit retry replaces the failed backend transport before hydration', () => {
  const retryIndex = composable.indexOf('async function retry(')
  const postIndex = composable.indexOf("method: 'POST'", retryIndex)
  const startIndex = composable.indexOf('await start(sourceId, source)', postIndex)

  assert.ok(retryIndex >= 0)
  assert.ok(postIndex > retryIndex)
  assert.ok(startIndex > postIndex)
  assert.match(composable.slice(retryIndex, startIndex), /`\$\{streamPath\}\/retry`/)
})

test('mode updates are scoped to the active stream and replace capabilities only after success', () => {
  const modeIndex = composable.indexOf('async function setMode(')
  const putIndex = composable.indexOf("method: 'PUT'", modeIndex)
  const responseIndex = composable.indexOf('const nextCapabilities', putIndex)
  const assignmentIndex = composable.indexOf('capabilities.value = nextCapabilities', responseIndex)

  assert.ok(modeIndex >= 0)
  assert.ok(putIndex > modeIndex)
  assert.ok(responseIndex > putIndex)
  assert.ok(assignmentIndex > responseIndex)
  assert.match(composable.slice(modeIndex, responseIndex), /`\$\{streamPath\}\/mode`/)
  assert.match(composable.slice(modeIndex, responseIndex), /JSON\.stringify\(\{ mode \}\)/)
  assert.match(composable.slice(modeIndex, assignmentIndex), /currentSessionId !== sourceId/)
})
