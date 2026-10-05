<template>
  <button
    type="button"
    class="extension-action"
    @click="appStore.openScheduledTasks(); emit('select')"
  >
    Scheduled tasks
  </button>
  <NetworkAccessMenu variant="menu" />
  <button
    type="button"
    class="extension-action"
    @click="appStore.restartDialogVisible = true; emit('select')"
  >
    Restart service
  </button>
</template>

<script setup lang="ts">
import NetworkAccessMenu from '@/components/NetworkAccessMenu.vue'
import { useAppStore } from '@/stores/appStore'

const appStore = useAppStore()
const emit = defineEmits<{ select: [] }>()
</script>

<style scoped>
.extension-action {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 40px;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font: inherit;
  font-size: var(--ch-font-size-sm);
  text-align: left;
  cursor: pointer;
}

.extension-action:hover { background: var(--ch-color-surface-control-hover); }
.extension-action:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: -2px; }
@media (max-width: 640px) { .extension-action { min-height: 44px; } }
</style>
