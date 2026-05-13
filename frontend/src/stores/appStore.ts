import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { AppMode } from '@/types'

const STORAGE_KEY_MODE = 'claude_hub_app_mode'

export const useAppStore = defineStore('app', () => {
  const mode = ref<AppMode>((localStorage.getItem(STORAGE_KEY_MODE) as AppMode) || 'terminal')

  function setMode(nextMode: AppMode) {
    mode.value = nextMode
    localStorage.setItem(STORAGE_KEY_MODE, nextMode)
  }

  return {
    mode,
    setMode,
  }
})
