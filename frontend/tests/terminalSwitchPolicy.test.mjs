import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/terminalSwitchPolicy.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  isAgentTuiTab,
  shouldReplayHistoryOnSwitch,
  decideSwitchReplay,
  decideStatusChangeReplay,
} = mod

test('isAgentTuiTab returns true only for claude/codex/cursor', () => {
  assert.equal(isAgentTuiTab('claude'), true)
  assert.equal(isAgentTuiTab('codex'), true)
  assert.equal(isAgentTuiTab('cursor'), true)
  assert.equal(isAgentTuiTab('terminal'), false)
  assert.equal(isAgentTuiTab(undefined), false)
})

test('shouldReplayHistoryOnSwitch: plain terminal tabs always replay', () => {
  for (const status of [null, 'idle', 'working', 'attention', 'offline']) {
    assert.equal(
      shouldReplayHistoryOnSwitch('terminal', status),
      true,
      `terminal tab should replay regardless of status=${status}`,
    )
  }
})

test('shouldReplayHistoryOnSwitch: agent TUI tabs replay only when idle or attention', () => {
  for (const agentType of ['claude', 'codex', 'cursor']) {
    assert.equal(shouldReplayHistoryOnSwitch(agentType, 'idle'), true)
    assert.equal(shouldReplayHistoryOnSwitch(agentType, 'attention'), true)
    assert.equal(shouldReplayHistoryOnSwitch(agentType, 'working'), false)
    assert.equal(shouldReplayHistoryOnSwitch(agentType, 'offline'), false)
    assert.equal(shouldReplayHistoryOnSwitch(agentType, null), false)
  }
})

test('decideSwitchReplay maps safe→immediate, unsafe→defer (including remount/reload defer)', () => {
  assert.equal(decideSwitchReplay('terminal', 'working'), 'immediate')
  assert.equal(decideSwitchReplay('claude', 'idle'), 'immediate')
  assert.equal(decideSwitchReplay('claude', 'working'), 'defer')
  assert.equal(decideSwitchReplay('codex', 'working'), 'defer')
  assert.equal(decideSwitchReplay('cursor', 'attention'), 'immediate')
  assert.equal(decideSwitchReplay('cursor', null), 'defer')
})

test('decideStatusChangeReplay: round-complete fires on working→stable and clears pending', () => {
  for (const stable of ['idle', 'attention']) {
    const r = decideStatusChangeReplay('working', stable, false)
    assert.equal(r.replay, true)
    assert.equal(r.clearPending, true)
  }
})

test('decideStatusChangeReplay: deferred-switch fulfills on first stable status', () => {
  for (const stable of ['idle', 'attention']) {
    const r = decideStatusChangeReplay(null, stable, true)
    assert.equal(r.replay, true)
    assert.equal(r.clearPending, true)
  }
})
