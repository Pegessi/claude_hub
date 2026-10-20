import type { StreamCapabilities, TerminalTab } from '@/types'

export const CAPABILITIES_CACHE_TTL_MS = 30 * 60 * 1_000

interface Entry {
  configuration: string
  expiresAt: number
  capabilities: StreamCapabilities
}

/** Process-local hints only. Live observation and actions still validate runtime state. */
export function createAgentStreamCapabilitiesCache(
  capacity = 128,
  ttlMs = CAPABILITIES_CACHE_TTL_MS,
  now: () => number = () => Date.now(),
) {
  const entries = new Map<string, Entry>()
  return {
    get(path: string, configuration = ''): StreamCapabilities | undefined {
      const entry = entries.get(path)
      if (!entry) return undefined
      if (entry.configuration !== configuration || entry.expiresAt <= now()) {
        entries.delete(path)
        return undefined
      }
      entries.delete(path)
      entries.set(path, entry)
      return entry.capabilities
    },
    set(path: string, configuration: string, capabilities: StreamCapabilities) {
      entries.delete(path)
      // MASO's current endpoint combines its model catalog with a live service
      // readiness check. Reuse would also freeze that readiness result.
      if (
        !capabilities.structured
        || capabilities.unavailable_reason
        || capabilities.metadata_cacheable === false
        || capabilities.adapter_id === 'maso_http'
        || capacity <= 0
      ) return
      entries.set(path, { configuration, capabilities, expiresAt: now() + ttlMs })
      while (entries.size > capacity) entries.delete(entries.keys().next().value!)
    },
    delete(path: string) { entries.delete(path) },
    clear() { entries.clear() },
  }
}

// Compare configuration exactly, without persisting it or including presentation-only
// changes such as unread timestamps. The signature is never logged or sent elsewhere.
export function streamCapabilityConfiguration(tab: TerminalTab | null): string {
  if (!tab) return ''
  return JSON.stringify([
    tab.id, tab.created_at, tab.agent_type, tab.session_kind, tab.managed_session_id, tab.agent_session_id,
    tab.cwd, tab.solo_mode, tab.target, tab.remote_profile_id, tab.remote_cwd, tab.remote_reconnect,
    tab.cursor_transport, Object.entries(tab.env ?? {}).sort(([a], [b]) => a.localeCompare(b)),
  ])
}

export const agentStreamCapabilitiesCache = createAgentStreamCapabilitiesCache()
