import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/taskAbort.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(helperSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const helperModule = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { DEFAULT_ABORT_REASON, resolveAbortReason } = helperModule

test('blank abort prompt confirmation uses the audited default reason', () => {
  assert.equal(resolveAbortReason('   '), DEFAULT_ABORT_REASON)
})

test('canceling the abort prompt remains a no-op', () => {
  assert.equal(resolveAbortReason(null), null)
})

test('typed abort prompt reasons are trimmed before submission', () => {
  assert.equal(resolveAbortReason('  reviewer stuck  '), 'reviewer stuck')
})
