import { inject, type InjectionKey } from 'vue'

import type { SubagentJumpChip } from '../presentation/subagentJump'
import type { useParentSubagents } from './useParentSubagents'

/** Per-pane sub-agent state. TerminalPane owns the single poller and provides
 *  it to both the top-right dock pill and the Chat body's inline links, so a
 *  pane never polls twice. */
export type SubagentDock = ReturnType<typeof useParentSubagents>

export const subagentDockKey: InjectionKey<SubagentDock> = Symbol('subagentDock')

export function useSubagentDock(): SubagentDock | null {
  return inject(subagentDockKey, null)
}

export function findDockChip(dock: SubagentDock | null, subagentId: string | null): SubagentJumpChip | null {
  if (!dock || !subagentId) return null
  return dock.chips.value.find(chip => chip.subagentId === subagentId) ?? null
}
