import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature, mountWorkspaceConsumer, vue } from './workspaceFeatureTestHarness.mjs'

const { default: WorkspacePicker } = await loadWorkspaceFeature('components/WorkspacePicker.vue')

const ITEMS = [
  { id: 'ws-1', name: 'Workspace One' },
  { id: 'ws-2', name: 'Workspace Two' },
]

function walk(node, fn) {
  fn(node)
  for (const child of node.children || []) walk(child, fn)
}

function findAll(root, predicate) {
  const matches = []
  walk(root, node => { if (predicate(node)) matches.push(node) })
  return matches
}

function textOf(node) {
  if (typeof node.text === 'string') return node.text
  return (node.children || []).map(textOf).join('')
}

const hasClass = name => node =>
  typeof node.props?.class === 'string' && node.props.class.split(/\s+/).includes(name)

// Minimal document/Node globals for the picker's open-scoped listeners. They
// live for the whole file so component unmount (which also releases listeners)
// never runs after the stubs are removed.
const listeners = new Map()
class FakeNode {}
const documentStub = {
  addEventListener(type, fn) { listeners.set(type, fn) },
  removeEventListener(type) { listeners.delete(type) },
}

test.before(() => {
  Object.defineProperty(globalThis, 'document', {
    configurable: true, writable: true, value: documentStub,
  })
  Object.defineProperty(globalThis, 'Node', {
    configurable: true, writable: true, value: FakeNode,
  })
})

test.after(() => {
  delete globalThis.document
  delete globalThis.Node
})

test.beforeEach(() => listeners.clear())

function mountPicker(t, props = {}, provide = () => ({})) {
  const calls = []
  const mounted = mountWorkspaceConsumer(
    t,
    WorkspacePicker,
    provide,
    {
      items: ITEMS,
      selectedId: 'ws-1',
      onSelect: id => calls.push(['select', id]),
      onDelete: workspace => calls.push(['delete', workspace]),
      ...props,
    }
  )
  const trigger = () => findAll(mounted.root, hasClass('workspace-picker__trigger'))[0]
  const panel = () => findAll(mounted.root, hasClass('workspace-picker__panel'))[0] || null
  const options = () => findAll(mounted.root, hasClass('workspace-picker__option'))
  const deleteButtons = () => findAll(mounted.root, hasClass('workspace-picker__delete'))
  return { ...mounted, calls, trigger, panel, options, deleteButtons }
}

test('renders the selected workspace on the closed trigger', (t) => {
  const view = mountPicker(t)
  assert.equal(view.trigger().props['aria-expanded'], false)
  assert.equal(view.trigger().props['aria-haspopup'], 'menu')
  assert.equal(textOf(view.trigger()), 'Workspace One▾')
  assert.equal(view.panel(), null)
})

test('opening lists every workspace with select and delete controls', async (t) => {
  const view = mountPicker(t)
  view.trigger().props.onClick({})
  await vue.nextTick()
  assert.equal(view.trigger().props['aria-expanded'], true)
  const panel = view.panel()
  assert.equal(panel.props.role, 'menu')
  assert.equal(panel.props['aria-label'], 'Workspaces')
  assert.equal(view.options().length, 2)
  assert.deepEqual(view.options().map(option => option.props['aria-checked']), [true, false])
  assert.deepEqual(
    view.deleteButtons().map(button => button.props['aria-label']),
    ['Delete workspace "Workspace One"', 'Delete workspace "Workspace Two"']
  )
})

test('selecting another workspace emits select once and closes; current row is a no-op', async (t) => {
  const view = mountPicker(t)
  view.trigger().props.onClick({})
  await vue.nextTick()
  view.options()[1].props.onClick({})
  await vue.nextTick()
  assert.deepEqual(view.calls, [['select', 'ws-2']])
  assert.equal(view.panel(), null)

  view.trigger().props.onClick({})
  await vue.nextTick()
  view.options()[0].props.onClick({})
  await vue.nextTick()
  assert.deepEqual(view.calls, [['select', 'ws-2']])
})

test('delete click closes the popover and reports the workspace to the parent', async (t) => {
  const view = mountPicker(t)
  view.trigger().props.onClick({})
  await vue.nextTick()
  view.deleteButtons()[1].props.onClick({ stopPropagation() {} })
  await vue.nextTick()
  assert.equal(view.panel(), null)
  assert.deepEqual(view.calls, [['delete', { id: 'ws-2', name: 'Workspace Two' }]])
})

test('Escape and outside pointerdown dismiss without emitting; inside clicks stay open', async (t) => {
  const view = mountPicker(t)
  view.trigger().props.onClick({})
  await vue.nextTick()
  const panel = view.panel()
  panel.contains = target => target === panel
  listeners.get('keydown')({ key: 'Escape', stopPropagation() {} })
  await vue.nextTick()
  assert.equal(view.panel(), null)
  assert.deepEqual(view.calls, [])

  view.trigger().props.onClick({})
  await vue.nextTick()
  const inside = new FakeNode()
  view.trigger().contains = () => false
  view.panel().contains = target => target === inside
  listeners.get('pointerdown')({ target: inside })
  await vue.nextTick()
  assert.ok(view.panel(), 'pointerdown inside the panel keeps it open')

  const outside = new FakeNode()
  view.panel().contains = () => false
  listeners.get('pointerdown')({ target: outside })
  await vue.nextTick()
  assert.equal(view.panel(), null)
  assert.deepEqual(view.calls, [])
})

test('a disabled trigger cannot open while switching', async (t) => {
  const view = mountPicker(t, { disabled: true })
  view.trigger().props.onClick({})
  await vue.nextTick()
  assert.equal(view.panel(), null)
})

test('deleting state disables the per-row delete buttons', async (t) => {
  const view = mountPicker(t, { deleting: true })
  view.trigger().props.onClick({})
  await vue.nextTick()
  assert.ok(view.deleteButtons().every(button => button.props.disabled === true))
})

test('zero workspaces shows the empty-state message and no rows', async (t) => {
  const view = mountPicker(t, { items: [], selectedId: null })
  assert.equal(textOf(view.trigger()), 'Select workspace▾')
  view.trigger().props.onClick({})
  await vue.nextTick()
  assert.equal(view.options().length, 0)
  assert.equal(view.deleteButtons().length, 0)
  const empty = findAll(view.root, hasClass('workspace-picker__empty'))[0]
  assert.equal(textOf(empty).trim(), 'No workspaces yet. Create one with New Workspace.')
})

test('document listeners are bound while open and released on close and unmount', async (t) => {
  const view = mountPicker(t)
  assert.deepEqual([...listeners.keys()], [])
  view.trigger().props.onClick({})
  await vue.nextTick()
  assert.deepEqual([...listeners.keys()].sort(), ['keydown', 'pointerdown'])
  view.unmount()
  assert.deepEqual([...listeners.keys()], [])
})
