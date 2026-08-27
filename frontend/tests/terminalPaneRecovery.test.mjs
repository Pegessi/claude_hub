import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/terminalPaneRecovery.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  bumpIframeDocumentGeneration,
  shouldDedupePaneRecovery,
  isStalePaneRefreshDone,
} = mod

test('bumpIframeDocumentGeneration increments per tab', () => {
  const counts = {}
  assert.equal(bumpIframeDocumentGeneration(counts, 'tab-a'), 1)
  assert.equal(bumpIframeDocumentGeneration(counts, 'tab-a'), 2)
  assert.equal(bumpIframeDocumentGeneration(counts, 'tab-b'), 1)
})

test('shouldDedupePaneRecovery only dedupes same tab+switch+document generation', () => {
  const inFlight = { tabId: 't1', tabSwitchGeneration: 2, documentGeneration: 1 }
  assert.equal(shouldDedupePaneRecovery(inFlight, 't1', 2, 1), true)
  assert.equal(shouldDedupePaneRecovery(inFlight, 't1', 2, 2), false, 'new iframe doc must not dedupe')
  assert.equal(shouldDedupePaneRecovery(inFlight, 't1', 3, 1), false, 'tab switch must not dedupe')
})

test('isStalePaneRefreshDone rejects stale tab-switch and prior iframe document', () => {
  const correlation = { tabId: 't1', tabSwitchGeneration: 2, documentGeneration: 1 }
  assert.equal(isStalePaneRefreshDone(correlation, 2, 2), true, 'prior doc refresh-done is stale after reload')
  assert.equal(isStalePaneRefreshDone(correlation, 3, 1), true, 'prior tab-switch generation is stale')
  assert.equal(isStalePaneRefreshDone(correlation, 2, 1), false, 'matching correlation is fresh')
})

test('iframe reload race: new document generation allows fresh recovery', () => {
  const counts = {}
  const doc1 = bumpIframeDocumentGeneration(counts, 't1')
  const inFlight = { tabId: 't1', tabSwitchGeneration: 1, documentGeneration: doc1 }
  assert.equal(shouldDedupePaneRecovery(inFlight, 't1', 1, doc1), true)
  const doc2 = bumpIframeDocumentGeneration(counts, 't1')
  assert.equal(doc2, 2)
  assert.equal(shouldDedupePaneRecovery(inFlight, 't1', 1, doc2), false)
  assert.equal(isStalePaneRefreshDone(inFlight, 1, doc2), true)
})
