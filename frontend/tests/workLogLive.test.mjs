import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'
import * as vue from 'vue'

const require = createRequire(import.meta.url)
const { parse, compileScript } = require('vue/compiler-sfc')
const root = new URL('../src/', import.meta.url)

/** Load real .vue/.ts modules (compiled, not stubbed) so the component's own
 *  reactivity graph is what the test exercises. */
function moduleLoader() {
  const urls = new Map()
  function urlFor(path) {
    if (urls.has(path)) return urls.get(path)
    const file = new URL(path, root)
    let source = readFileSyncSafe(file)
    if (path.endsWith('.vue')) {
      const descriptor = parse(source, { filename: path }).descriptor
      source = compileScript(descriptor, { id: path, inlineTemplate: true }).content
    }
    let { outputText } = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
    })
    for (const [, specifier] of outputText.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
      let target
      if (specifier.startsWith('@/')) target = specifier.slice(2)
      else if (specifier.startsWith('.')) target = new URL(specifier, file).pathname.slice(root.pathname.length)
      const resolved = target
        ? urlFor(/\.(vue|ts)$/.test(target) ? target : `${target}.ts`)
        : import.meta.resolve(specifier)
      outputText = outputText
        .replaceAll(`'${specifier}'`, JSON.stringify(resolved))
        .replaceAll(`"${specifier}"`, JSON.stringify(resolved))
    }
    const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
    urls.set(path, url)
    return url
  }
  return async path => import(urlFor(path))
}

function readFileSyncSafe(file) {
  return require('node:fs').readFileSync(file, 'utf8')
}

/** Minimal DOM: enough for a text-only component tree, and it keeps the
 *  rendered text addressable so assertions read what a user would see. */
function createRenderer() {
  const create = (tag, text) => ({
    tag, text, props: {}, children: [], parent: null,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
    focus() {}, querySelectorAll() { return [] },
    getBoundingClientRect() { return { top: 0, bottom: 0 } },
  })
  const body = create('body')
  return { body, renderer: vue.createRenderer({
    createElement: tag => create(tag, ''),
    createText: text => create('#text', text),
    createComment: text => create('#comment', text),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text },
    patchProp(node, name, _old, value) { node.props[name] = value },
    insert(node, parent, anchor = null) {
      if (node.parent) {
        const i = node.parent.children.indexOf(node)
        if (i >= 0) node.parent.children.splice(i, 1)
      }
      node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index >= 0) parent.children.splice(index, 0, node)
      else parent.children.push(node)
    },
    remove(node) {
      const i = node.parent?.children.indexOf(node) ?? -1
      if (i >= 0) node.parent.children.splice(i, 1)
      node.parent = null
    },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    querySelector: () => body,
  }) }
}

const flush = async () => {
  await vue.nextTick()
  await new Promise(resolve => setImmediate(resolve))
  await vue.nextTick()
}

/** Text a reader would see: walk the vnode tree, skipping comments. Static
 *  text lives on the vnode's string children, interpolations on Text vnodes. */
function renderedText(vnode) {
  if (!vnode || vnode.type === vue.Comment) return ''
  if (typeof vnode.children === 'string') return vnode.children
  const own = Array.isArray(vnode.children) ? '' : (typeof vnode.children === 'number' ? String(vnode.children) : '')
  const children = Array.isArray(vnode.children)
    ? vnode.children.filter(child => typeof child === 'object').map(renderedText).join('')
    : ''
  const nested = vnode.component ? renderedText(vnode.component.subTree) : ''
  return `${own}${children}${nested}`
}

const event = (sequence, type, payload = {}, extra = {}) => ({
  stream_sequence: sequence, type, payload, session_id: 's', tab_id: 't',
  turn_id: 'turn', agent_type: 'claude', created_at: '2026-11-09T10:00:00Z',
  redacted: false, ...extra,
})

/** Mount the real footer over a real reducer, the way the pane drives it:
 *  every batch replaces `turns` with a fresh array whose turn objects were
 *  mutated in place by ``IncrementalTimelineReducer``. */
async function mountFooter() {
  const load = moduleLoader()
  const ChatTurnWorking = (await load('features/chat/components/ChatTurnWorking.vue')).default
  const owners = await load('features/chat/owners.ts')
  const reducerModule = await load('features/chat/timeline/reducer.ts')
  const turns = vue.ref([])
  const reducer = new reducerModule.IncrementalTimelineReducer()
  const { renderer } = createRenderer()
  const app = renderer.createApp({
    setup() {
      vue.provide(owners.chatTimelineKey, { turns })
      return () => vue.h(ChatTurnWorking, { startedAt: new Date(Date.now() - 65_000).toISOString() })
    },
  })
  const vm = app.mount({ children: [], parent: null })
  return {
    turns,
    async apply(batch) {
      turns.value = reducer.reduce([...batch])
      await flush()
      return renderedText(vm.$.subTree).replace(/\s+/g, ' ').trim()
    },
    unmount() { app.unmount() },
  }
}

test('a streaming turn updates the footer on every batch, not just the first', async t => {
  // The reducer mutates each turn in place and returns a fresh array, so a
  // computed that returns the *turn object* never changes identity and Vue
  // never re-renders the footer: a long turn would sit on its first label for
  // the whole duration. This test drives the same pipeline the pane drives.
  const footer = await mountFooter()
  t.after(() => footer.unmount())

  const events = [event(1, 'turn_started', { summary: 'investigate' })]
  events.push(event(2, 'thinking_delta', { text: 'look at the reducer' }))
  const first = await footer.apply(events)
  assert.match(first, /Working/, first)
  assert.match(first, /· Thinking/, first)

  events.push(event(3, 'tool_call_started', { tool_call_id: 'r1', name: 'Read', args: { file_path: 'src/a.ts' } }))
  const second = await footer.apply(events)
  assert.match(second, /· Reading src\/a\.ts/, second)
  assert.match(second, /1 read\b/, second)
  assert.notEqual(second, first, 'the footer must not stay on the first batch')

  events.push(event(4, 'tool_call_started', { tool_call_id: 'r2', name: 'Read', args: { file_path: 'src/b.ts' } }))
  const third = await footer.apply(events)
  assert.match(third, /Reading 2 files/, third)
  assert.match(third, /2 reads\b/, third)

  events.push(event(5, 'tool_call_completed', { tool_call_id: 'r1', status: 'failed' }))
  const fourth = await footer.apply(events)
  assert.match(fourth, /1 failed/, fourth)
  assert.notEqual(fourth, third)
})

test('a turn that finishes while the footer is up stops claiming progress', async t => {
  const footer = await mountFooter()
  t.after(() => footer.unmount())

  const events = [event(1, 'turn_started', { summary: 'investigate' })]
  events.push(event(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: { command: 'pnpm test' } }))
  const running = await footer.apply(events)
  assert.match(running, /Running `pnpm test`/, running)

  // A failure while still running is reported without hiding the step.
  events.push(event(3, 'tool_call_completed', { tool_call_id: 'c1', status: 'failed' }))
  const failed = await footer.apply(events)
  assert.match(failed, /· Thinking/, failed)
  assert.match(failed, /1 command · 1 failed/, failed)
})
