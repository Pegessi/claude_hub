import type { TimelinePart, TimelineSubthread, TimelineSubthreadPart } from './agentStreamTimeline'

export type ThinkingState = 'active' | 'done' | 'interrupted'
type SourcePart = TimelinePart | TimelineSubthreadPart

export type ThinkingDisplayPart<T extends SourcePart> =
  | Exclude<T, { kind: 'thinking' }>
  | { kind: 'thinking'; key: string; text: string; state: ThinkingState }
  | { kind: 'reasoning_status'; key: string; state: ThinkingState }

/** Only adapter-authored reasoning statuses participate; ordinary status text stays intact. */
function reasoningState(part: SourcePart): ThinkingState | null {
  if (part.kind !== 'status') return null
  // The prefix supports legacy reduced snapshots; explicit metadata always wins.
  if (part.providerStatus !== 'reasoning' &&
      !(part.providerStatus === undefined && part.messageId?.startsWith('reasoning:'))) return null
  if (part.text === 'Thinking…') return 'active'
  if (part.text === 'Done thinking') return 'done'
  if (part.text === 'Thinking interrupted') return 'interrupted'
  return null
}

function isErrorPart(part: SourcePart): boolean {
  return part.kind === 'error' ||
    (part.kind === 'status' && (part.providerStatus === 'error' || ('isError' in part && part.isError === true)))
}

/**
 * Compact adjacent reasoning in one turn or child thread without mutating the log.
 * Text/tools/approvals/errors and thread regions are hard boundaries. Plaintext
 * has no universal end event, so only a trailing, nonterminal run can animate.
 * Snapshot statuses may precede their text even after completion: their explicit
 * state takes precedence over structural inference.
 */
export function presentThinkingParts<T extends SourcePart>(
  parts: readonly T[],
  terminal: Exclude<ThinkingState, 'active'> | null,
): ThinkingDisplayPart<T>[] {
  const result: ThinkingDisplayPart<T>[] = []
  // Child activity is a display boundary, not evidence that its parent stopped
  // thinking. A trailing region must not finish the parent's current phase.
  let lastOwnPart = parts.length - 1
  while (lastOwnPart >= 0 && parts[lastOwnPart].kind === 'subagents') lastOwnPart--
  let run: { key: string; texts: string[]; state: ThinkingState | null } | null = null

  function flush(atEnd: boolean, interrupted = false): void {
    if (!run) return
    let state = run.state ?? 'active'
    if (state === 'active') {
      if (interrupted || (atEnd && terminal === 'interrupted')) state = 'interrupted'
      else if (!atEnd || terminal === 'done') state = 'done'
    }
    const text = run.texts.join('\n\n')
    result.push(text.trim()
      ? { kind: 'thinking', key: run.key, text, state }
      : { kind: 'reasoning_status', key: run.key, state })
    run = null
  }

  for (const [index, part] of parts.entries()) {
    const state = reasoningState(part)
    if (part.kind === 'thinking' || state !== null) {
      // Keep interrupted attempts distinct from a preceding successful opaque run.
      if (state === 'interrupted' && run && run.texts.length === 0) flush(false)
      run ??= { key: part.key, texts: [], state: null }
      if (part.kind === 'thinking') run.texts.push(part.text)
      else run.state = state
      if (state === 'interrupted') flush(false)
    } else {
      const interrupted = isErrorPart(part) || (terminal === 'interrupted' &&
        part.kind === 'status' && part.providerStatus === 'subagent_lifecycle')
      flush(index > lastOwnPart, interrupted)
      result.push(part as Exclude<T, { kind: 'thinking' }>)
    }
  }
  flush(true)
  return result
}

/** A parent turn rerenders on any child's event; unchanged siblings need no scan. */
export function createSubthreadThinkingPresenter() {
  const cache = new WeakMap<TimelineSubthread, {
    revision: number
    length: number
    parts: ThinkingDisplayPart<TimelineSubthreadPart>[]
  }>()
  return (thread: TimelineSubthread): ThinkingDisplayPart<TimelineSubthreadPart>[] => {
    const previous = cache.get(thread)
    // Parent sendInput can append an instruction without a child-scoped event.
    // Tool/result objects are shared with the projection and need no copying.
    if (previous?.revision === thread.renderRevision && previous.length === thread.parts.length) {
      return previous.parts
    }
    const terminal = thread.lifecycleStatus === 'failed' || thread.lifecycleStatus === 'cancelled'
      ? 'interrupted' : thread.lifecycleStatus === 'completed' ? 'done' : null
    const parts = presentThinkingParts(thread.parts, terminal)
    cache.set(thread, { revision: thread.renderRevision, length: thread.parts.length, parts })
    return parts
  }
}
