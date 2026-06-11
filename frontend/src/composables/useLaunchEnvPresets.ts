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

const STORAGE_KEY = 'claude-hub.launch-env-presets'
const HIDDEN_KEY = 'claude-hub.launch-env-hidden'
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

const customPresets = ref<LaunchEnvPreset[]>(loadCustomPresets())
const hiddenIds = ref<Set<string>>(loadHiddenIds())

function loadCustomPresets(): LaunchEnvPreset[] {
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

function loadHiddenIds(): Set<string> {
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

function persistCustomPresets() {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(customPresets.value))
}

function persistHiddenIds() {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenIds.value]))
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

export function useLaunchEnvPresets() {
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

  function savePreset(name: string, text: string, id?: string): LaunchEnvPreset | null {
    const trimmedName = name.trim()
    if (!trimmedName || !text.trim()) return null
    const existing = customPresets.value.find(preset => preset.id === id || preset.name === trimmedName)
    if (existing) {
      existing.name = trimmedName
      existing.text = text
      persistCustomPresets()
      return existing
    }
    const preset = {
      id: `custom-${Date.now().toString(36)}`,
      name: trimmedName,
      text,
    }
    customPresets.value.push(preset)
    persistCustomPresets()
    return preset
  }

  function deletePreset(id: string) {
    if (id === 'none') return false
    const inCustom = customPresets.value.findIndex(preset => preset.id === id)
    if (inCustom >= 0) {
      customPresets.value.splice(inCustom, 1)
      persistCustomPresets()
      return true
    }
    const inBuiltin = builtInPresets.find(preset => preset.id === id)
    if (inBuiltin) {
      hiddenIds.value.add(id)
      persistHiddenIds()
      return true
    }
    return false
  }

  return {
    envPresets,
    getPresetText,
    defaultPresetTextForAgent,
    savePreset,
    deletePreset,
  }
}
