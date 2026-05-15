<template>
  <div class="layout-selector">
    <div class="layout-buttons">
      <button
        v-for="layout in layouts"
        :key="layout.type"
        :class="['layout-btn', { active: layoutType === layout.type }]"
        @click="setLayout(layout.type)"
        :title="layout.label"
      >
        <div class="layout-icon" :style="{ display: 'grid', gridTemplateColumns: layout.gridCols, gap: '2px', padding: '4px' }">
          <div
            v-for="i in layout.count"
            :key="i"
            class="layout-cell"
          ></div>
        </div>
      </button>
    </div>

    <!-- 用户信息和退出按钮 -->
    <div v-if="authStore.isAuthenticated" class="user-section">
      <div class="user-info">
        <img v-if="authStore.user?.avatar_url" :src="authStore.user.avatar_url" class="user-avatar" :alt="authStore.user?.name" />
        <span class="user-avatar-fallback" v-else>{{ authStore.user?.name?.charAt(0) || 'U' }}</span>
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
import { ref } from 'vue'
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

const layouts: LayoutOption[] = [
  { type: '1x1', label: 'Single', count: 1, gridCols: '1fr' },
  { type: '2x1', label: 'Two Columns', count: 2, gridCols: '1fr 1fr' },
  { type: '1x2', label: 'Two Rows', count: 2, gridCols: '1fr' },
  { type: '3x1', label: 'Three Columns', count: 3, gridCols: '1fr 1fr 1fr' },
  { type: '1x3', label: 'Three Rows', count: 3, gridCols: '1fr' },
  { type: '2x2', label: '2x2 Grid', count: 4, gridCols: '1fr 1fr' },
  { type: '3x3', label: '3x3 Grid', count: 9, gridCols: '1fr 1fr 1fr' },
]

function setLayout(type: LayoutType) {
  store.setLayout(type)
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
</style>
