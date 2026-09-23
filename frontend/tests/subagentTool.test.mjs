import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import ts from 'typescript'

// subagentTool.ts is dependency-free, so it transpiles and loads on its own.
const source = readFileSync(
  new URL('../src/utils/subagentTool.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  isSubagentTool,
  parseSubagent,
  subagentStatusLabel,
  subagentProviderLabel,
} = mod

const CLAUDE_ARGS = {
  description: 'Explore backend CLI probing & API',
  prompt: 'Please explore and report on the capability-probing pattern.',
  subagent_type: 'general-purpose',
}
const CURSOR_TASK_ARGS = {
  description: '比对两次评测差异',
  prompt: '请用中文只读比对两次评测。',
  subagentType: 'composer',
  agentId: '0104ad90-f345-4192-bd28-5545f1df1663',
}
const TRAEX_ARGS = {
  prompt: 'You are an independent read-only code reviewer. Review the diff.',
  receiverThreadIds: ['01a0bce6-af31-7ab1-b111-5d0b50967ae7'],
}

test('matches the confirmed sub-agent tool for each provider', () => {
  assert.equal(isSubagentTool('Agent', CLAUDE_ARGS), true)
  assert.equal(isSubagentTool('Task', CURSOR_TASK_ARGS), true)
  assert.equal(isSubagentTool('spawnAgent', TRAEX_ARGS), true)
})

test('ordinary tools never match, even with realistic args', () => {
  assert.equal(isSubagentTool('Bash', { command: 'ls -la', run_in_background: true }), false)
  assert.equal(isSubagentTool('Read', { file_path: '/tmp/x' }), false)
  assert.equal(isSubagentTool('Edit', { file_path: 'x', old_string: 'a', new_string: 'b' }), false)
  assert.equal(isSubagentTool('Grep', { pattern: 'Task', path: '.' }), false)
  assert.equal(isSubagentTool('Write', { file_path: 'x', content: 'y' }), false)
  assert.equal(isSubagentTool('Glob', { pattern: '**/*.ts' }), false)
  // TraeX lifecycle helpers are not spawns even though they carry a thread id.
  assert.equal(isSubagentTool('closeAgent', { prompt: null, receiverThreadIds: ['x'] }), false)
  // Claude's task-management tools share the "Task" stem but are unrelated.
  assert.equal(isSubagentTool('TaskCreate', { subject: 'x' }), false)
  assert.equal(isSubagentTool('TaskUpdate', { taskId: '1', status: 'done' }), false)
  assert.equal(isSubagentTool('TaskOutput', { task_id: '7' }), false)
  assert.equal(isSubagentTool('TaskStop', { task_id: '7' }), false)
})

test('a sub-agent NAME alone is never enough — fails closed without the signature', () => {
  // Claude Agent without a non-empty prompt cannot be trusted.
  assert.equal(isSubagentTool('Agent', { description: 'x', subagent_type: 'g' }), false)
  // Agent with a prompt but neither type nor description.
  assert.equal(isSubagentTool('Agent', { prompt: 'do it' }), false)
  // Cursor Task needs the camelCase typed target (subagentType/agentId).
  assert.equal(isSubagentTool('Task', { description: 'x', prompt: 'p' }), false)
  // TraeX spawn needs the receiver thread ids.
  assert.equal(isSubagentTool('spawnAgent', { prompt: 'p' }), false)
  assert.equal(isSubagentTool('spawnAgent', { prompt: 'p', receiverThreadIds: [] }), false)
  // Non-object / missing args always fall back to the generic tool block.
  assert.equal(isSubagentTool('Agent', null), false)
  assert.equal(isSubagentTool('Agent', undefined), false)
  assert.equal(isSubagentTool('Agent', 'not-an-object'), false)
  assert.equal(isSubagentTool(42, CLAUDE_ARGS), false)
})

test('parseSubagent projects the Claude Agent view', () => {
  const view = parseSubagent('Agent', { ...CLAUDE_ARGS, run_in_background: true })
  assert.deepEqual(view, {
    provider: 'claude',
    agentType: 'general-purpose',
    description: 'Explore backend CLI probing & API',
    prompt: CLAUDE_ARGS.prompt,
    threadIds: [],
    background: true,
  })
})

test('parseSubagent projects the Cursor Task view (camelCase type)', () => {
  const view = parseSubagent('Task', CURSOR_TASK_ARGS)
  assert.equal(view.provider, 'cursor')
  assert.equal(view.agentType, 'composer')
  assert.equal(view.description, '比对两次评测差异')
  assert.equal(view.prompt, CURSOR_TASK_ARGS.prompt)
  assert.equal(view.background, false)
})

test('parseSubagent projects the TraeX spawnAgent view (thread target, no type)', () => {
  const view = parseSubagent('spawnAgent', TRAEX_ARGS)
  assert.equal(view.provider, 'traex')
  assert.equal(view.agentType, null)
  assert.equal(view.description, '')
  assert.deepEqual(view.threadIds, ['01a0bce6-af31-7ab1-b111-5d0b50967ae7'])
})

test('status and provider labels cover every lifecycle/provider and pass through unknowns', () => {
  assert.equal(subagentStatusLabel('running'), '运行中')
  assert.equal(subagentStatusLabel('completed'), '已完成')
  assert.equal(subagentStatusLabel('failed'), '失败')
  assert.equal(subagentStatusLabel('cancelled'), '已取消')
  assert.equal(subagentStatusLabel('something-else'), 'something-else')
  assert.equal(subagentProviderLabel('claude'), 'Claude 子代理')
  assert.equal(subagentProviderLabel('cursor'), 'Cursor 子代理')
  assert.equal(subagentProviderLabel('traex'), 'TraeX 子代理')
})
