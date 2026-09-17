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
  const recentCwds = computed<string[]>(() => {
    // Depend on the shared revision so writes and other windows' storage
    // events invalidate the list.
    void revision.value
    return load(scope.value)
  })

  function addCwd(rawPath: string | undefined) {
    const path = rawPath?.trim()
    // Skip blanks and the bare home sentinel the remote flow defaults to.
    if (!path || path === '~') return
    const next = [path, ...load(scope.value).filter(p => p !== path)].slice(0, MAX_ENTRIES)
    revision.value += 1
    if (storageBroken) {
      memoryFallback.set(scope.value, next)
      return
    }
    try {
      localStorage.setItem(storageKey(scope.value), JSON.stringify(next))
    } catch {
      // Persisted storage unavailable; keep the entry for this session.
      storageBroken = true
      memoryFallback.set(scope.value, next)
    }
  }

  const mostRecentCwd = computed(() => recentCwds.value[0] ?? '')

  return { recentCwds, mostRecentCwd, addCwd }
}
