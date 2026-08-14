import { computed, ref } from 'vue'
import type { AgentType } from '@/types'

type LaunchEnv = Record<string, string>

export interface LaunchEnvPreset {
  id: string
  name: string
  text: string
}

interface StoredLaunchEnvPreset {
  id: string
  name: string
  text: string
}

interface BackendPresetsResponse {
  custom_presets: StoredLaunchEnvPreset[]
  hidden_builtin_ids: string[]
}

const STORAGE_KEY = 'claude-hub.launch-env-presets'
const HIDDEN_KEY = 'claude-hub.launch-env-hidden'
const API_BASE = '/api/env-presets'

export const DEFAULT_CLAUDE_ENV_PRESET_ID = 'volcengine-coding-plan'

export const BUILT_IN_PRESET_IDS = [
  'none',
  'local-proxy-7890',
  'socks-proxy-1080',
  'volcengine-coding-plan',
]

const builtInPresets: LaunchEnvPreset[] = [
  {
    id: 'none',
    name: 'No custom env',
    text: '',
  },
  {
    id: 'local-proxy-7890',
    name: 'Local proxy :7890',
    text: [
      'HTTP_PROXY=http://127.0.0.1:7890',
      'HTTPS_PROXY=http://127.0.0.1:7890',
      'ALL_PROXY=socks5://127.0.0.1:7890',
      'NO_PROXY=localhost,127.0.0.1,::1',
    ].join('\n'),
  },
  {
    id: 'socks-proxy-1080',
    name: 'SOCKS proxy :1080',
    text: [
      'ALL_PROXY=socks5://127.0.0.1:1080',
      'NO_PROXY=localhost,127.0.0.1,::1',
    ].join('\n'),
  },
  {
    id: 'volcengine-coding-plan',
    name: 'Volcengine Coding Plan',
    text: [
      'ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/coding',
      'ANTHROPIC_MODEL=doubao-seed-2.0-code',
      'ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-code',
      'ANTHROPIC_DEFAULT_SONNET_MODEL=doubao-seed-2.0-code',
      'ANTHROPIC_DEFAULT_HAIKU_MODEL=doubao-seed-2.0-code',
      'CLAUDE_CODE_SUBAGENT_MODEL=doubao-seed-2.0-code',
      'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1',
    ].join('\n'),
  },
]

// Module-level singleton state (shared across all consumers)
const customPresets = ref<LaunchEnvPreset[]>(loadCustomPresetsFromStorage())
const hiddenIds = ref<Set<string>>(loadHiddenIdsFromStorage())
const ready = ref(false)
let loadPromise: Promise<void> | null = null

function loadCustomPresetsFromStorage(): LaunchEnvPreset[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as StoredLaunchEnvPreset[]
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter(item => item && typeof item.name === 'string' && typeof item.text === 'string')
      .map(item => ({
        id: typeof item.id === 'string' ? item.id : crypto.randomUUID(),
        name: item.name,
        text: item.text,
      }))
  } catch {
    return []
  }
}

function loadHiddenIdsFromStorage(): Set<string> {
  if (typeof window === 'undefined') return new Set()
  try {
    const raw = window.localStorage.getItem(HIDDEN_KEY)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return new Set()
    return new Set(arr.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

function persistCustomPresetsToStorage() {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(customPresets.value))
}

function persistHiddenIdsToStorage() {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenIds.value]))
}

function _authHeaders(): Record<string, string> {
  return { 'Content-Type': 'application/json' }
}

/**
 * Load presets from backend and perform one-time localStorage migration if needed.
 * Backend is authoritative; localStorage data is migrated on first load if backend
 * is empty. Safe to call multiple times (guarded by loadPromise).
 */
