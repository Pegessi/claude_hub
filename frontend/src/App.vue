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
import { onMounted, onUnmounted, watch } from 'vue'
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

// Expose active pane tab ID for mobile controls.
watch(() => activePane.value?.tabId || null, (tabId) => {
  if (typeof window !== 'undefined') {
    (window as Window & { __activePaneTabId?: string | null }).__activePaneTabId = tabId
  }
}, { immediate: true })

// ---- Mobile viewport sync with visualViewport API ----
// When the virtual keyboard appears/disappears on mobile, the visual
// viewport shrinks/expands. We track this and set a CSS variable so
// the terminal area and mobile controls adjust properly.
let vvResizeHandler: (() => void) | null = null
let vvScrollHandler: (() => void) | null = null

function setupMobileViewportSync() {
  const vv = window.visualViewport
  if (!vv) {
    // Fallback for browsers without visualViewport: listen to window resize
    window.addEventListener('resize', handleFallbackResize)
    return
  }

  vvResizeHandler = () => {
    updateKeyboardHeight(vv)
  }
  vvScrollHandler = () => {
    // On iOS, visualViewport scrolls when keyboard is open.
    // The offsetTop tells us how far the page scrolled.
    updateKeyboardHeight(vv)
  }

  vv.addEventListener('resize', vvResizeHandler)
  vv.addEventListener('scroll', vvScrollHandler)
}

function handleFallbackResize() {
  // Rough fallback: if innerHeight dropped significantly, keyboard is likely open
  const app = document.documentElement
  app.style.setProperty('--keyboard-height', '0px')
}

function updateKeyboardHeight(vv: VisualViewport) {
  const fullHeight = window.innerHeight
  const viewportHeight = vv.height
  const keyboardHeight = Math.max(0, fullHeight - viewportHeight - vv.offsetTop)

  const app = document.documentElement
  if (keyboardHeight > 50) {
    // Keyboard is open (threshold to avoid false positives from URL bar)
    app.style.setProperty('--keyboard-height', `${keyboardHeight}px`)
  } else {
    app.style.setProperty('--keyboard-height', '0px')
  }
}

function cleanupMobileViewportSync() {
  const vv = window.visualViewport
  if (vv) {
    if (vvResizeHandler) vv.removeEventListener('resize', vvResizeHandler)
    if (vvScrollHandler) vv.removeEventListener('scroll', vvScrollHandler)
  }
  window.removeEventListener('resize', handleFallbackResize)
}

onMounted(async () => {
  // Always check auth first - it will handle the case when auth is not enabled
  await authStore.checkAuth()
  if (!authStore.authEnabled || !authStore.authRequired || authStore.isAuthenticated) {
    await store.fetchTabs()
  }
  // Set up mobile viewport sync
  setupMobileViewportSync()
})

onUnmounted(() => {
  cleanupMobileViewportSync()
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
  /* Adjust for mobile keyboard — set by visualViewport sync */
  padding-bottom: var(--keyboard-height, 0px);
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
