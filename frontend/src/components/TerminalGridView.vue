<template>
  <div class="terminal-grid" :class="`layout-${layoutType}`">
    <TerminalPane
      v-for="pane in panes"
      :key="pane.id"
      :pane="pane"
      @click="setActivePane(pane.id)"
    />
  </div>
</template>

<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import TerminalPane from '@/components/TerminalPane.vue'

const store = useTerminalStore()
const { layoutType, panes } = storeToRefs(store)

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
