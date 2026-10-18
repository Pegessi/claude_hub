import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { extname } from 'node:path'
import ts from 'typescript'
import * as vue from 'vue'

const require = createRequire(import.meta.url)
const { parse, compileScript, compileTemplate } = require('vue/compiler-sfc')
const sourceRoot = new URL('../src/', import.meta.url)
const moduleUrls = new Map()

async function sourceModuleUrl(url) {
  if (moduleUrls.has(url.href)) return moduleUrls.get(url.href)
  let source = await readFile(url, 'utf8')
  if (url.pathname.endsWith('.vue')) {
    const { descriptor } = parse(source, { filename: url.pathname })
    const script = compileScript(descriptor, { id: url.pathname, genDefaultAs: '__component' })
    const template = compileTemplate({
      source: descriptor.template?.content || '', filename: url.pathname, id: url.pathname,
      compilerOptions: { bindingMetadata: script.bindings },
    })
    if (template.errors.length) throw new Error(template.errors.join('\n'))
    source = `${script.content}\n${template.code}\n__component.render = render\nexport default __component`
  }
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  })
  const specifiers = [...new Set([...outputText.matchAll(/from\s+['"]([^'"]+)['"]/g)].map(match => match[1]))]
  for (const specifier of specifiers) {
    let target
    if (specifier.startsWith('@/') || specifier.startsWith('.')) {
      let path = specifier.startsWith('@/') ? new URL(specifier.slice(2), sourceRoot) : new URL(specifier, url)
      if (!extname(path.pathname)) path = new URL(`${path.href}.ts`)
      target = await sourceModuleUrl(path)
    } else {
      target = import.meta.resolve(specifier)
    }
    outputText = outputText.replaceAll(`'${specifier}'`, JSON.stringify(target)).replaceAll(`"${specifier}"`, JSON.stringify(target))
  }
  const compiled = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(url.href, compiled)
  return compiled
}

export async function loadWorkspaceFeature(path) {
  return import(await sourceModuleUrl(new URL(`features/workspace/${path}`, sourceRoot)))
}

export function mountWorkspaceConsumer(t, component, provide, props = {}) {
  const body = { tag: 'body', children: [] }
  const renderer = vue.createRenderer({
    createElement: tag => ({ tag, children: [], props: {} }),
    createText: text => ({ text }), createComment: text => ({ comment: text }),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text; node.children = [] },
    patchProp(node, name, _previous, value) { node.props[name] = value },
    insert(node, parent, anchor = null) {
      if (node.parent) {
        const oldIndex = node.parent.children.indexOf(node)
        if (oldIndex >= 0) node.parent.children.splice(oldIndex, 1)
      }
      node.parent = parent
      parent.children ??= []
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(node)
      else parent.children.splice(index, 0, node)
    },
    remove(node) {
      const index = node.parent?.children.indexOf(node) ?? -1
      if (index >= 0) node.parent.children.splice(index, 1)
    },
    parentNode: node => node.parent ?? null,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    querySelector: selector => selector === 'body' ? body : null,
  })
  let state
  const app = renderer.createApp({
    setup() { state = provide(); return () => vue.h(component, props) },
  })
  const root = { tag: 'root', children: [] }
  app.mount(root)
  let mounted = true
  const unmount = () => {
    if (!mounted) return
    mounted = false
    app.unmount()
  }
  t.after(unmount)
  function find(predicate, node = root) {
    if (predicate(node)) return node
    for (const child of node.children || []) {
      const found = find(predicate, child)
      if (found) return found
    }
    return null
  }
  return { root, body, state, find, unmount }
}

export { vue }
