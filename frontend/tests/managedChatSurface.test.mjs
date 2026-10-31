import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/managedChatSurface.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const {
  chatStreamIdentity,
  providerSupportsManagedSurface,
  tabOwnsStructuredChat,
} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('managed Chat uses managed session identity for every stream operation', () => {
  assert.deepEqual(chatStreamIdentity('tab / ignored', 'session / 42'), {
    source: 'managed-session',
    sourceId: 'session / 42',
    basePath: '/api/workspaces/sessions/session%20%2F%2042/stream',
  })
  assert.deepEqual(chatStreamIdentity('tab / 42'), {
    source: 'terminal-tab',
    sourceId: 'tab / 42',
    basePath: '/api/workspaces/tabs/tab%20%2F%2042/stream',
  })
})

test('managed Chat fails closed without its managed session id', () => {
  assert.equal(tabOwnsStructuredChat({ session_kind: 'chat' }), true)
  assert.equal(tabOwnsStructuredChat({
    session_kind: 'chat',
    workspace_role: 'worker',
    managed_session_id: 'managed-1',
    agent_session_id: 'provider-conversation-9',
  }), true)
  assert.equal(tabOwnsStructuredChat({
    session_kind: 'chat',
    workspace_role: 'reviewer',
    managed_session_id: null,
  }), false)
  assert.equal(tabOwnsStructuredChat({
    session_kind: 'terminal',
    workspace_role: 'worker',
    managed_session_id: 'managed-1',
  }), false)
})

test('surface support permits local managed Chat and rejects unsupported combinations', () => {
  for (const provider of ['claude', 'codex', 'cursor', 'maso', 'antigravity']) {
    assert.equal(providerSupportsManagedSurface(provider, 'chat', 'local'), true)
    assert.equal(providerSupportsManagedSurface(provider, 'chat', 'remote'), false)
  }
  assert.equal(providerSupportsManagedSurface('maso', 'terminal', 'local'), false)
  assert.equal(providerSupportsManagedSurface('antigravity', 'terminal', 'local'), false)
  assert.equal(providerSupportsManagedSurface('antigravity', 'terminal', 'remote'), false)
  assert.equal(providerSupportsManagedSurface('codex', 'terminal', 'remote'), true)
  assert.equal(providerSupportsManagedSurface('terminal', 'terminal', 'local'), true)
})
