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

    <!-- 用户信息和退出按钮 -->
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
        退出
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
  gap: 8px;
  padding: 7px 10px;
  background-color: var(--ch-color-canvas);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.layout-selector--menu {
  display: none;
}

.layout-buttons {
  display: flex;
  align-items: center;
  gap: 5px;
}

.layout-btn {
  width: 32px;
  height: 30px;
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 4px;
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

.layout-btn.active {
  border-color: var(--ch-color-accent-strong);
  background-color: var(--ch-color-accent-soft);
  box-shadow: 0 1px 3px var(--ch-shadow-color-soft);
}

.layout-icon {
  width: 24px;
  height: 20px;
}

.layout-cell {
  background-color: var(--ch-color-text-subtle);
  border-radius: 2px;
  min-height: 4px;
  min-width: 4px;
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
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.layout-menu-trigger:hover,
.layout-menu[open] .layout-menu-trigger {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.layout-menu-icon,
.layout-menu-item-icon {
  width: 20px;
  height: 16px;
}

.layout-menu-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 1200;
  width: 182px;
  max-height: min(340px, calc(var(--visual-viewport-height, 100dvh) - 96px));
  overflow-y: auto;
  padding: 6px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-soft);
}

.layout-menu-item {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 7px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  text-align: left;
  cursor: pointer;
}

.layout-menu-item:hover {
  background: var(--ch-color-surface-control-hover);
}

.layout-menu-item.active {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
}

.layout-menu-item.active .layout-cell {
  background-color: var(--ch-color-accent-strong);
}

.layout-menu-item-label {
  font-size: 12px;
  font-weight: 600;
}

.user-section {
  display: flex;
  align-items: center;
  gap: 12px;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
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
  font-weight: bold;
  font-size: 12px;
}

.user-name {
  color: var(--ch-color-text);
  font-size: 13px;
}

.logout-btn {
  background: none;
  border: 1px solid var(--ch-color-text-subtle);
  color: var(--ch-color-text);
  padding: 3px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
  transition: all 0.2s;
}

.logout-btn:hover {
  background-color: var(--ch-color-border-strong);
  border-color: var(--ch-color-text-soft);
  color: var(--ch-color-text);
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
