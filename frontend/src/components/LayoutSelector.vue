<template>
  <div
    v-if="variant === 'menu'"
    class="layout-selector layout-selector--menu"
  >
    <details
      ref="menuRef"
      class="layout-menu"
    >
      <summary
        class="layout-menu-trigger"
        title="Change split layout"
        aria-label="Change split layout"
      >
        <span
          class="layout-menu-icon"
          :style="{ display: 'grid', gridTemplateColumns: activeLayout.gridCols, gap: '2px', padding: '4px' }"
        >
          <span
            v-for="i in activeLayout.count"
            :key="i"
            class="layout-cell"
          />
        </span>
      </summary>
      <div class="layout-menu-panel">
        <button
          v-for="layout in layouts"
          :key="layout.type"
          type="button"
          :class="['layout-menu-item', { active: layoutType === layout.type }]"
          @click="setLayout(layout.type)"
        >
          <span
            class="layout-menu-item-icon"
            :style="{ display: 'grid', gridTemplateColumns: layout.gridCols, gap: '2px', padding: '4px' }"
          >
            <span
              v-for="i in layout.count"
              :key="i"
              class="layout-cell"
            />
          </span>
          <span class="layout-menu-item-label">{{ layout.label }}</span>
        </button>
      </div>
    </details>
  </div>
  <div
    v-else
    class="layout-selector layout-selector--row"
  >
    <div class="layout-buttons">
      <button
        v-for="layout in layouts"
        :key="layout.type"
        :class="['layout-btn', { active: layoutType === layout.type }]"
        :title="layout.label"
        @click="setLayout(layout.type)"
      >
        <div
          class="layout-icon"
          :style="{ display: 'grid', gridTemplateColumns: layout.gridCols, gap: '2px', padding: '4px' }"
        >
          <div
            v-for="i in layout.count"
            :key="i"
            class="layout-cell"
          />
        </div>
      </button>
    </div>

    <!-- User info and sign-out button -->
    <div
      v-if="authStore.isAuthenticated"
      class="user-section"
    >
      <div class="user-info">
        <img
          v-if="authStore.user?.avatar_url"
          :src="authStore.user.avatar_url"
          class="user-avatar"
          :alt="authStore.user?.name"
        >
        <span
          v-else
          class="user-avatar-fallback"
        >{{ authStore.user?.name?.charAt(0) || 'U' }}</span>
        <span class="user-name">{{ authStore.user?.name }}</span>
      </div>
      <LoadingButton
        class="logout-btn"
        :loading="logoutLoading"
        loading-label="Logging out"
        @click="handleLogout"
      >
        Sign out
      </LoadingButton>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingButton from '@/components/LoadingButton.vue'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAuthStore } from '@/stores/authStore'
import type { LayoutType } from '@/types'

interface LayoutOption {
  type: LayoutType
  label: string
  count: number
  gridCols: string
}

const store = useTerminalStore()
const authStore = useAuthStore()
const { layoutType } = storeToRefs(store)
const logoutLoading = ref(false)
const menuRef = ref<HTMLDetailsElement | null>(null)

const props = withDefaults(defineProps<{
  variant?: 'row' | 'menu'
}>(), {
  variant: 'row',
})

const layouts: LayoutOption[] = [
  { type: '1x1', label: 'Single', count: 1, gridCols: '1fr' },
  { type: '2x1', label: 'Two Columns', count: 2, gridCols: '1fr 1fr' },
  { type: '1x2', label: 'Two Rows', count: 2, gridCols: '1fr' },
  { type: '3x1', label: 'Three Columns', count: 3, gridCols: '1fr 1fr 1fr' },
  { type: '1x3', label: 'Three Rows', count: 3, gridCols: '1fr' },
  { type: '2x2', label: '2x2 Grid', count: 4, gridCols: '1fr 1fr' },
  { type: '3x3', label: '3x3 Grid', count: 9, gridCols: '1fr 1fr 1fr' },
]

const activeLayout = computed(() => (
  layouts.find((layout) => layout.type === layoutType.value) || layouts[0]
))

function setLayout(type: LayoutType) {
  store.setLayout(type)
  if (props.variant === 'menu' && menuRef.value) {
    menuRef.value.open = false
  }
}

async function handleLogout() {
  if (logoutLoading.value) return
  logoutLoading.value = true
  try {
    await authStore.logout()
  } finally {
    logoutLoading.value = false
  }
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (props.variant !== 'menu') return
  const target = event.target
  if (target instanceof Node && menuRef.value && !menuRef.value.contains(target)) {
    menuRef.value.open = false
  }
}

onMounted(() => {
  if (props.variant === 'menu') {
    document.addEventListener('pointerdown', handleDocumentPointerDown)
  }
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
})
</script>

