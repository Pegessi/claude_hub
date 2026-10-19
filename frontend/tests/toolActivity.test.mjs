import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/toolActivity.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { describeToolActivity, toolActivityKind, toolGroupHeadline, ACTIVITY_ICONS } =
  await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const toolGroup = await readFile(new URL('../src/features/chat/components/ChatToolGroup.vue', import.meta.url), 'utf8')

const call = (name, args) => ({ name, argsText: typeof args === 'string' ? args : JSON.stringify(args, null, 2) })

test('provider spellings of the same action share one activity kind', () => {
  for (const name of ['Bash', 'shell', 'exec_command', 'Shell']) assert.equal(toolActivityKind(name), 'command')
  for (const name of ['Edit', 'MultiEdit', 'apply_patch']) assert.equal(toolActivityKind(name), 'edit')
  assert.equal(toolActivityKind('mcp__github__create_issue'), 'mcp')
  assert.equal(toolActivityKind('SomethingNew'), 'other')
  for (const kind of ['command', 'read', 'edit', 'write', 'search', 'list', 'web', 'plan', 'mcp', 'other']) {
    assert.ok(ACTIVITY_ICONS[kind], `${kind} has an icon`)
  }
})

test('commands show the script, including Codex argv wrappers', () => {
  const claude = describeToolActivity(call('Bash', { command: 'pnpm test', description: 'Run tests' }))
  assert.deepEqual([claude.verb, claude.target, claude.command], ['Ran', 'pnpm test', 'pnpm test'])
  const codex = describeToolActivity(call('shell', { command: ['bash', '-lc', 'ls -la'] }))
  assert.equal(codex.command, 'ls -la')
  const exec = describeToolActivity(call('exec_command', { cmd: 'git status', cwd: '/repo' }))
  assert.equal(exec.target, 'git status')
})

test('long targets are bounded and long paths keep their tail', () => {
  const long = describeToolActivity(call('Bash', { command: `echo ${'x'.repeat(500)}` }))
  assert.ok(long.target.length <= 120)
  assert.ok(long.command.length > 500, 'the expanded command stays complete')
  const read = describeToolActivity(call('Read', { file_path: '/home/u/repo/frontend/src/utils/toolActivity.ts' }))
  assert.deepEqual([read.verb, read.target], ['Read', '…/src/utils/toolActivity.ts'])
})

test('edits expose a line diff from Claude and Codex argument shapes', () => {
  const edit = describeToolActivity(call('Edit', { file_path: 'a.ts', old_string: 'one\ntwo', new_string: 'one\n2' }))
  assert.equal(edit.target, 'a.ts')
  assert.deepEqual(edit.diff.map(line => line.sign), ['-', '-', '+', '+'])
  const patch = describeToolActivity(call('apply_patch', {
    changes: [{ path: 'src/b.ts', kind: 'update', diff: '@@ -1 +1 @@\n-old\n+new\n same' }],
  }))
  assert.equal(patch.target, 'src/b.ts')
  assert.deepEqual(patch.diff.map(line => `${line.sign}${line.text}`), ['@src/b.ts', '@@@ -1 +1 @@', '-old', '+new', ' same'])
  const raw = describeToolActivity(call('apply_patch', '*** Begin Patch\n*** Update File: c.py\n-a\n+b\n*** End Patch'))
  assert.equal(raw.target, 'c.py')
  assert.deepEqual(raw.diff.map(line => line.sign), ['-', '+'])
})

test('malformed or empty arguments degrade to the tool name, never throw', () => {
  assert.equal(describeToolActivity(call('Read', '{not json')).target, '')
  assert.equal(describeToolActivity(call('Mystery', '')).verb, 'Mystery')
  assert.equal(describeToolActivity(call('Edit', { file_path: 'x' })).diff, undefined)
})

test('group headlines aggregate by kind without parsing arguments', () => {
  let parses = 0
  const tools = ['Bash', 'Bash', 'Read', 'Edit'].map(name => ({
    name,
    get argsText() { parses++; return '{}' },
  }))
  assert.equal(toolGroupHeadline(tools), 'Ran 2 commands, read 1 file, edited 1 file')
  assert.equal(parses, 0)
  const many = ['Bash', 'Read', 'Edit', 'Grep'].map(name => ({ name }))
  assert.equal(toolGroupHeadline(many), 'Ran 1 command, read 1 file, edited 1 file, …')
})

test('expanded tool items parse arguments only while their card is open', () => {
  assert.match(toolGroup, /function expandedItems\(\)[^\n]*\n\s*if \(!expandedToolGroups\.get\(props\.turn\.key\)\?\.has\(props\.part\.key\)\) return \[\]/)
})
