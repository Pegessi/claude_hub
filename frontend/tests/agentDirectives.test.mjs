import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const load = async (path) => {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
}
const { renderAgentDirectives, plainAgentDirectives } = await load('../src/utils/agentDirectives.ts')
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
