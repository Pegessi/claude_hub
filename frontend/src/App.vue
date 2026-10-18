<template>
  <div
    class="app"
    :data-mode="mode"
  >
    <!-- 登录页面 -->
    <LoginView v-if="authStore.authRequired && !authStore.isAuthenticated && !authStore.isLoading" />

    <!-- 加载状态 -->
    <div
      v-else-if="authStore.isLoading"
      class="loading-state"
    >
      <div class="loading-spinner" />
      <p>Loading...</p>
    </div>

    <!-- 主应用 -->
    <template v-else>
      <div
        v-if="authStore.checkAuthError"
        class="auth-error-banner"
        role="alert"
      >
        <span>认证检查失败，无法连接后端。请检查后端服务是否启动或刷新页面重试。</span>
        <div class="auth-error-banner__actions">
          <button
            type="button"
            class="auth-error-banner__retry"
            @click="retryCheckAuth"
          >
            刷新重试
          </button>
          <button
            type="button"
            class="auth-error-banner__close"
            aria-label="关闭提示"
            @click="authStore.clearCheckAuthError()"
          >
            ×
          </button>
        </div>
      </div>
      <div class="app-mode-bar">
        <div
          class="mode-switch"
          role="tablist"
          aria-label="Application mode"
        >
          <button
            type="button"
            :class="['mode-button', { active: mode === 'terminal' }]"
            role="tab"
            :aria-selected="mode === 'terminal'"
            @click="appStore.setMode('terminal')"
          >
            Terminal
          </button>
          <button
            type="button"
            :class="['mode-button', { active: mode === 'workspace' }]"
            role="tab"
            :aria-selected="mode === 'workspace'"
            @click="appStore.setMode('workspace')"
          >
            Agent Workspace
          </button>
        </div>
        <TabBar v-if="!isMobile && mode === 'terminal'" />
        <div class="app-mode-tools">
          <DotLauncher v-if="!isMobile" />
          <AppExtensionsMenu v-if="!isMobile" />
          <button
            type="button"
            class="theme-switch"
            role="switch"
            :aria-checked="colorScheme === 'light'"
            :title="colorScheme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'"
            @click="appStore.toggleColorScheme()"
          >
            <span class="theme-switch-label">Dark</span>
            <span class="theme-switch-label">Light</span>
            <span class="theme-switch-thumb" />
          </button>
        </div>
      </div>
      <div
        v-if="error"
        class="error-banner"
      >
        <span>{{ error }}</span>
        <button
          class="error-close"
          @click="clearError"
        >
          ×
        </button>
      </div>
      <div
        v-show="mode === 'terminal'"
        class="terminal-mode-shell"
      >
        <ChatSidebar
          class="terminal-sidebar"
          :swipe-surface="terminalMainColumn"
          @open-archive="archivePanelOpen = true"
        />
        <div
          ref="terminalMainColumn"
          class="terminal-main-column"
        >
          <TabBar v-if="isMobile" />
          <LayoutSelector />
          <div
            v-if="tabs.length === 0"
            class="empty-state"
          >
            <h2>No Terminal Tabs</h2>
            <p>Click the + button to create a new terminal tab</p>
          </div>
          <TerminalGridView v-else />
          <!-- The floating keyboard ball only injects raw PTY keys, so it has no
               purpose (and just clutters the chat surface) when the active pane
               is a native structured Chat session. -->
          <MobileControls v-if="!activePaneIsChat" />
        </div>
      </div>
      <AgentWorkspaceView v-if="mode === 'workspace'" />
      <ArchivedSessionsPanel
        :open="archivePanelOpen"
        @close="archivePanelOpen = false"
      />
      <ScheduledTasksPanel
        :visible="scheduledTasksVisible"
        :create-target-tab-id="scheduledTaskCreateTargetTabId"
        :create-request="scheduledTaskCreateRequest"
        @close="appStore.closeScheduledTasks()"
      />
      <FeishuBotSettingsDialog
        v-if="feishuBotSettingsVisible"
        @close="appStore.closeFeishuBotSettings()"
      />
      <ServiceRestartDialog
        v-if="appStore.restartDialogVisible"
        @close="appStore.restartDialogVisible = false"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import TabBar from '@/components/TabBar.vue'
