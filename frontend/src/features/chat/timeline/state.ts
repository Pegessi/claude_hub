import type { TimelineTurn, TimelineTool, TimelinePart, TimelineSubthread } from './types'

/**
 * Mutable reducer state shared by ``groupEventsIntoTurns`` and
 * ``IncrementalTimelineReducer``. Processing one event mutates the state in
 * place; the reducer owns the lifecycle (reset, processed-count tracking).
 */
export interface ReducerState {
  turns: TimelineTurn[]
  byTurnId: Map<string, TimelineTurn>
  toolsByTurn: Map<string, Map<string, TimelineTool>>
  textChunksByTurn: Map<string, { text: string; count: number }[]>
  legacyCurrent: TimelineTurn | null
  /** Tool/approval call ids already attributed to a turn with a real turn id.
   *  A later null-turn event carrying one of these ids is a provider replay. */
  attributedToolIds: Set<string>
  /** Nested sub-agent threads keyed by turn key → full provider thread id.
   *  One turn-level region is mounted on first reference; child tools resolve
   *  in their own maps so call ids cannot collide with the main thread. */
  threadsByTurn: Map<string, Map<string, SubthreadState>>
  subagentRegionByTurn: Map<
    string,
    Extract<TimelinePart, { kind: 'subagents' }>
  >
}

export interface SubthreadState {
  thread: TimelineSubthread
  tools: TimelineTool[]
  toolMap: Map<string, TimelineTool>
}

export function createReducerState(): ReducerState {
  return {
    turns: [],
    byTurnId: new Map(),
    toolsByTurn: new Map(),
    textChunksByTurn: new Map(),
    legacyCurrent: null,
    attributedToolIds: new Set(),
    threadsByTurn: new Map(),
    subagentRegionByTurn: new Map(),
  }
}
