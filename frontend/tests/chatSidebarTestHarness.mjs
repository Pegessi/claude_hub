import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'
import { createRenderer } from 'vue'

const require = createRequire(import.meta.url)
export const { parse, compileScript, compileStyle } = require('vue/compiler-sfc')
export const source = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

/** Load real modules, keeping compatibility exports and component identity intact. */
export function createSidebarLoader(overrides = {}) {
  const cache = new Map()
  function urlFor(path) {
    if (cache.has(path)) return cache.get(path)
    const file = new URL(`../src/${path}`, import.meta.url)
    let content = overrides[path] ?? source(path)
    if (path.endsWith('.vue')) {
      const { descriptor } = parse(content, { filename: path })
      content = compileScript(descriptor, { id: 'sidebar-test', inlineTemplate: true }).content
    }
    let { outputText } = ts.transpileModule(content, {
      compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
    })
    for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
      let dependency
      if (specifier.startsWith('@/')) dependency = specifier.slice(2)
      else if (specifier.startsWith('.')) dependency = new URL(specifier, file).pathname.split('/frontend/src/')[1]
      if (dependency && !/\.(ts|vue)$/.test(dependency)) dependency += '.ts'
      const url = dependency ? urlFor(dependency) : import.meta.resolve(specifier)
      outputText = outputText.replaceAll(`'${specifier}'`, JSON.stringify(url)).replaceAll(`"${specifier}"`, JSON.stringify(url))
    }
    const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
    cache.set(path, url)
    return url
  }
  return path => import(urlFor(path))
}

export class TestElement {
  constructor(tag = 'div') {
    this.tag = tag
    this.tagName = tag.toUpperCase()
    this.children = []
    this.attributes = {}
    this.listeners = new Map()
    this.dataset = {}
    this.rect = { top: 0, height: 40 }
  }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set())
    this.listeners.get(name).add(listener)
  }
  removeEventListener(name, listener) { this.listeners.get(name)?.delete(listener) }
  dispatchEvent(event) {
    if (!event.target) Object.defineProperty(event, 'target', { value: this })
    for (const fn of this.listeners.get(event.type) ?? []) fn(event)
  }
  setAttribute(name, value) { this.attributes[name] = value }
  getAttribute(name) { return this.attributes[name] }
  removeAttribute(name) { delete this.attributes[name] }
  getBoundingClientRect() { return this.rect }
  focus() { this.focused = true }
  select() { this.selected = true }
  closest(selector) {
    if (selector === '.chat-sidebar__group') return this.group ?? null
    if (selector === '.chat-sidebar__item') return this.row ?? null
    if (selector === '[data-pointer-drop="pin"]') return this.pin ?? null
    if (selector === '.chat-sidebar__item-menu, .chat-sidebar__rename-input') return this.control ?? null
    return null
  }
  querySelector() { return this.header ?? null }
}

export function makeRenderer() {
  const teleportTarget = new TestElement('body')
  return createRenderer({
    querySelector: () => teleportTarget,
    createElement: tag => new TestElement(tag), createText: text => ({ text }), createComment: comment => ({ comment }),
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text },
    patchProp(node, name, _old, value) { node.attributes[name] = value },
    insert(node, parent, anchor = null) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
      node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(node)
      else parent.children.splice(index, 0, node)
    },
    remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1) },
    parentNode: node => node.parent ?? null,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  })
}

export function mountController(t, setup) {
  let api
  const app = makeRenderer().createApp({ setup() { api = setup(); return () => null } })
  app.mount({ children: [] })
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  t.after(unmount)
  return { api, unmount }
}

export function installBrowser(t) {
  const previous = new Map()
  const set = (key, value) => { previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key)); Object.defineProperty(globalThis, key, { configurable: true, writable: true, value }) }
  const listeners = new Map(), timers = new Map(), classes = new Set(), storage = new Map()
  let timerId = 0, hit = null
  const window = {
    addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn) },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn) },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId },
    clearTimeout(id) { timers.delete(id) },
  }
  const body = { classList: { add: name => classes.add(name), remove: name => classes.delete(name) } }
  set('window', window)
  set('document', { body, elementFromPoint: () => hit, activeElement: null })
  set('localStorage', { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) })
  set('HTMLElement', TestElement)
  set('HTMLInputElement', TestElement)
  t.after(() => { for (const [key, descriptor] of previous) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key] } })
  return {
    listeners, timers, classes, storage,
    hit(element) { hit = element },
    fire(name, event) { for (const fn of [...listeners.get(name) ?? []]) fn(event) },
    flushTimers() { for (const [id, fn] of [...timers]) { timers.delete(id); fn() } },
  }
}

export const pointer = (extra = {}) => ({
  button: 0, pointerId: 1, isPrimary: true, clientX: 0, clientY: 0, target: new TestElement(),
  preventDefault() { this.prevented = true }, ...extra,
})