import LayoutSelector from '@/components/LayoutSelector.vue'
import TerminalGridView from '@/components/TerminalGridView.vue'
import MobileControls from '@/components/MobileControls.vue'
import AgentWorkspaceView from '@/components/AgentWorkspaceView.vue'
import ChatSidebar from '@/components/ChatSidebar.vue'
import ArchivedSessionsPanel from '@/components/ArchivedSessionsPanel.vue'
import AppExtensionsMenu from '@/components/AppExtensionsMenu.vue'
import DotLauncher from '@/components/DotLauncher.vue'
import ServiceRestartDialog from '@/components/ServiceRestartDialog.vue'
import FeishuBotSettingsDialog from '@/components/FeishuBotSettingsDialog.vue'
import ScheduledTasksPanel from '@/components/ScheduledTasksPanel.vue'
import LoginView from '@/views/LoginView.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAuthStore } from '@/stores/authStore'
import { parseTabDeepLink } from '@/utils/deepLink'
import { useViewport } from '@/composables/useViewport'

const appStore = useAppStore()
const store = useTerminalStore()
const authStore = useAuthStore()
const { tabs, error, activePane, activePaneIsChat } = storeToRefs(store)
const {
  mode,
  colorScheme,
  feishuBotSettingsVisible,
  scheduledTasksVisible,
  scheduledTaskCreateTargetTabId,
  scheduledTaskCreateRequest,
} = storeToRefs(appStore)
const archivePanelOpen = ref(false)
const terminalMainColumn = ref<HTMLElement | null>(null)
const { isMobile } = useViewport()

// Clear all error-type notifications from the terminal store toast stack.
function clearError() {
  const ids = store.notifications
    .filter(n => n.type === 'error')
    .map(n => n.id)
  for (const id of ids) store.dismissNotification(id)
}

async function retryCheckAuth() {
  await authStore.checkAuth()
  if (!authStore.authEnabled || !authStore.authRequired || authStore.isAuthenticated) {
    await store.fetchTabs()
  }
}

// Expose active pane tab ID for mobile controls.
watch(() => activePane.value?.tabId || null, (tabId) => {
  if (typeof window !== 'undefined') {
    window.__claudeHub.activePaneTabId = tabId
  }
}, { immediate: true })

watch(colorScheme, (scheme) => {
  document.documentElement.dataset.theme = scheme
}, { immediate: true })

// ---- Mobile viewport sync with visualViewport API ----
// When the virtual keyboard appears/disappears on mobile, the visual
// viewport shrinks/expands. We track this and set a CSS variable so
// the terminal area and mobile controls adjust properly.
let vvResizeHandler: (() => void) | null = null
let vvScrollHandler: (() => void) | null = null
let largestVisualViewportHeight = 0
let lastVisualViewportWidth = 0
let keyboardOpenState = false
let keyboardCloseTimer: number | null = null
let viewportUpdateFrame: number | null = null
let pendingVisualViewport: VisualViewport | undefined
let lastAppliedViewportHeight = ''
let lastAppliedStableViewportHeight = ''
let lastAppliedKeyboardHeight = ''
let lastAppliedViewportOffsetTop = ''
let lastAppliedControlsViewportShift = ''
let fixedViewportProbe: HTMLDivElement | null = null

const KEYBOARD_OPEN_THRESHOLD_PX = 36
const KEYBOARD_CLOSE_THRESHOLD_PX = 12
const KEYBOARD_CLOSE_DELAY_MS = 160

function setupMobileViewportSync() {
  const vv = window.visualViewport
  if (!vv) {
    // Fallback for browsers without visualViewport: listen to window resize
    window.addEventListener('resize', handleFallbackResize)
    handleFallbackResize()
    return
  }

  vvResizeHandler = () => {
    scheduleViewportMetricsUpdate(vv)
  }
  vvScrollHandler = () => {
    // On iOS, visualViewport scrolls when keyboard is open.
    // The offsetTop tells us how far the page scrolled.
    scheduleViewportMetricsUpdate(vv)
  }

  vv.addEventListener('resize', vvResizeHandler)
  vv.addEventListener('scroll', vvScrollHandler)
  updateViewportMetrics(vv)
}

