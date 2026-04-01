<template>
  <div class="app">
    <!-- 登录页面 -->
    <LoginView v-if="authStore.authRequired && !authStore.isAuthenticated && !authStore.isLoading" />

    <!-- 加载状态 -->
    <div v-else-if="authStore.isLoading" class="loading-state">
      <div class="loading-spinner"></div>
      <p>Loading...</p>
    </div>

    <!-- 主应用 -->
    <template v-else>
      <TabBar />
      <LayoutSelector />
      <div v-if="error" class="error-banner">
        <span>{{ error }}</span>
        <button class="error-close" @click="clearError">×</button>
      </div>
      <div v-if="tabs.length === 0" class="empty-state">
        <h2>No Terminal Tabs</h2>
        <p>Click the + button to create a new terminal tab</p>
      </div>
      <TerminalGridView v-else />
      <MobileControls />
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { storeToRefs } from 'pinia'
import TabBar from '@/components/TabBar.vue'
import LayoutSelector from '@/components/LayoutSelector.vue'
import TerminalGridView from '@/components/TerminalGridView.vue'
import MobileControls from '@/components/MobileControls.vue'
import LoginView from '@/views/LoginView.vue'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAuthStore } from '@/stores/authStore'

const store = useTerminalStore()
const authStore = useAuthStore()
const { tabs, error, activePane } = storeToRefs(store)

function clearError() {
  error.value = null
}

// Expose active pane tab ID for mobile controls
watch(activePane, (pane) => {
  if (typeof window !== 'undefined') {
    ;(window as any).__activePaneTabId = pane?.tabId || null
  }
}, { immediate: true })

onMounted(async () => {
  // Always check auth first - it will handle the case when auth is not enabled
  await authStore.checkAuth()
  if (!authStore.authEnabled || !authStore.authRequired || authStore.isAuthenticated) {
    await store.fetchTabs()
  }
})
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #app {
  height: 100%;
  overflow: hidden;
}

.app {
  height: 100%;
  display: flex;
  flex-direction: column;
  background-color: #1a1a1a;
}

.error-banner {
  background-color: #dc2626;
  color: white;
  padding: 8px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.error-close {
  background: none;
  border: none;
  color: white;
  font-size: 20px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #888;
}

.empty-state h2 {
  margin-bottom: 8px;
  color: #ccc;
}

.loading-state {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background-color: #1a1a1a;
  color: #888;
}

.loading-spinner {
  width: 40px;
  height: 40px;
  border: 4px solid #333;
  border-top-color: #667eea;
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-bottom: 16px;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
