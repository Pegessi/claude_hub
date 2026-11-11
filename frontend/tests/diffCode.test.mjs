import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'
import * as vue from 'vue'

// ── Load the real ChatToolGroup, and give it a DOM it can render into ──────
// The diff card is the surface under test, so the component is compiled from
// source (not stubbed): the rows, the header chip and the highlighted HTML have
// to come out of the code that ships. Only the DOM is faked — a renderer whose
// elements keep their props, which is where ``v-html`` output lands.
const require = createRequire(import.meta.url)
const { parse, compileScript } = require('vue/compiler-sfc')
const root = new URL('../src/', import.meta.url)

function moduleLoader() {
  const urls = new Map()
  function urlFor(path) {
    if (urls.has(path)) return urls.get(path)
    const file = new URL(path, root)
    let source = readFileSync(file, 'utf8')
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
    // codeHighlight.ts loads highlight.js through a dynamic import; a `data:`
    // module has no base to resolve a bare specifier against, so resolve it here
    // (the chunk is loaded for real, which is what the ceiling test needs).
    outputText = outputText.replace(
      /import\(\s*(['"])([^'"]+)\1\s*\)/g,
      (whole, _quote, specifier) => (specifier.startsWith('.')
        ? whole
        : `import(${JSON.stringify(import.meta.resolve(specifier))})`),
    )
    const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
    urls.set(path, url)
    return url
  }
  return async path => import(urlFor(path))
}

/** One loader for the whole file: production loads the highlighter chunk once,
 *  so the later mounts are the "chunk already there" case. */
const load = moduleLoader()

function createRenderer() {
  const create = (tag, text) => ({
    tag, text, props: {}, children: [], parent: null,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {},
    focus() {}, querySelectorAll() { return [] },
    getBoundingClientRect() { return { top: 0, bottom: 0 } },
  })
  const body = create('body')
  return vue.createRenderer({
    createElement: tag => create(tag, ''),
    createText: text => create('#text', text),
    createComment: text => create('#comment', text),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text },
    patchProp(node, name, _old, value) { node.props[name] = value },
    insert(node, parent, anchor = null) {
      if (node.parent) {
        const stale = node.parent.children.indexOf(node)
        if (stale >= 0) node.parent.children.splice(stale, 1)
      }
      node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index >= 0) parent.children.splice(index, 0, node)
      else parent.children.push(node)
    },
    remove(node) {
      const index = node.parent ? node.parent.children.indexOf(node) : -1
      if (index >= 0) node.parent.children.splice(index, 1)
      node.parent = null
    },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    querySelector: () => body,
  })
}

const flush = async () => {
  await vue.nextTick()
  await new Promise(resolve => setImmediate(resolve))
  await vue.nextTick()
}

/** Every element under `node` whose class list names `className`, in reading order. */
function collect(node, className, found = []) {
  if (!node || typeof node !== 'object') return found
  const raw = node.props?.class
  const classes = Array.isArray(raw) ? raw.join(' ') : (typeof raw === 'string' ? raw : '')
  if (classes.split(' ').includes(className)) found.push(node)
  for (const child of node.children ?? []) collect(child, className, found)
  return found
}

/** A patch whose every hunk names a Python file, so each row has a language. */
const PYTHON_PATCH = [
  '*** Begin Patch',
  '*** Update File: backend/claude_hub/services/terminal_runtime/manager/status.py',
  '@@ -236,8 +236,11 @@ def native_runtime_snapshot(runtime):',
  '     if runtime is None:',
  '         return {"state": "offline", "detail": None}',
  '-    return {"state": runtime.state, "detail": runtime.detail}',
  '+    snapshot = {"state": runtime.state, "detail": runtime.detail}',
  '+    snapshot["pending_question"] = runtime.has_pending_question()',
  '+    return snapshot',
  ' ',
  ' ',
  ' def effective_status(runtime):',
  '     snapshot = native_runtime_snapshot(runtime)',
  // Tool arguments are untrusted, so a row may contain anything at all.
  '+    label = f"<script>alert(1)</script>"',
  '+    return label',
  // An extension the bundled highlighter has no grammar for: plain text, no chip.
  '*** Update File: /tmp/claude_hub/run.log',
  '@@ -1,2 +1,2 @@',
  '-migrate: start',
  '-migrate: idle',
  '+migrate: done',
  '+migrate: clean',
  '*** End Patch',
].join('\n')

/** A patch of `contentRows` added lines. The diff builder caps a diff at 400
 *  rows (a truncation row is appended when it overflows), so 398 content rows is
 *  exactly 400 rows and 399 is the first size over the ceiling. */
function pythonRowsPatch(contentRows) {
  return [
    '*** Begin Patch',
    '*** Update File: backend/claude_hub/services/terminal_runtime/manager/status.py',
    '@@ -1,1 +1,400 @@',
    ...Array.from({ length: contentRows }, (_, i) => `+    return compute(${i})`),
    '*** End Patch',
  ].join('\n')
}

/** Rows in a rendered diff: file headers and code rows together, which is the
 *  count the card's highlighting ceiling is measured against. */
function diffRowCount(cardRoot) {
  return collect(cardRoot, 'activity-diff-file').length + collect(cardRoot, 'activity-diff-line').length
}

/** Mount the real card over `patchText` and hand back its rendered elements. */
async function mountDiff(patchText) {
  const ChatToolGroup = (await load('features/chat/components/ChatToolGroup.vue')).default
  const owners = await load('features/chat/owners.ts')
  const highlight = await load('utils/codeHighlight.ts')
  const tools = [{
    key: 'tool-1',
    name: 'apply_patch',
    status: 'completed',
    argsText: JSON.stringify({ input: patchText }),
    resultText: 'Done',
  }]
  const part = vue.reactive({ key: 'part-1', kind: 'tool_group', tools })
  const turn = vue.reactive({ key: 'turn-1', parts: [part], renderRevision: 0 })
  const renderer = createRenderer()
  const app = renderer.createApp({
    setup() {
      vue.provide(owners.chatTimelineKey, {
        expandedToolGroups: vue.shallowReactive(new Map([[turn.key, new Set([part.key])]])),
        turns: vue.ref([turn]),
        onToolGroupToggle() {},
        collapseDetails() {},
      })
      vue.provide(owners.chatReviewKey, {
        comments: vue.ref([]),
        addComment() {},
        reanchorComment() {},
        removeComment() {},
        setAnchorStale() {},
        focusTarget: vue.ref(null),
      })
      return () => vue.h(ChatToolGroup, { turn, part })
    },
  })
  const vm = app.mount({ children: [], parent: null })
  // The card asks for the chunk itself; wait for it so every assertion below
  // reads the highlighted state, not the graceful-degradation one.
  await highlight.ensureHighlighter()
  await flush()
  return {
    highlight,
    root: vm.$.subTree.el,
    rows: () => collect(vm.$.subTree.el, 'activity-diff-text'),
    unmount: () => app.unmount(),
  }
}

/** Decode the entities `escapeHtml` emits, so escaped text can be compared with
 *  the row it came from. */
function decodeEntities(html) {
  return html
    .replaceAll('&quot;', '"')
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&#39;', "'")
    .replaceAll('&amp;', '&')
}

/** The text of a row's `v-html`, with the highlighter's own spans removed: what
 *  is left is what a reader sees, and it must never be markup. */
function textOf(html) {
  return html.replace(/<\/?span[^>]*>/g, '')
}

test('a Python row is highlighted as Python and a file it has no grammar for stays plain', async t => {
  const card = await mountDiff(PYTHON_PATCH)
  t.after(() => card.unmount())

  const html = card.rows().map(row => row.props.innerHTML)
  // Rows of the Python file, by the index the fixture gives them.
  const hunk = html[0]
  const removed = html[3]
  const statement = html[4]
  const returned = html[6]
  const definition = html[9]
  // The hunk header is structure, not code: it stays as written.
  assert.equal(hunk, '@@ -236,8 +236,11 @@ def native_runtime_snapshot(runtime):')
  // A Python line reads as Python: `def` is a keyword, its name a title, the
  // literals are strings, and `return` is a keyword on an added row too.
  assert.match(definition, /<span class="hljs-keyword">def<\/span>/)
  assert.match(definition, /<span class="hljs-title function_">effective_status<\/span>/)
  assert.match(statement, /<span class="hljs-string">&quot;state&quot;<\/span>/)
  assert.match(returned, /<span class="hljs-keyword">return<\/span>/)
  // A removed row is highlighted as well: the sign and the tint carry the side
  // of the change, the colours only say what the code is.
  assert.match(removed, /<span class="hljs-keyword">return<\/span>/)
  // More than one token kind really came out of the highlighter.
  assert.match(html.join(''), /hljs-literal/)
  // `/tmp/claude_hub/run.log` has no grammar in the bundled highlighter: those
  // rows keep their text and get no spans rather than a neighbour's grammar.
  const logRows = html.slice(-4)
  assert.ok(logRows.every(row => !row.includes('hljs-')), logRows.join('\n'))
  assert.equal(logRows[2], 'migrate: done')
})

test('the header chip names the language, and only when there really is one', async t => {
  const card = await mountDiff(PYTHON_PATCH)
  t.after(() => card.unmount())
  await flush()

  const chips = collect(card.root, 'activity-diff-file-lang')
  // Exactly one chip: the Python file. The unknown-extension file gets nothing
  // rather than a guessed label.
  assert.equal(chips.length, 1)
  // The chip carries one text node and nothing else.
  assert.equal(chips[0].text, 'Python')
  const headers = collect(card.root, 'activity-diff-file')
  assert.equal(headers.length, 2, 'both files still have a header row')
})

test('a row of tool arguments can never become markup', async t => {
  const card = await mountDiff(PYTHON_PATCH)
  t.after(() => card.unmount())

  const scriptRow = card.rows().find(row => row.props.innerHTML.includes('&lt;script&gt;'))
  assert.ok(scriptRow, 'the fixture really carries a script tag in a row')
  const html = scriptRow.props.innerHTML
  // The tag arrives escaped, and the only markup in the string is the
  // highlighter's own spans.
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), html)
  assert.ok(!textOf(html).includes('<'), textOf(html))
  // Decoded, the row reads exactly as the tool wrote it.
  assert.equal(decodeEntities(textOf(html)), '    label = f"<script>alert(1)</script>"')
})