async function _loadFromBackend(): Promise<void> {
  if (typeof window === 'undefined') {
    ready.value = true
    return
  }
  try {
    const response = await fetch(API_BASE, { headers: _authHeaders() })
    if (!response.ok) {
      console.warn('[env-presets] Backend fetch failed, using localStorage only:', response.status)
      ready.value = true
      return
    }
    const data: BackendPresetsResponse = await response.json()
    const backendCustom = Array.isArray(data.custom_presets) ? data.custom_presets : []
    const backendHidden = Array.isArray(data.hidden_builtin_ids)
      ? new Set(data.hidden_builtin_ids)
      : new Set<string>()

    const localCustom = customPresets.value
    const localHidden = new Set(hiddenIds.value)

    // Migration: backend empty but localStorage has data → bulk-import to backend
    const needsMigration = backendCustom.length === 0 && localCustom.length > 0
    const hasLocalOnlyHidden = backendHidden.size === 0 && localHidden.size > 0

    if (needsMigration || hasLocalOnlyHidden) {
      const toImport: StoredLaunchEnvPreset[] = []
      const backendIds = new Set(backendCustom.map(p => p.id))
      for (const preset of localCustom) {
        if (!backendIds.has(preset.id)) {
          toImport.push({ id: preset.id, name: preset.name, text: preset.text })
        }
      }
      const hiddenToImport = [...localHidden].filter(id => !backendHidden.has(id))

      if (toImport.length > 0 || hiddenToImport.length > 0) {
        try {
          const importResp = await fetch(`${API_BASE}/bulk-import`, {
            method: 'POST',
            headers: _authHeaders(),
            body: JSON.stringify({
              custom_presets: toImport,
              hidden_builtin_ids: hiddenToImport,
            }),
          })
          if (importResp.ok) {
            const imported: BackendPresetsResponse = await importResp.json()
            customPresets.value = (imported.custom_presets || []).map(p => ({
              id: p.id,
              name: p.name,
              text: p.text,
            }))
            hiddenIds.value = new Set(imported.hidden_builtin_ids || [])
            persistCustomPresetsToStorage()
            persistHiddenIdsToStorage()
            ready.value = true
            return
          }
        } catch (e) {
          console.warn('[env-presets] Migration to backend failed:', e)
        }
      }
    }

    // Backend is authoritative: replace local state with backend data
    customPresets.value = backendCustom.map(p => ({ id: p.id, name: p.name, text: p.text }))
    hiddenIds.value = backendHidden
    persistCustomPresetsToStorage()
    persistHiddenIdsToStorage()
  } catch (e) {
    console.warn('[env-presets] Backend unavailable, using localStorage only:', e)
  }
  ready.value = true
}

function ensureLoaded(): Promise<void> {
  if (ready.value) return Promise.resolve()
  if (!loadPromise) {
    loadPromise = _loadFromBackend()
  }
  return loadPromise
}

function stripShellQuotes(value: string): string {
  if (value.length < 2) return value
  const first = value[0]
  const last = value[value.length - 1]
  if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
    return value.slice(1, -1)
  }
  return value
}

export function parseLaunchEnv(text: string): LaunchEnv | undefined {
  const env: LaunchEnv = {}
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const normalized = line.startsWith('export ') ? line.slice(7).trim() : line
    const equalsIndex = normalized.indexOf('=')
    if (equalsIndex <= 0) continue
    const name = normalized.slice(0, equalsIndex).trim()
    if (!name) continue
    env[name] = stripShellQuotes(normalized.slice(equalsIndex + 1).trim())
  }
  return Object.keys(env).length ? env : undefined
}

export function defaultLaunchEnvPresetForAgent(agentType: AgentType): string {
  return agentType === 'claude' ? DEFAULT_CLAUDE_ENV_PRESET_ID : 'none'
}

/**
 * Serialize a launch-env dict back into the `KEY=VALUE` newline text format
 * used by the textarea / preset editors. Inverse of {@link parseLaunchEnv}.
 */
export function serializeLaunchEnv(env: LaunchEnv | null | undefined): string {
  if (!env) return ''
  return Object.entries(env)
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')
}

