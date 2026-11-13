import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref, watch } from 'vue'
import {
  LEGACY_THEME_STORAGE_KEY, THEME_PALETTES, THEME_STORAGE_KEY,
  readStoredValue, readThemePreference, resolveColorScheme,
  type AppearanceMode, type ThemePalette,
} from '@/features/app-shell/themePreference'
import type { AppMode, ColorScheme } from '@/types'
import { RESTART_STORAGE_KEY } from '@/utils/serviceRestart'

const STORAGE_KEY_MODE = 'claude_hub_app_mode'

export const useAppStore = defineStore('app', () => {
  const mode = ref<AppMode>((readStoredValue(STORAGE_KEY_MODE) as AppMode) || 'terminal')
  const preference = ref(readThemePreference())
  const systemQuery = window.matchMedia('(prefers-color-scheme: dark)')
  const systemDark = ref(systemQuery.matches)
  const appearanceMode = computed(() => preference.value.mode)
  const themePalette = computed(() => preference.value.palette)
  const themePaletteName = computed(() => THEME_PALETTES.find(theme => theme.id === themePalette.value)!.name)
  const colorScheme = computed<ColorScheme>(() => resolveColorScheme(appearanceMode.value, systemDark.value))
  const appearanceVisible = ref(false)
  const themePersistenceError = ref(false)

  function onSystemChange(event: MediaQueryListEvent) { systemDark.value = event.matches }
  function onStorageChange(event: StorageEvent) {
    if (event.storageArea !== localStorage) return
    if (event.key === THEME_STORAGE_KEY || event.key === LEGACY_THEME_STORAGE_KEY || event.key === null) {
      preference.value = readThemePreference()
      themePersistenceError.value = false
    }
  }
  systemQuery.addEventListener('change', onSystemChange)
  window.addEventListener('storage', onStorageChange)
  onScopeDispose(() => {
    systemQuery.removeEventListener('change', onSystemChange)
    window.removeEventListener('storage', onStorageChange)
  })

  // One owner for the main app and lightweight standalone readers. Keep the
  // resolved light/dark attribute stable for existing terminal/code consumers.
  watch([colorScheme, themePalette], ([scheme, palette]) => {
    const root = document.documentElement
    root.dataset.theme = scheme
    root.dataset.palette = palette
    const chromeColor = getComputedStyle(root).getPropertyValue('--ch-color-app-bg').trim()
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', chromeColor)
  }, { immediate: true, flush: 'sync' })
  const feishuBotSettingsVisible = ref(false)
  const scheduledTasksVisible = ref(false)
  const restartDialogVisible = ref(!!sessionStorage.getItem(RESTART_STORAGE_KEY))
  const scheduledTaskCreateTargetTabId = ref<string | null>(null)
  const scheduledTaskCreateRequest = ref(0)

  function setMode(nextMode: AppMode) {
    mode.value = nextMode
    localStorage.setItem(STORAGE_KEY_MODE, nextMode)
  }

  function setAppearance(mode: AppearanceMode, palette: ThemePalette = themePalette.value) {
    preference.value = { mode, palette }
    try {
      localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(preference.value))
      themePersistenceError.value = false
    } catch {
      // Storage can be blocked; the current page should still be usable.
      themePersistenceError.value = true
    }
  }

  function openAppearance() { appearanceVisible.value = true }

  function openScheduledTasks(targetTabId: string | null = null) {
    scheduledTaskCreateTargetTabId.value = targetTabId
    if (targetTabId) scheduledTaskCreateRequest.value += 1
    scheduledTasksVisible.value = true
  }

  function openFeishuBotSettings() {
    feishuBotSettingsVisible.value = true
  }

  function closeFeishuBotSettings() {
    feishuBotSettingsVisible.value = false
  }

  function closeScheduledTasks() {
    scheduledTasksVisible.value = false
    scheduledTaskCreateTargetTabId.value = null
  }

  return {
    mode,
    colorScheme,
    feishuBotSettingsVisible,
    scheduledTasksVisible,
    restartDialogVisible,
    scheduledTaskCreateTargetTabId,
    scheduledTaskCreateRequest,
    setMode,
    appearanceMode,
    themePalette,
    themePaletteName,
    appearanceVisible,
    themePersistenceError,
    setAppearance,
    openAppearance,
    openScheduledTasks,
    closeScheduledTasks,
    openFeishuBotSettings,
    closeFeishuBotSettings,
  }
})
