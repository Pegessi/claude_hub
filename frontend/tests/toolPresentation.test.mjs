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
const toolGroup = await readFile(new URL('../src/features/chat/components/ChatToolGroup.vue', import.meta.url), 'utf8')
const subthread = await readFile(new URL('../src/features/chat/components/ChatSubthread.vue', import.meta.url), 'utf8')
const timeline = await readFile(new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url), 'utf8')
const controller = await readFile(new URL('../src/features/chat/controllers/useChatTimeline.ts', import.meta.url), 'utf8')

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

test('main and child tool bodies are gated by native details state', () => {
  assert.match(toolGroup, /:open="expandedToolGroups\.get\(turn\.key\)\?\.has\(part\.key\)"/)
  assert.match(toolGroup, /@toggle="onToolGroupToggle\(turn, part\.key, \$event\)"/)
  assert.match(toolGroup, /<template v-if="expandedToolGroups\.get\(turn\.key\)\?\.has\(part\.key\)">/)
  assert.match(subthread, /:open="expandedToolGroups\.get\(turn\.key\)\?\.has\(sub\.key\)"/)
  assert.match(subthread, /@toggle="onToolGroupToggle\(turn, sub\.key, \$event\)"/)
  assert.match(subthread, /<template v-if="expandedToolGroups\.get\(turn\.key\)\?\.has\(sub\.key\)">/)
  const memo = timeline.match(/v-memo="\[([^\]]*)\]"/)[1]
  assert.match(memo, /expandedToolGroups\.get\(turn\.key\)/)
  assert.match(controller, /shallowReactive\(new Map<string, ReadonlySet<string>>\(\)\)/)
  assert.doesNotMatch(controller, /expandedToolGroups\.get\(turn\)/)
})

test('tool data stays plain text and scrollable inputs/results support keyboard focus', () => {
  for (const component of [toolGroup, subthread]) {
    assert.match(component, /<pre\s+tabindex="0"\s+aria-label="Tool input"\s*>\{\{ tool\.argsText \}\}<\/pre>/)
    assert.match(component, /<pre\s+tabindex="0"\s+aria-label="Tool result"\s*>\{\{ tool\.resultText \}\}<\/pre>/)
    const styles = component.slice(component.indexOf('.tool-header {'), component.indexOf('.tool-name {', component.indexOf('.tool-header {')))
    assert.doesNotMatch(styles, /animation\s*:/)
    assert.match(component, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.tool-chevron,[\s\S]*\.tool-header\s*\{\s*transition: none/)
  }
})
