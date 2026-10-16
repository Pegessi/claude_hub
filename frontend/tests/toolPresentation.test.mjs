import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/toolPresentation.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { toolGroupNames, toolStatusLabel } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const pane = await readFile(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')

test('tool summaries keep a small group readable and count names omitted from large groups', () => {
  const tools = ['Read', 'Bash', 'Search', 'Write', 'Fetch'].map(name => ({ name }))
  assert.equal(toolGroupNames([]), '')
  assert.equal(toolGroupNames(tools.slice(0, 1)), 'Read')
  assert.equal(toolGroupNames(tools.slice(0, 3)), 'Read, Bash, Search')
  assert.equal(toolGroupNames(tools), 'Read, Bash, Search · +2 more')
})

test('summary construction reads at most three names regardless of group size', () => {
  let reads = 0
  const tools = Array.from({ length: 10000 }, (_, n) => ({
    get name() { reads++; return `Tool${n}` },
  }))
  assert.equal(toolGroupNames(tools), 'Tool0, Tool1, Tool2 · +9997 more')
  assert.equal(reads, 3)
})

test('long names are bounded in the summary without changing the underlying tool data', () => {
  const name = 'x'.repeat(10000)
  const tools = [{ name }, { name }, { name }, { name }]
  const summary = toolGroupNames(tools)
  assert.ok(summary.length < 165)
  assert.equal((summary.match(/…/g) ?? []).length, 3)
  assert.equal(tools[0].name, name)
})

test('tool status labels remain explicit without relying on animation or color', () => {
  assert.deepEqual(['running', 'completed', 'failed', 'cancelled'].map(toolStatusLabel),
    ['Running', 'Completed', 'Failed', 'Cancelled'])
})

test('main and child tool bodies are gated by the native details state', () => {
  for (const part of ['part', 'sub']) {
    assert.ok(pane.includes(`:open="expandedToolGroups.get(turn.key)?.has(${part}.key)"`))
    assert.ok(pane.includes(`@toggle="onToolGroupToggle(turn, ${part}.key, $event)"`))
    assert.match(pane, new RegExp(`<template v-if="expandedToolGroups\\.get\\(turn\\.key\\)\\?\\.has\\(${part}\\.key\\)">\\s*<div\\s+v-for="tool in ${part}\\.tools"`))
  }
  const memo = pane.match(/v-memo="\[([^\]]*)\]"/)[1]
  assert.ok(memo.includes('expandedToolGroups.get(turn.key)'), 'history folds must invalidate their own memo')
  assert.match(pane, /shallowReactive\(new Map<string, ReadonlySet<string>>\(\)\)/)
  assert.doesNotMatch(pane, /expandedToolGroups\.get\(turn\)/,
    'visibleTurns copies turn objects on streaming updates; expansion must use a stable key')
})

test('tool data stays plain text and both scrollable inputs and results support keyboard focus', () => {
  for (const field of ['argsText', 'resultText']) {
    const label = field === 'argsText' ? 'Tool input' : 'Tool result'
    const matches = pane.match(new RegExp(`<pre\\s+tabindex="0"\\s+aria-label="${label}"\\s*>\\{\\{ tool\\.${field} \\}\\}</pre>`, 'g'))
    assert.equal(matches?.length, 2)
  }
  const styles = pane.slice(pane.indexOf('.tool-header {'), pane.indexOf('.tool-name {', pane.indexOf('.tool-header {')))
  assert.doesNotMatch(styles, /animation\s*:/, 'large tool histories must not keep status animations running')
  assert.match(pane, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.tool-chevron,[\s\S]*\.tool-header\s*\{\s*transition: none/)
})
