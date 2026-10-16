import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// Same data-URL bundling as the other timeline suites: the module's sibling
// `@/utils/*` imports are stripped and concatenated ahead of it because a
// data-URL module has no resolver.
const timelineSource = await readFile(
  new URL('../src/utils/agentStreamTimeline.ts', import.meta.url),
  'utf8',
)
const transpileOptions = {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
}
const siblings = await Promise.all(
  ['duration', 'chatQuestionResponse', 'subagentTool', 'agentImage'].map(async name => {
    const source = await readFile(
      new URL(`../src/utils/${name}.ts`, import.meta.url),
      'utf8',
    )
    return ts.transpileModule(source, transpileOptions).outputText
  }),
)
const timelineJs = ts.transpileModule(
  timelineSource.replace(/^import .* from '@\/utils\/.*$/gm, ''),
  transpileOptions,
).outputText
const bundled = `${siblings.join('\n')}\n${timelineJs}`
const { groupEventsIntoTurns } = await import(
  `data:text/javascript;base64,${Buffer.from(bundled).toString('base64')}`
)

const paneSource = await readFile(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)
const errorStart = paneSource.indexOf(`v-else-if="part.kind === 'error'"`)
const statusStart = paneSource.indexOf(`v-else-if="part.kind === 'status'"`, errorStart)
assert.ok(errorStart > -1, 'pane renders the error part')
assert.ok(statusStart > errorStart, 'status branch follows the complete error branch')
const errorBlock = paneSource.slice(errorStart, statusStart)

function errorEvent(payload, seq = 2) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'maso',
    type: 'error',
    payload,
    created_at: '2026-10-15T06:41:35.548001Z',
    run_epoch: 1,
    turn_id: 'turn-1',
    message_id: null,
    call_id: null,
  }
}

function errorParts(payload) {
  const [turn] = groupEventsIntoTurns([
    { ...errorEvent({}, 1), type: 'turn_started', payload: { summary: 'go' } },
    errorEvent(payload),
  ])
  return turn.parts.filter(part => part.kind === 'error')
}

test('a MASO failure keeps the provider payload beside the explanation', () => {
  const [part] = errorParts({
    message:
      'MASO run failed and reported no error description. '
      + 'Retry the turn; if it repeats, check the MASO service logs.',
    detail: '{"sequence_number": 1, "type": "error"}',
    stop_reason: 'error',
  })
  assert.equal(
    part.message,
    'MASO run failed and reported no error description. '
    + 'Retry the turn; if it repeats, check the MASO service logs.',
  )
  assert.equal(part.detail, '{"sequence_number": 1, "type": "error"}')
})

test('an error without provider evidence carries no detail', () => {
  assert.equal(errorParts({ message: 'Connection refused' })[0].detail, undefined)
  assert.equal(errorParts({ message: 'Connection refused', detail: '' })[0].detail, undefined)
  // A non-string detail is provider noise, not text to paint.
  assert.equal(errorParts({ message: 'Connection refused', detail: 7 })[0].detail, undefined)
})

test('the banner shows the explanation first and the payload as evidence', () => {
  assert.match(errorBlock, /role="alert"/)
  assert.match(errorBlock, /\{\{ part\.message \}\}/)
  assert.match(errorBlock, /v-if="part\.detail"/)
  assert.match(errorBlock, /class="error-detail"/)
  assert.match(errorBlock, /\{\{ part\.detail \}\}/)
  assert.ok(
    errorBlock.indexOf('part.message') < errorBlock.indexOf('part.detail'),
    'the readable statement precedes the raw payload',
  )
})

test('the evidence line is de-emphasized and wraps instead of overflowing', () => {
  assert.match(paneSource, /\.error-body \{[\s\S]*?flex-direction: column/)
  assert.match(paneSource, /\.error-body \{[\s\S]*?min-width: 0/)
  assert.match(paneSource, /\.error-detail \{[\s\S]*?--ch-font-mono/)
  assert.match(paneSource, /\.error-detail \{[\s\S]*?--ch-color-text-subtle/)
  assert.match(paneSource, /\.error-detail \{[\s\S]*?overflow-wrap: anywhere/)
})
