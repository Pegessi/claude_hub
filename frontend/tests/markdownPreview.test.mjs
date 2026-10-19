import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/markdownPreview.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
})
const { localMarkdownTarget, markdownPreviewUrl, markdownStreamBase, localAttachmentTarget, attachmentKind, attachmentPreviewUrl, attachmentTargetFromLink } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('local Markdown links retain path, Unicode, query delimiters, and fragment identity', () => {
  for (const path of ['README.md', './docs/report.markdown', '../report.mdown', '/tmp/报告 (1).mkd', '~/note.MD']) {
    const url = new URL(markdownPreviewUrl('tab-a', path), 'http://hub')
    assert.equal(url.searchParams.get('markdown'), path)
    assert.equal(url.searchParams.get('scope'), 'tab-a')
  }
  assert.deepEqual(localMarkdownTarget('/tmp/a%23b%3Fc%26d.md:12#results'), {
    path: '/tmp/a#b?c&d.md', hash: '#results',
  })
  assert.deepEqual(localMarkdownTarget('file:///tmp/report.md#section'), { path: '/tmp/report.md', hash: '#section' })
  assert.deepEqual(localMarkdownTarget('file://localhost/tmp/report.md'), { path: '/tmp/report.md', hash: '' })
})

test('external links and non-Markdown resources are not hijacked', () => {
  for (const href of [
    'https://example.com/report.md', 'http://example.com/report.md', '//example.com/report.md',
    'file://remote/report.md', 'mailto:user@example.md', 'javascript:alert.md',
    '#report.md', '?download=report.md', '/api/report.md', '/assets/report.md',
    'report.pdf', 'diagram.png', '%ZZ.md', '%2F%2Fevil/report.md', 'bad%00name.md', 'foo\\report.md',
  ]) assert.equal(markdownPreviewUrl('tab-a', href), null, href)
  assert.equal(markdownPreviewUrl('', 'report.md'), null)
})

test('document-relative targets and managed identity stay scoped', () => {
  const scope = '/api/workspaces/sessions/managed-a/stream'
  assert.equal(markdownStreamBase(scope), scope)
  assert.equal(markdownStreamBase('tab-a'), '/api/workspaces/tabs/tab-a/stream')
  for (const invalid of ['/api/workspaces/sessions/a/stream/other', 'https://evil/', '//evil', '/etc', 'a?b']) {
    assert.equal(markdownStreamBase(invalid), null)
  }
  const url = new URL(markdownPreviewUrl(scope, 'next.md#details', '/work/docs/report.md'), 'http://hub')
  assert.equal(url.searchParams.get('markdown'), '/work/docs/next.md')
  assert.equal(url.searchParams.get('scope'), scope)
  assert.equal(url.hash, '#details')
  assert.equal(localMarkdownTarget('/tmp/other.md', '/work/docs/report.md').path, '/tmp/other.md')
  assert.equal(localMarkdownTarget('../other.md', '/work/docs/report.md').path, '/work/docs/../other.md')
})

test('code, data and named text files use one scoped attachment URL', () => {
  for (const path of ['code.py', 'data.json', 'events.jsonl', 'task.yaml', 'query.sql', 'notes.txt', 'run.log', 'Dockerfile', 'Makefile', 'README', 'CMakeLists.txt', 'notebook.ipynb', 'index.html']) {
    assert.equal(attachmentKind(path), 'text', path)
    const url = new URL(attachmentPreviewUrl('tab-a', path), 'http://hub')
    assert.equal(url.searchParams.get('attachment'), path)
    assert.equal(url.searchParams.get('scope'), 'tab-a')
  }
  assert.deepEqual(localAttachmentTarget('file:///tmp/code.py:12:3'), { path: '/tmp/code.py', hash: '' })
  assert.deepEqual(localAttachmentTarget('../数据.json#item', '/work/docs/report.md'), { path: '/work/docs/../数据.json', hash: '#item' })
  assert.equal(attachmentKind('archive.zip'), 'unsupported')
  assert.equal(attachmentKind('plot.png'), 'image')
  assert.equal(attachmentKind('.env'), null)
})

test('attachment reader links cannot override the current Chat scope', () => {
  assert.deepEqual(attachmentTargetFromLink('/?attachment=%2Fwork%2Fa.json&scope=another-tab#item'), {
    path: '/work/a.json', hash: '#item',
  })
  for (const href of [
    'https://other/code.py', '//other/data.json', 'file://remote/code.py', '/api/data.json',
    '%2Fapi%2Fdata.json', '%2F%2Fevil/a.py', 'code%00.py', 'code%5C.py', '%ZZ.json',
    '#code.py', '&num;code.py', '?file=data.json', 'javascript:alert.py',
  ]) assert.equal(localAttachmentTarget(href), null, href)
  assert.equal(attachmentPreviewUrl('https://evil', 'data.json'), null)
})
