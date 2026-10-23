import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, readdir, access } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const { parse, compileScript, compileStyle } = require('vue/compiler-sfc')
const compilerRequire = createRequire(createRequire(require.resolve('vue/compiler-sfc')).resolve('@vue/compiler-sfc'))
const postcss = compilerRequire('postcss')
const selectorParser = compilerRequire('postcss-selector-parser')
const featureRoot = new URL('../src/features/workspace/', import.meta.url)


async function filesBelow(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  return (await Promise.all(entries.map(entry => {
    const url = new URL(entry.name + (entry.isDirectory() ? '/' : ''), directory)
    return entry.isDirectory() ? filesBelow(url) : [url]
  }))).flat()
}
const files = await filesBelow(featureRoot)
const descriptors = new Map()
for (const file of files.filter(file => file.pathname.endsWith('.vue'))) {
  descriptors.set(file.pathname.split('/').at(-1).slice(0, -4), parse(await readFile(file, 'utf8')).descriptor)
}

function expressionTokens(expression) {
  const source = ts.createSourceFile('expression.ts', `(${expression})`, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const tokens = []
  function visit(node) {
    const children = node.getChildren(source)
    if (children.length) children.forEach(visit)
    else tokens.push([node.kind, node.getText(source)])
  }
  visit(source)
  return tokens
}

function nativeTree(node) {
  if (node.type === 0) return node.children.flatMap(nativeTree)
  if (node.type === 1) {
    if (descriptors.has(node.tag)) return nativeTree(descriptors.get(node.tag).template.ast)
    return [{
      tag: node.tag,
      props: node.props.map(prop => prop.type === 6
        ? [prop.name, prop.value?.content ?? null]
        : [prop.name, prop.arg?.content ?? null, prop.exp ? expressionTokens(prop.exp.content) : null, prop.modifiers.map(modifier => modifier.content ?? modifier)]),
      children: node.children.flatMap(nativeTree),
    }]
  }
  if (node.type === 2) {
    const text = node.content.replace(/\s+/g, ' ').trim()
    return text ? [['text', text]] : []
  }
  if (node.type === 5) return [['expression', expressionTokens(node.content.content)]]
  return []
}
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')

function stylesheetContracts(source) {
  const contracts = []
  const root = postcss.parse(source)
  root.walkRules(rule => {
    const parents = []
    for (let parent = rule.parent; parent?.type === 'atrule'; parent = parent.parent) {
      parents.unshift([parent.name, parent.params])
    }
    const declarations = rule.nodes.filter(node => node.type === 'decl').map(node => [node.prop, node.value, Boolean(node.important)])
    selectorParser(selectors => selectors.each(selector => {
      contracts.push(JSON.stringify([parents, selector.toString().trim(), declarations]))
    })).processSync(rule.selector)
  })
  return contracts
}

test('workspace entry re-exports the feature component without another lifecycle owner', async () => {
  const source = await readFile(new URL('../src/components/AgentWorkspaceView.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  assert.equal(descriptor.template, null)
  assert.equal(descriptor.scriptSetup, null)
  assert.equal(descriptor.styles.length, 0)
  assert.match(compileScript(descriptor, { id: 'workspace-alias' }).content, /export\s*\{\s*default\s*\}\s*from ['"]@\/features\/workspace\/WorkspacePage\.vue['"]/)
})

test('workspace production files respect the hard line budget and own scoped styles', async () => {
  for (const file of files) {
    const source = await readFile(file, 'utf8')
    assert.ok(source.trimEnd().split('\n').length <= 2500, file.pathname)
    if (!file.pathname.endsWith('.vue')) continue
    const { descriptor } = parse(source)
    for (const style of descriptor.styles) {
      assert.equal(style.scoped, true, file.pathname)
      if (style.src) {
        assert.ok(style.src.includes('styles/'), file.pathname)
        assert.ok(new URL(style.src, file).href.startsWith(featureRoot.href))
      }
    }
  }
  for (const name of ['workspaceFeedback', 'workspaceReports', 'workspaceProgress', 'workspaceResident']) {
    await assert.rejects(access(new URL(`../src/utils/${name}.ts`, import.meta.url)))
  }
})

// Characterization of the accepted cf16254 view: component boundaries may change,
// while native elements, conditions, handlers, Teleports and rendered text stay the same.
test('workspace component expansion preserves the accepted native template', () => {
  assert.equal(digest(nativeTree(descriptors.get('WorkspacePage').template.ast)), '1723451c1e6d0501164f5e1aa6c0c9cd84b815f121931b18b73469f1c9c215c6')
})

test('workspace scoped styles retain every accepted selector and declaration', async () => {
  const rules = new Set()
  for (const file of files.filter(file => file.pathname.endsWith('.css'))) {
    for (const contract of stylesheetContracts(await readFile(file, 'utf8'))) rules.add(contract)
  }
  for (const descriptor of descriptors.values()) {
    for (const style of descriptor.styles.filter(style => !style.src)) {
      for (const contract of stylesheetContracts(style.content)) rules.add(contract)
    }
  }
  assert.equal(digest([...rules].sort()), 'dc1112f1c0ee2eadfe17f8f652270895eb55c6b4c15c62ffa9b1cd8b9a0d60f1')
})

async function componentStyles(name) {
  const descriptor = descriptors.get(name)
  const directory = new URL(name === 'WorkspacePage' ? './' : 'components/', featureRoot)
  return (await Promise.all(descriptor.styles.map(style => style.src
    ? readFile(new URL(style.src, directory), 'utf8')
    : style.content))).join('\n')
}

test('responsive rules compile for their actual component owners', async () => {
  const actions = await componentStyles('TaskDetailActions')
  const parent = await componentStyles('TaskDetailDialog')
  const board = await componentStyles('WorkspaceBoard')
  const editor = await componentStyles('WorkspaceEditorDialog')
  assert.match(actions, /\.task-detail-panel\.mobile-actions-expanded \.detail-action-drawer/)
  assert.doesNotMatch(parent, /\.detail-action-drawer/)
  assert.doesNotMatch(board, /\.task-detail-panel|\.task-detail-overlay/)
  assert.doesNotMatch(editor, /\.task-column|\.task-detail-panel/)
  const compiled = compileStyle({ source: actions, filename: 'TaskDetailActions.css', id: 'data-v-actions', scoped: true })
  assert.deepEqual(compiled.errors, [])
  assert.match(compiled.code, /\.task-detail-panel\.mobile-actions-expanded \.detail-action-drawer\[data-v-actions\]/)
  assert.doesNotMatch(compiled.code, /\.task-detail-panel\.mobile-actions-expanded\[data-v-actions\]/)
})

export { nativeTree, stylesheetContracts, digest }
