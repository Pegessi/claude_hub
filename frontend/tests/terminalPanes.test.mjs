import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/terminalPanes.ts', import.meta.url),
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
const { visiblePaneTabIds, visibleIframes } = helperModule

test('visiblePaneTabIds returns empty set for empty panes', () => {
  const result = visiblePaneTabIds([])
  assert.equal(result.size, 0)
})

test('visiblePaneTabIds skips null tabIds', () => {
  const panes = [{ tabId: null }, { tabId: null }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 0)
})

test('visiblePaneTabIds returns single tab id for 1x1 layout', () => {
  const panes = [{ tabId: 'tab-1' }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 1)
  assert.ok(result.has('tab-1'))
})

test('visiblePaneTabIds returns all non-null tab ids for split layout', () => {
  const panes = [
    { tabId: 'tab-1' },
    { tabId: 'tab-2' },
    { tabId: null },
    { tabId: 'tab-3' },
  ]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 3)
  assert.ok(result.has('tab-1'))
  assert.ok(result.has('tab-2'))
  assert.ok(result.has('tab-3'))
})

test('visiblePaneTabIds deduplicates tab ids assigned to multiple panes', () => {
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-1' }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 1)
  assert.ok(result.has('tab-1'))
})

test('visibleIframes returns only iframes for visible pane tabs', () => {
  const iframes = {
    'tab-1': { contentWindow: {} },
    'tab-2': { contentWindow: {} },
    'tab-hidden': { contentWindow: {} },
  }
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-2' }]
  const result = visibleIframes(iframes, panes)
  assert.deepEqual(Object.keys(result).sort(), ['tab-1', 'tab-2'])
  assert.ok(!('tab-hidden' in result))
})

test('visibleIframes skips null iframes even if tab is in a pane', () => {
  const iframes = {
    'tab-1': null,
    'tab-2': { contentWindow: {} },
  }
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-2' }]
  const result = visibleIframes(iframes, panes)
  assert.deepEqual(Object.keys(result), ['tab-2'])
})

test('visibleIframes returns empty object when no panes have tabs', () => {
  const iframes = { 'tab-1': { contentWindow: {} } }
  const panes = [{ tabId: null }]
  const result = visibleIframes(iframes, panes)
  assert.deepEqual(result, {})
})
