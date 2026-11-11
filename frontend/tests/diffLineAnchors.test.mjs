import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/toolActivity.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { describeToolActivity } =
  await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)

const call = (name, args) => ({ name, argsText: typeof args === 'string' ? args : JSON.stringify(args, null, 2) })
const rows = activity => activity.diff.map(line => ({
  sign: line.sign,
  text: line.text,
  file: line.file ?? '',
  old: line.oldLine ?? null,
  new: line.newLine ?? null,
  header: line.fileHeader === true,
}))

test('a numbered hunk gives both sides their own line numbers', () => {
  const diff = ['--- a/app.py', '+++ b/app.py', '@@ -10,3 +10,4 @@', ' keep', '-old', '+new', '+extra', ' tail'].join('\n')
  const edit = describeToolActivity(call('apply_patch', { changes: [{ path: 'app.py', diff }] }))
  assert.deepEqual(rows(edit), [
    { sign: '@', text: 'app.py', file: 'app.py', old: null, new: null, header: true },
    { sign: '@', text: '@@ -10,3 +10,4 @@', file: 'app.py', old: null, new: null, header: false },
    // Context advances both sides; a removal only the original, an addition only the new.
    { sign: ' ', text: 'keep', file: 'app.py', old: 10, new: 10, header: false },
    { sign: '-', text: 'old', file: 'app.py', old: 11, new: null, header: false },
    { sign: '+', text: 'new', file: 'app.py', old: null, new: 11, header: false },
    { sign: '+', text: 'extra', file: 'app.py', old: null, new: 12, header: false },
    { sign: ' ', text: 'tail', file: 'app.py', old: 12, new: 13, header: false },
  ])
})

test('a patch envelope numbers each file it names', () => {
  const patch = [
    '*** Begin Patch',
    '*** Update File: src/a.ts',
    '@@ -1,2 +1,2 @@',
    '-one',
    '+uno',
    ' two',
    '*** Update File: src/b.ts',
    '@@ -7 +7 @@',
    '-x',
    '+y',
    '*** End Patch',
  ].join('\n')
  const edit = describeToolActivity(call('apply_patch', { input: patch }))
  assert.deepEqual(rows(edit).map(row => [row.sign, row.text, row.file, row.old, row.new, row.header]), [
    ['@', 'src/a.ts', 'src/a.ts', null, null, true],
    ['@', '@@ -1,2 +1,2 @@', 'src/a.ts', null, null, false],
    ['-', 'one', 'src/a.ts', 1, null, false],
    ['+', 'uno', 'src/a.ts', null, 1, false],
    [' ', 'two', 'src/a.ts', 2, 2, false],
    ['@', 'src/b.ts', 'src/b.ts', null, null, true],
    ['@', '@@ -7 +7 @@', 'src/b.ts', null, null, false],
    ['-', 'x', 'src/b.ts', 7, null, false],
    ['+', 'y', 'src/b.ts', null, 7, false],
  ])
})

test('a Claude old/new string edit names its file but cannot number lines', () => {
  const edit = describeToolActivity(call('Edit', { file_path: '/repo/src/app.ts', old_string: 'a\nb', new_string: 'a\nc' }))
  assert.deepEqual(rows(edit).map(row => [row.sign, row.text, row.file, row.old, row.new]), [
    ['-', 'a', '/repo/src/app.ts', null, null],
    ['-', 'b', '/repo/src/app.ts', null, null],
    ['+', 'a', '/repo/src/app.ts', null, null],
    ['+', 'c', '/repo/src/app.ts', null, null],
  ])
})

test('a hunk without ranges stays unnumbered and a nameless diff keeps no file', () => {
  const raw = describeToolActivity(call('apply_patch', '*** Begin Patch\n*** Update File: notes.md\n@@\n-a\n+b\n*** End Patch'))
  assert.deepEqual(rows(raw).map(row => [row.sign, row.text, row.new]), [
    ['@', 'notes.md', null],
    ['@', '@@', null],
    ['-', 'a', null],
    ['+', 'b', null],
  ])
  const loose = describeToolActivity(call('apply_patch', { input: '@@ -1 +1 @@\n-a\n+b' }))
  assert.deepEqual(rows(loose).map(row => [row.sign, row.text, row.file, row.new]), [
    ['@', '@@ -1 +1 @@', '', null],
    ['-', 'a', '', null],
    ['+', 'b', '', 1],
  ])
})

test('a multi-file git diff attributes each row to the file it came from', () => {
  const diff = [
    'diff --git a/x.ts b/x.ts', '--- a/x.ts', '+++ b/x.ts', '@@ -1 +1 @@', '-a', '+b',
    'diff --git a/y.ts b/y.ts', '--- a/y.ts', '+++ b/y.ts', '@@ -9 +9 @@', '-c', '+d',
  ].join('\n')
  const edit = describeToolActivity(call('apply_patch', { changes: [{ path: 'x.ts', diff }] }))
  assert.deepEqual(rows(edit).map(row => [row.sign, row.text, row.file, row.new]), [
    ['@', 'x.ts', 'x.ts', null],
    ['@', '@@ -1 +1 @@', 'x.ts', null],
    ['-', 'a', 'x.ts', null],
    ['+', 'b', 'x.ts', 1],
    ['@', '@@ -9 +9 @@', 'y.ts', null],
    ['-', 'c', 'y.ts', null],
    ['+', 'd', 'y.ts', 9],
  ])
})

test('the 400-row cap still ends with an explicit truncation row', () => {
  const edit = describeToolActivity(call('Edit', { file_path: 'a', old_string: '', new_string: 'x\n'.repeat(1000) }))
  assert.equal(edit.diff.length, 401)
  assert.equal(edit.diff.at(-1).sign, '@')
  assert.equal(edit.diff.at(-1).text, '… diff truncated')
})
