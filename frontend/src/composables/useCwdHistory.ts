import { computed, ref } from 'vue'
import type { Ref } from 'vue'

/**
 * Recently-used working directories for the Create Session dialog.
 *
 * History is per launch scope — 'local', or 'remote:<profileId>' so remote
 * directories from different servers never get mixed — and persisted in
 * localStorage (one list per scope). The newest entry is first; duplicates
 * move to the front and the list is capped.
 *
 * `removeCwd` drops an entry from the persisted list and additionally dismisses
 * it for the page session, so callers that merge in open-tab paths can hide it
 * until a reload. `addCwd` clears that dismissal.
 *
 * Pass a ref to the current scope. The exposed list is a computed that reads
 * localStorage synchronously on every scope change (no async watcher gap), so
 * callers can switch target and read the new scope's most-recent entry in the
 * same tick. A module-wide revision (also bumped by `storage` events from
 * other windows) keeps every instance in sync.
 */

const STORAGE_PREFIX = 'claude-hub:cwd-history:'
const MAX_ENTRIES = 8

// Used only when localStorage itself throws (quota / disabled / private mode).
const memoryFallback = new Map<string, string[]>()
let storageBroken = false

// Paths the user dismissed from the picker for the current page session.
// Dismissal is intentionally not persisted: it hides an entry that is still
// backed by an open tab until the page reloads, and `addCwd` clears it the
// moment the path is used again.
const dismissed = new Map<string, Set<string>>()

function dismissedFor(scope: string): Set<string> {
  let set = dismissed.get(scope)
  if (!set) {
    set = new Set()
    dismissed.set(scope, set)
  }
  return set
}

// Shared by every composable instance; bumped on local writes and on
// cross-window `storage` events so all lists re-read together.
const revision = ref(0)

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key?.startsWith(STORAGE_PREFIX)) revision.value += 1
  })
}

function storageKey(scope: string): string {
  return `${STORAGE_PREFIX}${scope}`
}

function load(scope: string): string[] {
  if (storageBroken) return memoryFallback.get(scope) ?? []
  try {
    const raw = localStorage.getItem(storageKey(scope))
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((v): v is string => typeof v === 'string' && v.trim().length > 0)
  } catch {
    storageBroken = true
    return memoryFallback.get(scope) ?? []
  }
}

export function useCwdHistory(scope: Ref<string>) {
  const dismissedCwds = computed<string[]>(() => {
    void revision.value
    return [...dismissedFor(scope.value)]
  })

  const recentCwds = computed<string[]>(() => {
    // Depend on the shared revision so writes and other windows' storage
    // events invalidate the list.
    void revision.value
    const hidden = dismissedFor(scope.value)
    return load(scope.value).filter(path => !hidden.has(path))
  })

  function persist(scopeValue: string, next: string[]) {
    revision.value += 1
    if (storageBroken) {
      if (next.length) memoryFallback.set(scopeValue, next)
      else memoryFallback.delete(scopeValue)
      return
    }
    try {
      if (next.length) localStorage.setItem(storageKey(scopeValue), JSON.stringify(next))
      else localStorage.removeItem(storageKey(scopeValue))
    } catch {
      // Persisted storage unavailable; keep the entry for this session.
      storageBroken = true
      if (next.length) memoryFallback.set(scopeValue, next)
      else memoryFallback.delete(scopeValue)
    }
  }

  function addCwd(rawPath: string | undefined) {
    const path = rawPath?.trim()
    // Skip blanks and the bare home sentinel the remote flow defaults to.
    if (!path || path === '~') return
    // Re-using a previously dismissed path restores it everywhere.
    dismissedFor(scope.value).delete(path)
    const next = [path, ...load(scope.value).filter(p => p !== path)].slice(0, MAX_ENTRIES)
    persist(scope.value, next)
  }

  function removeCwd(rawPath: string | undefined) {
    const path = rawPath?.trim()
    if (!path) return
    dismissedFor(scope.value).add(path)
    persist(scope.value, load(scope.value).filter(p => p !== path))
  }

  const mostRecentCwd = computed(() => recentCwds.value[0] ?? '')

  return { recentCwds, dismissedCwds, mostRecentCwd, addCwd, removeCwd }
}