test('at 400 rows the card still highlights; past 400 it does not', async t => {
  const atCeiling = await mountDiff(pythonRowsPatch(398))
  t.after(() => atCeiling.unmount())
  const rows = atCeiling.rows()
  assert.equal(diffRowCount(atCeiling.root), 400)
  // The hunk header is structure, not code, so it stays as written; every code
  // row below it is tokenised.
  assert.ok(rows.slice(1).every(row => row.props.innerHTML.includes('hljs-')), 'a full-size diff still highlights')

  const overCeiling = await mountDiff(pythonRowsPatch(399))
  t.after(() => overCeiling.unmount())
  const bigRows = overCeiling.rows()
  // The builder stops at 400 rows and appends one truncation row, so this diff
  // is the first size the card declines to highlight.
  assert.equal(diffRowCount(overCeiling.root), 401)
  // The chunk is loaded (the previous card proved it), so plain text here is the
  // row-count ceiling and nothing else.
  assert.equal(overCeiling.highlight.highlightReady.value, true)
  assert.equal(overCeiling.highlight.isLanguageRegistered('python'), true)
  assert.ok(bigRows.every(row => !row.props.innerHTML.includes('hljs-')), 'no row of a huge diff is tokenised')
  // The row text itself is unchanged: only the highlighting is skipped.
  assert.equal(textOf(bigRows[1].props.innerHTML), '    return compute(0)')
})

test('a diff over the ceiling promises no grammar and loads no chunk', () => {
  // L-4 of review round 4: the header chip named a language whose rows would stay
  // plain, and the card asked for a highlighter chunk it would not use.
  const source = readFileSync(new URL('../src/features/chat/components/ChatToolGroup.vue', import.meta.url), 'utf8')
  const label = source.match(/function diffFileLabel\([\s\S]*?\n\}/)?.[0] ?? ''
  assert.match(label, /diff\.length > DIFF_HIGHLIGHT_ROW_LIMIT/, 'the chip knows about the ceiling')
  assert.match(label, /isKnownLanguage\(lang\)/, 'and names the language without the chunk')
  const effect = source.match(/watchEffect\(\(\) => \{[\s\S]*?\n\}\)/)?.[0] ?? ''
  assert.match(effect, /diff\.length <= DIFF_HIGHLIGHT_ROW_LIMIT/, 'the chunk is only requested for a diff it will colour')
})
