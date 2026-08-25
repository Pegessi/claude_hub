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

// ── Component-level regression: no forced scroll-bottom on working-agent switch ──

const terminalViewSource = await readFile(
  new URL('../src/components/TerminalView.vue', import.meta.url),
  'utf8',
)

test('deferred (working-agent) switch branch must NOT call postTerminalScrollBottom', () => {
  // The deferred branch handles agent TUI tabs that are working (or unknown
  // status). A full history replay would corrupt the live xterm screen, so we
  // only resize. Crucially, we must NOT force-scroll to bottom: if the user
  // intentionally scrolled up to read output, yanking them down on tab switch
  // violates the preserve-user-scroll invariant.
  //
  // This test parses TerminalView.vue and asserts that the `else` branch of
  // `if (switchAction === 'immediate')` (the deferred path) contains no
  // `postTerminalScrollBottom` call.
  const switchMatch = terminalViewSource.match(
    /if \(switchAction === 'immediate'\) \{[\s\S]*?\n {6}\} else \{([\s\S]*?)\n {6}\}/,
  )
  assert.ok(switchMatch, 'could not locate the switchAction immediate/else branch in TerminalView.vue')
  const deferredBranch = switchMatch[1]
  assert.ok(
    !deferredBranch.includes('postTerminalScrollBottom'),
    'deferred (working-agent) switch branch must not call postTerminalScrollBottom — ' +
      'it would yank scrolled-up users to the bottom on tab switch',
  )
  // The deferred branch should only resize (plus set the pending flag).
  assert.ok(
    deferredBranch.includes('scheduleTerminalResize'),
    'deferred branch should still call scheduleTerminalResize so the iframe fits its container',
  )
})

test('scheduleMobileTerminalActivation must not exist (no forced scroll-bottom on mobile switch)', () => {
  // Mobile warm tab switches must follow the same preserve-user-scroll contract
  // as desktop: history=0, scroll-bottom=0, activate=0, resize=1. The previous
  // scheduleMobileTerminalActivation helper sent terminal-activate with
  // scrollToBottom:true plus delayed terminal-scroll-bottom messages, which
  // yanked scrolled-up users. It was removed; this test guards against
  // re-introduction.
  assert.ok(
    !terminalViewSource.includes('scheduleMobileTerminalActivation'),
    'scheduleMobileTerminalActivation must not be defined or called — ' +
      'mobile tab switch must not force scroll-to-bottom',
  )
})

test('the only postTerminalScrollBottom calls are inside the manual refresh path', () => {
  // postTerminalScrollBottom posts a terminal-scroll-bottom message to the
  // iframe. It must ONLY be used by the explicit manual ↻ refresh
  // (refreshTerminalHistory), never by tab-switch or auto-round-complete
  // paths (which use preserveUserScroll instead).
  //
  // We locate the refreshTerminalHistory function body via brace matching and
  // assert every postTerminalScrollBottom( call falls inside it.
  const fnStart = terminalViewSource.indexOf('function refreshTerminalHistory(')
  assert.ok(fnStart !== -1, 'refreshTerminalHistory function must exist')

  // Find the opening brace of the function body.
  const braceStart = terminalViewSource.indexOf('{', fnStart)
  assert.ok(braceStart !== -1, 'refreshTerminalHistory must have a body')

  // Walk forward counting braces to find the matching closing brace.
  let depth = 0
  let fnEnd = -1
  for (let i = braceStart; i < terminalViewSource.length; i++) {
    if (terminalViewSource[i] === '{') depth++
    else if (terminalViewSource[i] === '}') {
      depth--
      if (depth === 0) {
        fnEnd = i
        break
      }
    }
  }
  assert.ok(fnEnd !== -1, 'could not find closing brace of refreshTerminalHistory')

  const fnBody = terminalViewSource.slice(braceStart, fnEnd + 1)

  // Collect all postTerminalScrollBottom( call sites in the whole file,
  // excluding the function definition itself (which is preceded by `function `).
  const allCalls = [...terminalViewSource.matchAll(/(?<!function )postTerminalScrollBottom\(/g)]
  assert.ok(allCalls.length > 0, 'postTerminalScrollBottom should still exist for manual refresh')

  for (const match of allCalls) {
    const callIdx = match.index
    const insideManual = callIdx > braceStart && callIdx < fnEnd
    assert.ok(
      insideManual,
      `postTerminalScrollBottom must only be called from refreshTerminalHistory ` +
        `(manual refresh), but found a call outside it at offset ${callIdx}`,
    )
  }

  // Sanity: the manual refresh body itself must contain at least one call.
  assert.ok(
    fnBody.includes('postTerminalScrollBottom('),
    'refreshTerminalHistory should call postTerminalScrollBottom to force latest output',
  )
})
