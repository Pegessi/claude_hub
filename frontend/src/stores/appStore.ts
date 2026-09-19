import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { AppMode, ColorScheme } from '@/types'

const STORAGE_KEY_MODE = 'claude_hub_app_mode'
const STORAGE_KEY_THEME = 'claude_hub_color_scheme'

function normalizeTheme(value: string | null): ColorScheme {
  return value === 'light' ? 'light' : 'dark'
}

export const useAppStore = defineStore('app', () => {
  const mode = ref<AppMode>((localStorage.getItem(STORAGE_KEY_MODE) as AppMode) || 'terminal')
  const colorScheme = ref<ColorScheme>(normalizeTheme(localStorage.getItem(STORAGE_KEY_THEME)))
  const scheduledTasksVisible = ref(false)
  const scheduledTaskCreateTargetTabId = ref<string | null>(null)
  const scheduledTaskCreateRequest = ref(0)

  function setMode(nextMode: AppMode) {
    mode.value = nextMode
    localStorage.setItem(STORAGE_KEY_MODE, nextMode)
  }

  function setColorScheme(nextScheme: ColorScheme) {
    colorScheme.value = nextScheme
    localStorage.setItem(STORAGE_KEY_THEME, nextScheme)
  }

  function toggleColorScheme() {
    setColorScheme(colorScheme.value === 'dark' ? 'light' : 'dark')
  }

  function openScheduledTasks(targetTabId: string | null = null) {
    scheduledTaskCreateTargetTabId.value = targetTabId
    if (targetTabId) scheduledTaskCreateRequest.value += 1
    scheduledTasksVisible.value = true
  }

  function closeScheduledTasks() {
    scheduledTasksVisible.value = false
    scheduledTaskCreateTargetTabId.value = null
  }

  return {
    mode,
    colorScheme,
    scheduledTasksVisible,
    scheduledTaskCreateTargetTabId,
    scheduledTaskCreateRequest,
    setMode,
    setColorScheme,
    toggleColorScheme,
    openScheduledTasks,
    closeScheduledTasks,
  }
})
