import type { AgentType } from '@/types'

// Agent TUI tabs (Claude/Codex/Cursor) render their UI through relative-cursor
// live writes. A full tmux snapshot replay while the agent is actively writing
// corrupts xterm's screen state, so tab-switch history replay is gated on the
// agent being idle/attention (stable screen). Plain `terminal` tabs have no
// such constraint and always replay on switch.
export function isAgentTuiTab(agentType?: AgentType): boolean {
  return agentType === 'claude' || agentType === 'codex' || agentType === 'cursor'
}

// Pure decision: is it safe to full-replay tmux history when switching to this
// tab? Agent TUI tabs only replay when the agent screen is stable (idle or
// waiting for input). While `working`, live relative-cursor writes are in
// flight and a snapshot replay would corrupt xterm's screen. Plain `terminal`
// tabs always replay.
export function shouldReplayHistoryOnSwitch(
  agentType: AgentType | undefined,
  status: string | null,
): boolean {
  if (!isAgentTuiTab(agentType)) return true
  return status === 'idle' || status === 'attention'
}

// On tab switch, decide whether to replay history immediately or defer it
// until the agent reaches a stable state. The deferred case sets a
// pending-safe-replay flag that the status-change watcher fulfills.
export type SwitchReplayAction = 'immediate' | 'defer'

export function decideSwitchReplay(
  agentType: AgentType | undefined,
  status: string | null,
): SwitchReplayAction {
  return shouldReplayHistoryOnSwitch(agentType, status) ? 'immediate' : 'defer'
}

// On agent status change, decide whether to fire a history replay. Two paths:
//   1. round-complete: last status was `working` and new status is stable
//      (idle/attention) — the agent finished a turn.
//   2. deferred-switch-fulfill: a switch earlier set pendingSafeReplay (because
//      the status was working or unknown at switch time), and the first stable
//      status has now arrived — even if no working→stable edge was observed
//      (the poll may have missed the working state).
// Both paths clear the pending flag.
export function decideStatusChangeReplay(
  lastStatus: string | null,
  newStatus: string | null,
  pendingSafeReplay: boolean,
): { replay: boolean; clearPending: boolean } {
  const stable = newStatus === 'idle' || newStatus === 'attention'
  const roundComplete = lastStatus === 'working' && stable
  const deferredFulfilled = pendingSafeReplay && stable
  if (roundComplete || deferredFulfilled) {
    return { replay: true, clearPending: true }
  }
  return { replay: false, clearPending: false }
}
