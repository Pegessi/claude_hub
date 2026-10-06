<template>
  <button
    type="button"
    class="extension-action"
    @click="appStore.openScheduledTasks(); emit('select')"
  >
    <svg
      class="extension-action-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle
        cx="12"
        cy="12"
        r="9"
      />
      <path d="M12 7v5l3 2" />
    </svg>
    <span>Scheduled tasks</span>
  </button>
  <button
    type="button"
    class="extension-action"
    @click="appStore.openFeishuBotSettings(); emit('select')"
  >
    <svg
      class="extension-action-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect
        x="4"
        y="7"
        width="16"
        height="13"
        rx="3"
      />
      <path d="M12 3v4M8 12h.01M16 12h.01M8 16h8" />
    </svg>
    <span>Feishu Bot settings</span>
  </button>
  <NetworkAccessMenu variant="menu" />
  <button
    type="button"
    class="extension-action"
    @click="appStore.restartDialogVisible = true; emit('select')"
  >
    <svg
      class="extension-action-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M20 4v6h-6" />
      <path d="M20 10a8 8 0 1 0-2 7.3" />
    </svg>
    <span>Restart service</span>
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
  gap: 10px;
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

.extension-action-icon {
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  color: var(--ch-color-text-muted);
}

.extension-action:hover { background: var(--ch-color-surface-control-hover); }
.extension-action:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: -2px; }
@media (max-width: 640px) { .extension-action { min-height: 44px; } }
</style>