function handleFallbackResize() {
  scheduleViewportMetricsUpdate()
}

function scheduleViewportMetricsUpdate(vv?: VisualViewport) {
  pendingVisualViewport = vv

  if (viewportUpdateFrame !== null) return

  viewportUpdateFrame = window.requestAnimationFrame(() => {
    viewportUpdateFrame = null
    updateViewportMetrics(pendingVisualViewport)
    pendingVisualViewport = undefined
  })
}

function clearKeyboardCloseTimer() {
  if (keyboardCloseTimer !== null) {
    window.clearTimeout(keyboardCloseTimer)
    keyboardCloseTimer = null
  }
}

function getFixedViewportProbe() {
  if (fixedViewportProbe?.isConnected) {
    return fixedViewportProbe
  }

  if (!document.body) return null

  fixedViewportProbe = document.createElement('div')
  fixedViewportProbe.setAttribute('aria-hidden', 'true')
  fixedViewportProbe.style.cssText = [
    'position: fixed',
    'left: 0',
    'bottom: 0',
    'width: 0',
    'height: 0',
    'padding: 0',
    'border: 0',
    'pointer-events: none',
    'visibility: hidden',
    'z-index: -1',
  ].join(';')
  document.body.appendChild(fixedViewportProbe)
  return fixedViewportProbe
}

function measureControlsViewportShift(viewportHeight: number, isMobileViewport: boolean) {
  if (!isMobileViewport) return 0

  const probe = getFixedViewportProbe()
  if (!probe) return 0

  return Math.round(viewportHeight - probe.getBoundingClientRect().bottom)
}

function applyKeyboardOpenState(open: boolean) {
  const root = document.documentElement
  keyboardOpenState = open

  if (open) {
    root.dataset.keyboardOpen = 'true'
  } else {
    delete root.dataset.keyboardOpen
  }
}

function updateViewportMetrics(vv?: VisualViewport) {
  const root = document.documentElement
  const viewportHeight = vv?.height ?? window.innerHeight
  const viewportWidth = vv?.width ?? window.innerWidth
  const offsetTop = vv?.offsetTop ?? 0
  const isMobileViewport = window.innerWidth <= 768

  if (Math.abs(viewportWidth - lastVisualViewportWidth) > 24) {
    largestVisualViewportHeight = viewportHeight
    lastVisualViewportWidth = viewportWidth
  } else if (!largestVisualViewportHeight || viewportHeight > largestVisualViewportHeight) {
    largestVisualViewportHeight = viewportHeight
  }

  const keyboardHeight = isMobileViewport
    ? Math.max(0, largestVisualViewportHeight - viewportHeight - offsetTop)
    : 0

  const viewportHeightValue = `${Math.round(viewportHeight)}px`
  const stableViewportHeightValue = `${Math.round(isMobileViewport ? largestVisualViewportHeight || viewportHeight : viewportHeight)}px`
  const keyboardHeightValue = `${Math.round(keyboardHeight)}px`
  const viewportOffsetTopValue = `${Math.round(offsetTop)}px`
  const controlsViewportShiftValue = `${measureControlsViewportShift(viewportHeight, isMobileViewport)}px`

  if (viewportHeightValue !== lastAppliedViewportHeight) {
    root.style.setProperty('--visual-viewport-height', viewportHeightValue)
    lastAppliedViewportHeight = viewportHeightValue
  }

  if (stableViewportHeightValue !== lastAppliedStableViewportHeight) {
    root.style.setProperty('--stable-viewport-height', stableViewportHeightValue)
    lastAppliedStableViewportHeight = stableViewportHeightValue
  }

  if (keyboardHeightValue !== lastAppliedKeyboardHeight) {
    root.style.setProperty('--keyboard-height', keyboardHeightValue)
    lastAppliedKeyboardHeight = keyboardHeightValue
  }

  if (viewportOffsetTopValue !== lastAppliedViewportOffsetTop) {
    root.style.setProperty('--visual-viewport-offset-top', viewportOffsetTopValue)
    lastAppliedViewportOffsetTop = viewportOffsetTopValue
  }

  if (controlsViewportShiftValue !== lastAppliedControlsViewportShift) {
    root.style.setProperty('--mobile-controls-viewport-shift', controlsViewportShiftValue)
    lastAppliedControlsViewportShift = controlsViewportShiftValue
  }

  if (!isMobileViewport) {
    clearKeyboardCloseTimer()
    if (keyboardOpenState) {
      applyKeyboardOpenState(false)
    }
    return
  }

  if (keyboardHeight >= KEYBOARD_OPEN_THRESHOLD_PX) {
    clearKeyboardCloseTimer()
    if (!keyboardOpenState) {
      applyKeyboardOpenState(true)
    }
    return
  }

  if (keyboardOpenState && keyboardHeight > KEYBOARD_CLOSE_THRESHOLD_PX) {
    clearKeyboardCloseTimer()
    return
  }

  if (keyboardOpenState && keyboardHeight <= KEYBOARD_CLOSE_THRESHOLD_PX && keyboardCloseTimer === null) {
    keyboardCloseTimer = window.setTimeout(() => {
      keyboardCloseTimer = null
      applyKeyboardOpenState(false)
    }, KEYBOARD_CLOSE_DELAY_MS)
  }
}

