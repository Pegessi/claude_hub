<template>
  <div
    class="terminal-grid"
    :class="`layout-${layoutType}`"
  >
    <TerminalPane
      v-for="pane in panes"
      :key="pane.id"
      :pane="pane"
      @click="setActivePane(pane.id)"
    />
  </div>
</template>

<script setup lang="ts">
import { watchEffect } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import TerminalPane from '@/components/TerminalPane.vue'

const store = useTerminalStore()
const { layoutType, panes, activePaneId } = storeToRefs(store)

// Expose the active pane's tab id on window so downstream components can
// cheaply check whether they're the active terminal without creating
// reactive dependencies on the whole panes array.
watchEffect(() => {
  const activePane = panes.value.find(p => p.id === activePaneId.value)
  if (typeof window !== 'undefined') {
    window.__activePaneTabId = activePane?.tabId ?? null
  }
})

function setActivePane(paneId: string) {
  store.setActivePane(paneId)
}
</script>

<style scoped>
.terminal-grid {
  flex: 1;
  display: grid;
  gap: 4px;
  padding: 4px;
  overflow: hidden;
  min-height: 0;
  transition: padding 180ms cubic-bezier(0.2, 0, 0, 1), gap 180ms cubic-bezier(0.2, 0, 0, 1);
}

.layout-1x1 {
  grid-template-columns: 1fr;
  grid-template-rows: 1fr;
}

.layout-2x1 {
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr;
}

.layout-1x2 {
  grid-template-columns: 1fr;
  grid-template-rows: 1fr 1fr;
}

.layout-3x1 {
  grid-template-columns: 1fr 1fr 1fr;
  grid-template-rows: 1fr;
}

.layout-1x3 {
  grid-template-columns: 1fr;
  grid-template-rows: 1fr 1fr 1fr;
}

.layout-2x2 {
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
}

.layout-3x3 {
  grid-template-columns: 1fr 1fr 1fr;
  grid-template-rows: 1fr 1fr 1fr;
}
</style>
