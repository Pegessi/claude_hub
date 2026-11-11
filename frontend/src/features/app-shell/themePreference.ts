import type { ColorScheme } from '@/types'

export const THEME_STORAGE_KEY = 'claude_hub_appearance'
export const LEGACY_THEME_STORAGE_KEY = 'claude_hub_color_scheme'
export const THEME_PALETTES = [
  { id: 'classic', name: 'Classic', description: 'Original carbon & clay', sample: 'CH' },
  { id: 'contrast', name: 'Contrast', description: 'Crisp edges, clear focus', sample: 'Aa' },
  { id: 'mono', name: 'Mono', description: 'Black, white, nothing extra', sample: 'Aa' },
  { id: 'studio', name: 'Studio', description: 'Quiet layers, softer edges', sample: 'Aa' },
  { id: 'carbon', name: 'Carbon', description: 'Graphite, raised surfaces', sample: 'Aa' },
  { id: 'zenless', name: 'Zenless', description: 'Zenless Zone Zero · urban signal', sample: 'ZZ' },
  { id: 'frontier', name: 'Frontier', description: 'Red Dead Redemption · western', sample: 'RDR' },
  { id: 'los-santos', name: 'Los Santos', description: 'GTA V · city nights', sample: 'V' },
] as const
export type ThemePalette = typeof THEME_PALETTES[number]['id']
export type AppearanceMode = ColorScheme | 'system'
export interface ThemePreference {
  mode: AppearanceMode
  palette: ThemePalette
}

export function readStoredValue(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

export function parseThemePreference(value: string | null, legacy: string | null = null): ThemePreference {
  const fallback: ThemePreference = { mode: legacy === 'light' ? 'light' : 'dark', palette: 'classic' }
  if (!value) return fallback
  try {
    const parsed: unknown = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object') return fallback
    const { mode, palette } = parsed as Record<string, unknown>
    return {
      mode: mode === 'system' || mode === 'light' || mode === 'dark' ? mode : fallback.mode,
      palette: THEME_PALETTES.some(theme => theme.id === palette) ? palette as ThemePalette : 'classic',
    }
  } catch { return fallback }
}

export function readThemePreference(): ThemePreference {
  return parseThemePreference(readStoredValue(THEME_STORAGE_KEY), readStoredValue(LEGACY_THEME_STORAGE_KEY))
}

export function resolveColorScheme(mode: AppearanceMode, systemDark: boolean): ColorScheme {
  return mode === 'system' ? (systemDark ? 'dark' : 'light') : mode
}
