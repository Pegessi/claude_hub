import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/timelineFollow.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const { isTimelineNearBottom } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('timeline follows content growth while it remains near the tail', () => {
  assert.equal(isTimelineNearBottom({ scrollTop: 536, clientHeight: 400, scrollHeight: 1000 }), true)
  assert.equal(isTimelineNearBottom({ scrollTop: 535, clientHeight: 400, scrollHeight: 1000 }), false)
})

test('timeline does not claim a deliberately detached viewport is near the tail', () => {
  assert.equal(isTimelineNearBottom({ scrollTop: 120, clientHeight: 400, scrollHeight: 1000 }), false)
})

test('new or temporarily unmeasurable timelines default to following', () => {
  assert.equal(isTimelineNearBottom({ scrollTop: 0, clientHeight: 0, scrollHeight: 0 }), true)
  assert.equal(isTimelineNearBottom({ scrollTop: Number.NaN, clientHeight: 400, scrollHeight: 1000 }), true)
})
