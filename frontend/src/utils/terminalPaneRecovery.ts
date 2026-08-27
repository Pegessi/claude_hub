/** Correlation token for pane history recovery across tab switches and iframe reloads. */
export type PaneRecoveryCorrelation = {
  tabId: string
  tabSwitchGeneration: number
  documentGeneration: number
}

export function bumpIframeDocumentGeneration(
  counts: Record<string, number>,
  tabId: string,
): number {
  const next = (counts[tabId] ?? 0) + 1
  counts[tabId] = next
  return next
}

export function getIframeDocumentGeneration(counts: Record<string, number>, tabId: string): number {
  return counts[tabId] ?? 0
}

/** True when an in-flight recovery for the same tab+document should block a duplicate post. */
export function shouldDedupePaneRecovery(
  inFlight: PaneRecoveryCorrelation | null,
  tabId: string,
  tabSwitchGeneration: number,
  documentGeneration: number,
): boolean {
  if (!inFlight) return false
  return (
    inFlight.tabId === tabId &&
    inFlight.tabSwitchGeneration === tabSwitchGeneration &&
    inFlight.documentGeneration === documentGeneration
  )
}

/** Stale refresh-done from a prior iframe document must never reveal the new document. */
export function isStalePaneRefreshDone(
  correlation: PaneRecoveryCorrelation,
  tabSwitchGeneration: number,
  documentGeneration: number,
): boolean {
  return (
    correlation.tabSwitchGeneration !== tabSwitchGeneration ||
    correlation.documentGeneration !== documentGeneration
  )
}
