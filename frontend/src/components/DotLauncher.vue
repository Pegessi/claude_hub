<template>
  <div
    :class="['dot-launcher', { 'dot-launcher--compact': compact }]"
    data-testid="dot-launcher"
  >
    <button
      type="button"
      class="dot-launcher__open"
      :disabled="opening"
      :title="`Talk to ${identity.name}`"
      :aria-label="`Talk to ${identity.name}`"
      @click="openDot"
    >
      <DotAvatar
        :identity="identity"
        :size="compact ? 24 : 20"
      />
      <span
        v-if="!compact"
        class="dot-launcher__name"
      >{{ identity.name }}</span>
    </button>
    <button
      type="button"
      class="dot-launcher__memory"
      :title="`${identity.name} memory, focus and work`"
      :aria-label="`Open ${identity.name} memory`"
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
import { onMounted, ref } from 'vue'
import DotAvatar from '@/components/DotAvatar.vue'
import DotPanel from '@/components/DotPanel.vue'
import { useDotIdentity } from '@/composables/useDotIdentity'
import { parseLaunchEnv, useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { setupDot } from '@/utils/dotApi'

// Compact: avatar only, with larger touch targets (mobile header).
defineProps<{ compact?: boolean }>()

const appStore = useAppStore()
const { identity, setIdentity, refreshIdentity } = useDotIdentity()
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

onMounted(() => { void refreshIdentity() })

// Setup is idempotent: it creates (or restores) Dot's Chat tab on first use.
// The backend only seeds env into a tab still on the built-in fallback env.
async function openDot() {
  opening.value = true
  try {
    const status = await setupDot(providerEnv())
    if (status.identity) setIdentity(status.identity)
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
  padding: 0 10px 0 6px;
}

.dot-launcher__name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.dot-launcher--compact {
  height: 36px;
  flex: 0 0 auto;
}

.dot-launcher--compact .dot-launcher__open {
  justify-content: center;
  min-width: 40px;
  padding: 0 6px 0 8px;
}

.dot-launcher--compact .dot-launcher__memory {
  min-width: 36px;
  justify-content: center;
  padding: 0 8px;
  font-size: var(--ch-font-size-sm);
}
</style>