export function useLaunchEnvPresets() {
  // Kick off async backend load; does not block — localStorage data is already
  // available as initial state for instant rendering.
  ensureLoaded()

  const envPresets = computed(() => {
    const all = [...builtInPresets, ...customPresets.value]
    return all.filter(preset => !hiddenIds.value.has(preset.id))
  })

  function getPresetText(id: string): string | null {
    return envPresets.value.find(preset => preset.id === id)?.text ?? null
  }

  function defaultPresetTextForAgent(agentType: AgentType): string {
    const presetId = defaultLaunchEnvPresetForAgent(agentType)
    return getPresetText(presetId) ?? ''
  }

  /**
   * Save a custom preset (create or update). Persists optimistically to
   * localStorage first, then syncs to backend. Returns the saved preset or
   * null if validation fails.
   */
  async function savePreset(
    name: string,
    text: string,
    id?: string,
  ): Promise<LaunchEnvPreset | null> {
    const trimmedName = name.trim()
    if (!trimmedName || !text.trim()) return null

    const existing = customPresets.value.find(
      preset => preset.id === id || preset.name === trimmedName,
    )
    let preset: LaunchEnvPreset
    if (existing) {
      preset = existing
      preset.name = trimmedName
      preset.text = text
    } else {
      preset = {
        id: id ?? `custom-${Date.now().toString(36)}`,
        name: trimmedName,
        text,
      }
      customPresets.value.push(preset)
    }
    // Optimistic local persistence
    persistCustomPresetsToStorage()

    // Sync to backend
    try {
      const response = await fetch(`${API_BASE}/${encodeURIComponent(preset.id)}`, {
        method: 'PUT',
        headers: _authHeaders(),
        body: JSON.stringify({ name: trimmedName, text }),
      })
      if (response.ok) {
        const saved = await response.json()
        preset.id = saved.id
        preset.name = saved.name
        preset.text = saved.text
        persistCustomPresetsToStorage()
      } else {
        console.warn('[env-presets] Backend save failed, kept local copy:', response.status)
      }
    } catch (e) {
      console.warn('[env-presets] Backend save failed (offline?), kept local copy:', e)
    }

    return preset
  }

  /**
   * Delete a custom preset or hide a built-in preset. Returns true on success.
   */
  async function deletePreset(id: string): Promise<boolean> {
    if (id === 'none') return false
    const inCustomIdx = customPresets.value.findIndex(preset => preset.id === id)
    if (inCustomIdx >= 0) {
      const removed = customPresets.value.splice(inCustomIdx, 1)[0]
      persistCustomPresetsToStorage()
      try {
        const response = await fetch(`${API_BASE}/${encodeURIComponent(id)}`, {
          method: 'DELETE',
          headers: _authHeaders(),
        })
        if (!response.ok && response.status !== 404) {
          console.warn('[env-presets] Backend delete failed:', response.status)
          // Restore local copy on unexpected failure
          customPresets.value.splice(inCustomIdx, 0, removed)
          persistCustomPresetsToStorage()
          return false
        }
      } catch (e) {
        console.warn('[env-presets] Backend delete failed (offline?):', e)
        // Keep local deletion in offline mode
      }
      return true
    }
    const inBuiltin = builtInPresets.find(preset => preset.id === id)
    if (inBuiltin) {
      hiddenIds.value.add(id)
      persistHiddenIdsToStorage()
      try {
        await fetch(`${API_BASE}/hidden/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: _authHeaders(),
          body: JSON.stringify({ hidden: true }),
        })
      } catch (e) {
        console.warn('[env-presets] Backend hide failed (offline?):', e)
      }
      return true
    }
    return false
  }

  /**
   * Restore a previously hidden built-in preset.
   */
  async function unhidePreset(id: string): Promise<boolean> {
    if (!hiddenIds.value.has(id)) return false
    hiddenIds.value.delete(id)
    persistHiddenIdsToStorage()
    try {
      await fetch(`${API_BASE}/hidden/${encodeURIComponent(id)}`, {
        method: 'PUT',
        headers: _authHeaders(),
        body: JSON.stringify({ hidden: false }),
      })
    } catch (e) {
      console.warn('[env-presets] Backend unhide failed (offline?):', e)
    }
    return true
  }

  return {
    envPresets,
    getPresetText,
    defaultPresetTextForAgent,
    savePreset,
    deletePreset,
    unhidePreset,
    ready,
    ensureLoaded,
  }
}
