/** Bound expensive turn rendering while keeping the complete transcript. */
export const TIMELINE_PAGE_SIZE = 40

export function timelineWindowStart(total: number, start: number | null): number {
  return Math.max(0, Math.min(start ?? total - TIMELINE_PAGE_SIZE, total - 1))
}

/** Retained rows keep older pending approvals and an open editor accessible. */
export function selectTimelineWindow<T>(
  turns: readonly T[],
  start: number,
  retain: (turn: T) => boolean,
): { turn: T; ordinal: number }[] {
  const selected: { turn: T; ordinal: number }[] = []
  for (let ordinal = 0; ordinal < turns.length; ordinal++) {
    const turn = turns[ordinal]
    if (ordinal >= start || retain(turn)) selected.push({ turn, ordinal })
  }
  return selected
}