function cleanupMobileViewportSync() {
  const vv = window.visualViewport
  if (vv) {
    if (vvResizeHandler) vv.removeEventListener('resize', vvResizeHandler)
    if (vvScrollHandler) vv.removeEventListener('scroll', vvScrollHandler)
  }
  window.removeEventListener('resize', handleFallbackResize)
  clearKeyboardCloseTimer()
  if (viewportUpdateFrame !== null) {
    window.cancelAnimationFrame(viewportUpdateFrame)
    viewportUpdateFrame = null
  }
  applyKeyboardOpenState(false)
  fixedViewportProbe?.remove()
  fixedViewportProbe = null
}

// Route to a session from a ?tab=<id> deep link. Three cases: the tab is
// active (just load it), the tab is archived (restore then load), or the id is
// unknown (toast + clean the URL). Runs on mount and on browser back/forward.
async function handleDeepLink() {
  const tabId = parseTabDeepLink(window.location.search)
  if (!tabId) return

  if (store.tabs.some(tab => tab.id === tabId)) {
    store.setActiveTab(tabId)
    return
  }

  // Not active — check the archive before giving up.
  await store.fetchArchivedTabs()
  if (store.archivedTabs.some(tab => tab.id === tabId)) {
    const restored = await store.unarchiveTab(tabId)
    // unarchiveTab surfaces its own error toast on failure; only confirm
    // success here so a failed restore doesn't show a contradictory
    // "Restored archived session" notification.
    if (restored) {
      store.pushNotification({
        type: 'success',
        message: 'Restored archived session',
        autoDismissMs: 4000,
      })
    }
    return
  }

  store.pushNotification({
    type: 'warning',
    message: 'Session not found',
    autoDismissMs: 4000,
  })
  // Clean the stale ?tab param so a refresh doesn't re-trigger the warning.
  window.history.replaceState(null, '', window.location.pathname)
}

onMounted(async () => {
  // Always check auth first - it will handle the case when auth is not enabled
  await authStore.checkAuth()
  if (!authStore.authEnabled || !authStore.authRequired || authStore.isAuthenticated) {
    await store.fetchTabs()
    void store.fetchArchivedTabs()
    await handleDeepLink()
  }
  // Set up mobile viewport sync
  setupMobileViewportSync()
  // Browser back/forward re-runs the deep-link routing (app-internal tab
  // switches deliberately do not pushState, so this only fires on nav).
  window.addEventListener('popstate', handleDeepLink)
})

onUnmounted(() => {
  cleanupMobileViewportSync()
  window.removeEventListener('popstate', handleDeepLink)
})
</script>

<style src="@/features/app-shell/styles/theme.css"></style>
<style src="@/features/app-shell/styles/controls.css"></style>
<style src="@/features/app-shell/styles/shell.css"></style>
