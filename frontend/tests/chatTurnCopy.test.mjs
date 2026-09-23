import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// chatTurnCopy.ts has no runtime imports, so it transpiles and loads cleanly.
const source = readFileSync(
  new URL('../src/utils/chatTurnCopy.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { buildTurnCopyText } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('copies the user question and every assistant text block as one turn', () => {
  const text = buildTurnCopyText({
    userText: '  How do I read a file?  ',
    parts: [
      { kind: 'thinking', key: 'thinking-1', text: 'secret reasoning' },
      { kind: 'text', key: 'text-1', text: 'Use the Read tool:' },
      {
        kind: 'tool_group',
        key: 'tool-1',
        tools: [{ name: 'Read', argsText: '{"file_path":"/etc/passwd"}', resultText: 'root:x:0:0' }],
      },
      { kind: 'text', key: 'text-2', text: 'Then inspect the output above.' },
      { kind: 'status', key: 'status-1', text: 'Working…' },
      { kind: 'error', key: 'error-1', message: 'boom' },
    ],
  })

  assert.equal(
    text,
    [
      'User:',
      'How do I read a file?',
      '',
      'Assistant:',
      'Use the Read tool:',
      '',
      'Then inspect the output above.',
    ].join('\n'),
  )
})

test('excludes thinking, tool JSON, status, error and internal event content', () => {
  const text = buildTurnCopyText({
    userText: 'q',
    parts: [
      { kind: 'thinking', text: 'THINKING_SECRET' },
      { kind: 'tool', tool: { name: 'Bash', argsText: 'TOOL_JSON_SECRET' } },
      { kind: 'approval', approval: { id: 'a1' } },
      { kind: 'process', meta: 'PROCESS_SECRET' },
      { kind: 'text', text: 'visible answer' },
    ],
  })

  for (const secret of ['THINKING_SECRET', 'TOOL_JSON_SECRET', 'PROCESS_SECRET']) {
    assert.ok(!text.includes(secret), `${secret} must not be copied`)
  }
  assert.ok(text.includes('visible answer'))
})

test('joins multiple assistant text blocks with blank lines and trims edges', () => {
  const text = buildTurnCopyText({
    userText: '\nhello\n',
    parts: [
      { kind: 'text', text: '\nfirst block\n' },
      { kind: 'text', text: '  second block  ' },
    ],
  })
  assert.equal(text, 'User:\nhello\n\nAssistant:\nfirst block\n\nsecond block')
})

test('drops empty and whitespace-only assistant text parts', () => {
  const withEmpty = buildTurnCopyText({
    userText: 'q',
    parts: [
      { kind: 'text', text: '' },
      { kind: 'text', text: '   \n' },
      { kind: 'text', text: 'real' },
    ],
  })
  assert.equal(withEmpty, 'User:\nq\n\nAssistant:\nreal')
})

test('omits section labels when one side is absent and returns empty for empty turns', () => {
  assert.equal(
    buildTurnCopyText({ userText: 'just a question', parts: [] }),
    'User:\njust a question',
  )
  assert.equal(
    buildTurnCopyText({
      userText: '',
      parts: [{ kind: 'text', text: 'unsolicited answer' }],
    }),
    'Assistant:\nunsolicited answer',
  )
  assert.equal(buildTurnCopyText({ userText: '  ', parts: [] }), '')
})