<style scoped>
.layout-selector {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-2);
  padding: 7px 10px; /* off-scale: between space-1 (4px) and space-2 (8px), rounding shifts layout */
  background-color: var(--ch-color-canvas);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.layout-selector--menu {
  display: none;
}

.layout-buttons {
  display: flex;
  align-items: center;
  gap: 5px; /* off-scale: between space-1 (4px) and space-2 (8px), tight density between tiles */
}

.layout-btn {
  width: 32px;
  height: 30px;
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: var(--ch-space-1);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.layout-btn:hover {
  background-color: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
}

.layout-btn:active {
  transform: translateY(1px);
}

.layout-btn.active {
  border-color: var(--ch-color-accent-strong);
  background-color: var(--ch-color-accent-soft);
  box-shadow: 0 1px 3px var(--ch-shadow-color-soft); /* off-scale: tight 1px highlight, not replaceable by --ch-shadow-soft (popover) */
}

.layout-btn:focus-visible,
.layout-menu-trigger:focus-visible,
.layout-menu-item:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

.layout-icon {
  width: 24px;
  height: 20px;
}

.layout-cell {
  background-color: var(--ch-color-text-subtle);
  border-radius: 2px; /* functional geometry for tiny layout-preset cells; --ch-radius-sm (5px) reads too rounded here */
  min-height: 4px;
  min-width: 4px;
  transition: background-color var(--ch-motion-fast);
}

.layout-btn.active .layout-cell {
  background-color: var(--ch-color-accent-strong);
}

.layout-menu {
  position: relative;
}

.layout-menu summary {
  list-style: none;
}

.layout-menu summary::-webkit-details-marker {
  display: none;
}

.layout-menu-trigger {
  width: 30px;
  height: 30px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), transform var(--ch-motion-fast), width var(--ch-motion-standard) var(--ch-motion-ease), height var(--ch-motion-standard) var(--ch-motion-ease), border-radius var(--ch-motion-standard) var(--ch-motion-ease);
}

.layout-menu-trigger:hover,
.layout-menu[open] .layout-menu-trigger {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.layout-menu-trigger:active {
  transform: translateY(1px);
}

.layout-menu-icon,
.layout-menu-item-icon {
  width: 20px;
  height: 16px;
}

.layout-menu-panel {
  position: absolute;
  top: calc(100% + 7px); /* off-scale: tiny gap to trigger, keep as literal */
  right: 0;
  z-index: 1200;
  width: 182px;
  max-height: min(340px, calc(var(--visual-viewport-height, 100dvh) - 96px));
  overflow-y: auto;
  padding: 6px; /* off-scale: between space-1 (4px) and space-2 (8px), density-tuned */
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-soft);
  animation: layout-menu-in var(--ch-motion-fast) var(--ch-motion-ease);
  transform-origin: top right;
}

@keyframes layout-menu-in {
  from {
    opacity: 0;
    transform: translateY(-4px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.layout-menu-item {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  gap: var(--ch-space-2);
  padding: 5px 7px; /* off-scale: dense menu item, between space-1 and space-2 */
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  text-align: left;
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.layout-menu-item:hover {
  background: var(--ch-color-surface-control-hover);
}

.layout-menu-item:active {
  transform: translateY(1px);
}

.layout-menu-item.active {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent-strong);
  font-weight: var(--ch-weight-semibold);
}

.layout-menu-item.active .layout-cell {
  background-color: var(--ch-color-accent-strong);
}

.layout-menu-item-label {
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-semibold);
}

.user-section {
  display: flex;
  align-items: center;
  gap: var(--ch-space-3);
}

.user-info {
  display: flex;
  align-items: center;
  gap: var(--ch-space-2);
}

.user-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  object-fit: cover;
}

.user-avatar-fallback {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background-color: var(--ch-color-accent);
  color: var(--ch-color-text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: var(--ch-weight-semibold);
  font-size: var(--ch-font-sm);
}

.user-name {
  color: var(--ch-color-text);
  font-size: var(--ch-font-md);
}

.logout-btn {
  background: none;
  border: 1px solid var(--ch-color-text-subtle);
  color: var(--ch-color-text);
  padding: var(--ch-space-1) 11px; /* horizontal 11px off-scale, keep literal */
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  display: inline-flex;
  align-items: center;
  gap: var(--ch-space-1);
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.logout-btn:hover:not(:disabled) {
  background-color: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger);
  color: var(--ch-color-danger-strong);
}

.logout-btn:active:not(:disabled) {
  transform: translateY(1px);
}

.logout-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .layout-selector--row {
    display: none;
  }

  .layout-selector--menu {
    display: flex;
    flex: 0 0 auto;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .layout-menu-trigger {
    width: 30px;
    height: 30px;
  }
}

@media (min-width: 769px) {
  .layout-selector--menu {
    display: none;
  }
}
</style>
