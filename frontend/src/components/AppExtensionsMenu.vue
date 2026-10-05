<template>
  <details
    ref="menu"
    class="extensions-menu"
    @keydown.esc.stop.prevent="close(true)"
  >
    <summary
      title="Extensions"
      aria-label="Extensions"
      class="extensions-trigger"
    >
      ⋯
    </summary>
    <div class="extensions-panel">
      <AppExtensionActions @select="close(true)" />
    </div>
  </details>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import AppExtensionActions from '@/components/AppExtensionActions.vue'

const menu = ref<HTMLDetailsElement | null>(null)
function close(restoreFocus = false) {
  if (!menu.value) return
  menu.value.open = false
  if (restoreFocus) menu.value.querySelector('summary')?.focus()
}
function outside(event: PointerEvent) {
  if (!menu.value?.contains(event.target as Node)) close()
}
onMounted(() => document.addEventListener('pointerdown', outside))
onUnmounted(() => document.removeEventListener('pointerdown', outside))
</script>

<style scoped>
.extensions-menu { position: relative; }
.extensions-trigger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  box-sizing: border-box;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 22px;
  line-height: 1;
  list-style: none;
  cursor: pointer;
}
.extensions-trigger::-webkit-details-marker { display: none; }
.extensions-trigger:hover, .extensions-menu[open] > summary { background: var(--ch-color-surface-control-hover); }
.extensions-trigger:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
.extensions-panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 60;
  width: min(280px, calc(100vw - 24px));
  max-height: calc(100dvh - 80px);
  overflow-y: auto;
  padding: 6px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  box-shadow: var(--ch-shadow-popover);
}
</style>
