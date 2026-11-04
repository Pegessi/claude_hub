import assert from 'node:assert/strict'
import { readFile, readdir, access } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(import.meta.url)
const { parse, compileScript, compileStyle } = require('vue/compiler-sfc')
const featureRoot = new URL('../src/features/workspace/', import.meta.url)

async function filesBelow(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  return (await Promise.all(entries.map(entry => {
    const url = new URL(entry.name + (entry.isDirectory() ? '/' : ''), directory)
    return entry.isDirectory() ? filesBelow(url) : [url]
  }))).flat()
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
  for (const file of await filesBelow(featureRoot)) {
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

async function componentStyles(name) {
  const file = new URL(`components/${name}.vue`, featureRoot)
  const { descriptor } = parse(await readFile(file, 'utf8'))
  return (await Promise.all(descriptor.styles.map(style => style.src
    ? readFile(new URL(style.src, file), 'utf8')
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
