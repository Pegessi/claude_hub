/**
 * Pure decision logic for the terminal iframe cache.
 *
 * The cache has two lists that MUST be kept distinct:
 *
 *  - `cachedTabIds`: the v-for render list. It stays in stable insertion
 *    order — never reordered on tab switch — because moving an <iframe> in
 *    the DOM makes Chromium rebuild its browsing context, which reloads
 *    xterm and wipes the buffer/scroll state. This list only grows (append)
 *    and shrinks (evict by id).
 *
 *  - `tabRecency`: LRU order (most-recently-used at the end). Updated on
 *    every cache hit/miss. Used ONLY to pick the eviction victim when the
 *    cache exceeds the cap. Never used as a render order.
 *
 * The pure function `computeCacheUpdate` takes the current lists and the
 * tab being activated and returns the next lists plus the evicted ids, so
 * the component can apply them (and run side effects like resetting
 * connecting state) without burying the policy inside Vue reactivity.
 */

export type LayoutType = '1x1' | string

export interface CacheState {
  /** Stable insertion-order render list. */
  cachedTabIds: string[]
  /** LRU order, most-recently-used at the end. */
  tabRecency: string[]
}

export interface CacheUpdateResult {
  cachedTabIds: string[]
  tabRecency: string[]
  /** Tab ids that were evicted from the cache (caller resets their state). */
  evicted: string[]
}

/**
 * Compute the next cache state after activating `tabId`.
 *
 * Rules:
 *  - In a non-1x1 (split) layout, only the active tab is cached; everything
 *    else is evicted (hidden iframes must not stay attached to tmux).
 *  - In a 1x1 layout:
 *    1. Move `tabId` to the end of `tabRecency` (MRU).
 *    2. If `tabId` is not already in `cachedTabIds`, append it (never
 *       reorder existing entries — that would reload iframes).
 *    3. While `cachedTabIds.length > maxCached`, evict the LRU tab that is
 *       NOT the active tab (front of `tabRecency`, skipping `tabId`).
 *
 * This function is pure: it returns new arrays and never mutates its inputs.
 */
export function computeCacheUpdate(
  state: CacheState,
  tabId: string,
  layoutType: LayoutType,
  maxCached: number,
): CacheUpdateResult {
  if (!tabId) {
    return { ...state, evicted: [] }
  }

  // Split layout: keep only the active tab.
  if (layoutType !== '1x1') {
    const evicted = state.cachedTabIds.filter((id) => id !== tabId)
    return {
      cachedTabIds: [tabId],
      tabRecency: [tabId],
      evicted,
    }
  }

  // 1. LRU recency: move tabId to the end (most-recently-used).
  const recencyWithoutCurrent = state.tabRecency.filter((id) => id !== tabId)
  const nextRecency = [...recencyWithoutCurrent, tabId]

  // 2. Render list in stable insertion order: append only if new.
  const alreadyCached = state.cachedTabIds.includes(tabId)
  let nextCached = alreadyCached
    ? state.cachedTabIds
    : [...state.cachedTabIds, tabId]

  // 3. Evict LRU tabs over the cap, never the active tab.
  const evicted: string[] = []
  let recencyForEviction = [...nextRecency]
  while (nextCached.length > maxCached) {
    const victim = recencyForEviction.find((id) => id !== tabId)
    if (!victim) break
    evicted.push(victim)
    nextCached = nextCached.filter((id) => id !== victim)
    recencyForEviction = recencyForEviction.filter((id) => id !== victim)
  }

  // Keep tabRecency in sync with cachedTabIds: drop any recency entries that
  // are no longer cached (evicted ones).
  const finalRecency = nextRecency.filter((id) => nextCached.includes(id))

  return {
    cachedTabIds: nextCached,
    tabRecency: finalRecency,
    evicted,
  }
}
