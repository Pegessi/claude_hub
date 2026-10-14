<template>
  <div
    class="dot-launcher"
    data-testid="dot-launcher"
  >
    <button
      type="button"
      class="dot-launcher__open"
      :disabled="opening"
      title="Talk to Dot"
      @click="openDot"
    >
      <span
        class="dot-launcher__mark"
        aria-hidden="true"
      />
      Dot
    </button>
    <button
      type="button"
      class="dot-launcher__memory"
      title="Dot memory, focus and work"
      aria-label="Open Dot memory"
      @click="panelOpen = true"
    >
      ☰
    </button>
    <DotPanel
      :open="panelOpen"
      @close="panelOpen = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import DotPanel from '@/components/DotPanel.vue'
import { parseLaunchEnv, useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { setupDot } from '@/utils/dotApi'

const appStore = useAppStore()
const terminalStore = useTerminalStore()
const { defaultPresetTextForAgent } = useLaunchEnvPresets()
const opening = ref(false)
const panelOpen = ref(false)

// Provider env for a new Dot tab: borrow it from a Claude tab the user already
// works in (active one first), else the default Claude preset.
function providerEnv(): Record<string, string> | undefined {
  const claudeTabs = terminalStore.tabs.filter(tab => tab.agent_type === 'claude' && tab.env && Object.keys(tab.env).length)
  const source = claudeTabs.find(tab => tab.id === terminalStore.activeTabId) ?? claudeTabs[0]
  return source?.env ?? parseLaunchEnv(defaultPresetTextForAgent('claude'))
}

// Setup is idempotent: it creates (or restores) Dot's Chat tab on first use.
// The backend only seeds env into a tab still on the built-in fallback env.
async function openDot() {
  opening.value = true
  try {
    const status = await setupDot(providerEnv())
    appStore.setMode('terminal')
    await terminalStore.fetchTabs({ force: true })
    if (status.tab_id && terminalStore.tabs.some(tab => tab.id === status.tab_id)) {
      terminalStore.setActiveTab(status.tab_id)
    }
  } catch (error) {
    terminalStore.pushNotification({
      type: 'error',
      message: error instanceof Error ? error.message : 'Could not open Dot',
      autoDismissMs: 8000,
    })
  } finally {
    opening.value = false
  }
}
</script>

<style scoped>
.dot-launcher {
  display: inline-flex;
  align-items: stretch;
  height: 30px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  background: var(--ch-color-surface-sunken);
  overflow: hidden;
}

.dot-launcher__open,
.dot-launcher__memory {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.dot-launcher__open {
  padding: 0 10px 0 12px;
}

.dot-launcher__memory {
  padding: 0 10px;
  border-left: 1px solid var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.dot-launcher__open:hover:not(:disabled),
.dot-launcher__memory:hover {
  background: var(--ch-color-surface-control-hover);
}

.dot-launcher__open:disabled {
  opacity: 0.6;
  cursor: progress;
}

.dot-launcher__mark {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}
</style>
