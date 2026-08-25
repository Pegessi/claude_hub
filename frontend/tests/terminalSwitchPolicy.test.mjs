import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// terminalSwitchPolicy.ts only does `import type { AgentType } from '@/types'`,
// which TypeScript erases at transpile time, so no runtime shim is needed.
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
const { isAgentTuiTab, shouldReplayHistoryOnSwitch, decideSwitchReplay, decideStatusChangeReplay } = mod

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
    assert.equal(
      shouldReplayHistoryOnSwitch(agentType, 'idle'),
      true,
      `${agentType} idle → safe to replay`,
    )
    assert.equal(
      shouldReplayHistoryOnSwitch(agentType, 'attention'),
      true,
      `${agentType} attention → safe to replay`,
    )
    // working: live relative-cursor writes in flight → must NOT replay
    assert.equal(
      shouldReplayHistoryOnSwitch(agentType, 'working'),
      false,
      `${agentType} working → must NOT replay (would corrupt xterm screen)`,
    )
    assert.equal(
      shouldReplayHistoryOnSwitch(agentType, 'offline'),
      false,
      `${agentType} offline → must NOT replay`,
    )
    assert.equal(
      shouldReplayHistoryOnSwitch(agentType, null),
      false,
      `${agentType} null status → must NOT replay`,
    )
  }
})

test('decideSwitchReplay maps safe→immediate, unsafe→defer', () => {
  // Plain terminal: always immediate.
  assert.equal(decideSwitchReplay('terminal', 'working'), 'immediate')
  assert.equal(decideSwitchReplay('terminal', null), 'immediate')
  // Agent TUI: idle/attention → immediate.
  assert.equal(decideSwitchReplay('claude', 'idle'), 'immediate')
  assert.equal(decideSwitchReplay('codex', 'attention'), 'immediate')
  // Agent TUI: working/unknown/offline → defer (pending safe replay).
  assert.equal(decideSwitchReplay('claude', 'working'), 'defer')
  assert.equal(decideSwitchReplay('cursor', null), 'defer')
  assert.equal(decideSwitchReplay('claude', 'offline'), 'defer')
})

test('decideStatusChangeReplay: round-complete fires on working→stable and clears pending', () => {
  for (const stable of ['idle', 'attention']) {
    const r = decideStatusChangeReplay('working', stable, false)
    assert.equal(r.replay, true, `working→${stable} should replay`)
    assert.equal(r.clearPending, true, `working→${stable} should clear pending`)
  }
})

test('decideStatusChangeReplay: deferred-switch fulfills on first stable status even without working edge', () => {
  // lastStatus is null (switch reset it) and the first polled status is
  // already idle — the working→stable edge was missed. With pending=true,
  // the deferred replay must still fire.
  for (const stable of ['idle', 'attention']) {
    const r = decideStatusChangeReplay(null, stable, true)
    assert.equal(r.replay, true, `null→${stable} with pending should replay`)
    assert.equal(r.clearPending, true, `null→${stable} with pending should clear pending`)
  }
  // Without pending, no replay (no working edge observed).
  for (const stable of ['idle', 'attention']) {
    const r = decideStatusChangeReplay(null, stable, false)
    assert.equal(r.replay, false, `null→${stable} without pending must not replay`)
    assert.equal(r.clearPending, false)
  }
})

test('decideStatusChangeReplay: non-stable statuses never replay and leave pending untouched', () => {
  for (const ns of ['working', 'offline', null]) {
    const r1 = decideStatusChangeReplay('working', ns, true)
    assert.equal(r1.replay, false, `working→${ns} must not replay`)
    assert.equal(r1.clearPending, false, `working→${ns} must not clear pending`)
    const r2 = decideStatusChangeReplay(null, ns, true)
    assert.equal(r2.replay, false, `null→${ns} must not replay even with pending`)
    assert.equal(r2.clearPending, false, `null→${ns} must leave pending set`)
  }
})

test('decideStatusChangeReplay: pending+working stays pending (agent still writing)', () => {
  // Switch set pending=true (agent was working/unknown). The next poll still
  // reports working — we must NOT replay (would corrupt live screen) and must
  // NOT clear pending (the stable state hasn't arrived yet).
  const r = decideStatusChangeReplay(null, 'working', true)
  assert.equal(r.replay, false)
  assert.equal(r.clearPending, false)
})

test('switch-away resets the deferred-replay state for the previous tab', () => {
  // On tab switch the component resets lastAgentStatus=null and recomputes
  // pendingSafeReplay from decideSwitchReplay for the *new* tab. The previous
  // tab's pending flag is overwritten (not carried over). We model this by
  // showing that a fresh switch to a working tab sets pending=true, and a
  // subsequent switch to a stable tab immediately replays (pending cleared)
  // — the stale pending from the previous tab does not leak.
  // Switch to working agent tab → defer.
  assert.equal(decideSwitchReplay('claude', 'working'), 'defer')
  // Switch away then to a stable agent tab → immediate (no stale pending).
  assert.equal(decideSwitchReplay('claude', 'idle'), 'immediate')
  // And the status watcher for the new tab starts from lastStatus=null with
  // pending=false (reset by the switch watcher), so a null→idle with
  // pending=false does NOT replay (the immediate switch replay already did).
  const r = decideStatusChangeReplay(null, 'idle', false)
  assert.equal(r.replay, false)
})
