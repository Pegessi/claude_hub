import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const transpile = async (path) => ts.transpileModule(await readFile(new URL(path, import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const dataUrl = (js) => `data:text/javascript;base64,${Buffer.from(js).toString('base64')}`
const load = async (path) => import(dataUrl(await transpile(path)))
const previewJs = await transpile('../src/utils/markdownPreview.ts')
const { attachmentTargetFromLink } = await import(dataUrl(previewJs))
// agentDirectives imports markdownPreview; point that import at the same module.
const { renderAgentDirectives, plainAgentDirectives } = await import(dataUrl(
  (await transpile('../src/utils/agentDirectives.ts')).replace("'./markdownPreview'", `'${dataUrl(previewJs)}'`),
))
const { buildTurnCopyText } = await load('../src/utils/chatTurnCopy.ts')

const OPEN = '\uE200'
const SEP = '\uE202'
const CLOSE = '\uE201'
const PATH = '/home/u/work/visualizations/device-monitor.html'
// The exact shape Codex emitted in a real Chat (visualize skill).
const directive = `${OPEN}visualize${SEP}{"path":"${PATH}","mode":"wide"}${CLOSE}`

test('a visualize directive becomes a preview card, with no marker glyphs left', () => {
  const html = renderAgentDirectives(`预览如下。${directive}\n\n曲线为示例数据。`)
  assert.doesNotMatch(html, /[\uE200-\uE202]/)
  assert.doesNotMatch(html, /"mode":"wide"/)
  assert.match(html, /<a class="agent-directive-card" href="\/home\/u\/work\/visualizations\/device-monitor\.html">/)
  assert.match(html, /<span class="agent-directive-label">Open visualization<\/span>/)
  assert.match(html, /<span class="agent-directive-file">device-monitor\.html<\/span>/)
  assert.match(html, /^预览如下。/)
  assert.match(html, /曲线为示例数据。$/)
})

test('a directive still streaming in is hidden until it closes', () => {
  const partial = `预览如下。${OPEN}visualize${SEP}{"path":"/home/u/wo`
  assert.equal(renderAgentDirectives(partial), '预览如下。')
  assert.equal(renderAgentDirectives(`${partial}rk.html"}${CLOSE}`).includes('agent-directive-card'), true)
})

test('directives without a local path and stray markers disappear', () => {
  assert.equal(renderAgentDirectives(`a${OPEN}cite${SEP}turn0search0${CLOSE}b`), 'ab')
  assert.equal(renderAgentDirectives(`a${CLOSE}b${SEP}c`), 'abc')
  assert.equal(renderAgentDirectives('plain text'), 'plain text')
})

test('directive payloads cannot inject markup', () => {
  const hostile = `${OPEN}"><img src=x onerror=alert(1)>${SEP}{"path":"/x/a\\"><script>.html"}${CLOSE}`
  const html = renderAgentDirectives(hostile)
  assert.doesNotMatch(html, /<img|<script/)
  assert.match(html, /&quot;&gt;&lt;img/)
})

test('copied turns carry the directive as a plain path', () => {
  assert.equal(plainAgentDirectives(`see ${directive} now`), `see ${PATH} now`)
  const text = buildTurnCopyText(
    { userText: 'q', parts: [{ kind: 'text', text: `done ${directive}` }, { kind: 'text', text: `${OPEN}cite${SEP}x${CLOSE}` }] },
    plainAgentDirectives,
  )
  assert.equal(text, `User:\nq\n\nAssistant:\ndone ${PATH}`)
})

const card = (path, extra = '') => `${OPEN}visualize${SEP}${JSON.stringify({ path, ...JSON.parse(extra || '{}') })}${CLOSE}`
const hrefOf = (html) => html.match(/href="([^"]*)"/)?.[1].replace(/&amp;/g, '&')

test('card hrefs round-trip to the exact path, including # ? spaces and non-ASCII', () => {
  for (const path of ['/a b/x#1?.html', '/a/x?y.html', '/数据/图 1.html', '/a/100%.html']) {
    const href = hrefOf(renderAgentDirectives(card(path)))
    assert.ok(href, `${path} renders a card`)
    assert.equal(attachmentTargetFromLink(href)?.path, path, path)
  }
})

test('only absolute, previewable paths become cards', () => {
  for (const path of ['javascript:alert(1)', 'rel/x.html', '~/x.html', '//host/x.html', '/x/no-extension', '/x/archive.zip']) {
    const html = renderAgentDirectives(`a${card(path)}b`)
    assert.equal(html, 'ab', path)
  }
})

test('the skill title labels the card, escaped and bounded', () => {
  const html = renderAgentDirectives(card('/v/a.html', JSON.stringify({ title: `<b>GPU</b> ${'x'.repeat(200)}` })))
  const label = html.match(/agent-directive-label">([^<]*)</)[1]
  assert.match(label, /^&lt;b&gt;GPU/)
  assert.ok(label.length < 120)
})

test('several directives render, and quoted directives in fences stay as code', () => {
  const two = renderAgentDirectives(`${card('/v/a.html')} and ${card('/v/b.html')}`)
  assert.equal((two.match(/agent-directive-card/g) ?? []).length, 2)
  const fenced = renderAgentDirectives(`Syntax:\n\n\`\`\`text\n${card('/v/a.html')}\n\`\`\`\n\nDone ${card('/v/b.html')}`)
  assert.match(fenced, /```text\nvisualize\{"path":"\/v\/a\.html"\}\n```/)
  assert.equal((fenced.match(/agent-directive-card/g) ?? []).length, 1)
  assert.doesNotMatch(fenced, /[\uE200-\uE202]/)
})

test('an unclosed marker hides text only while streaming', () => {
  const broken = `before ${OPEN}visualize${SEP}{"path" after`
  assert.equal(renderAgentDirectives(broken), 'before ')
  assert.equal(renderAgentDirectives(broken, { complete: true }), 'before visualize{"path" after')
  assert.equal(plainAgentDirectives(broken), 'before visualize{"path" after')
})
