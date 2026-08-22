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
        <div class="app-mode-tools">
          <NetworkAccessMenu />
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
        class="terminal-mode-shell"
        :class="{ 'terminal-mode-shell--hidden': mode !== 'terminal' }"
      >
        <TabBar />
        <LayoutSelector />
        <div
          v-if="tabs.length === 0"
          class="empty-state"
        >
          <h2>No Terminal Tabs</h2>
          <p>Click the + button to create a new terminal tab</p>
        </div>
        <TerminalGridView v-else />
        <MobileControls />
      </div>
      <AgentWorkspaceView v-if="mode === 'workspace'" />
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
import AgentWorkspaceView from '@/components/AgentWorkspaceView.vue'
import NetworkAccessMenu from '@/components/NetworkAccessMenu.vue'
import LoginView from '@/views/LoginView.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAuthStore } from '@/stores/authStore'

const appStore = useAppStore()
const store = useTerminalStore()
const authStore = useAuthStore()
const { tabs, error, activePane } = storeToRefs(store)
const { mode, colorScheme } = storeToRefs(appStore)

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

// When switching back to terminal mode, nudge every *visible* terminal iframe
// to resize and scroll to bottom. The shell stays rendered (visibility:hidden)
// while the workspace is active, so this is a cheap correctness pass rather
// than a full history replay. We target only the tab IDs currently assigned to
// a visible pane (store.panes) — cached/hidden iframes for tabs not in any
// pane are skipped because their containers are not laid out and a fit there
// would be a no-op or race against a stale size.
watch(mode, (newMode, oldMode) => {
  if (newMode !== 'terminal' || oldMode === 'terminal') return
  // Defer to the next frame so the shell has left the absolute-positioned
  // hidden state and its layout box is final before we ask iframes to fit.
  requestAnimationFrame(() => {
    const state = window.__claudeHub?.terminalState
    const iframes = state?.iframes
    if (!iframes) return
    // Collect tab IDs assigned to currently visible panes (non-null only).
    const visibleTabIds = new Set(
      store.panes
        .map((p) => p.tabId)
        .filter((id): id is string => typeof id === 'string' && id !== null),
    )
    for (const tabId of visibleTabIds) {
      const iframe = iframes[tabId]
      if (iframe?.contentWindow) {
        iframe.contentWindow.postMessage({ type: 'terminal-resize', tabId }, '*')
        iframe.contentWindow.postMessage({ type: 'terminal-scroll-bottom', tabId }, '*')
      }
    }
  })
})

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

:root {
  color-scheme: dark;
  --visual-viewport-height: 100dvh;
  --stable-viewport-height: 100dvh;
  --visual-viewport-offset-top: 0px;
  --mobile-controls-viewport-shift: 0px;
  --keyboard-height: 0px;
  --ch-color-app-bg: #1a1a1a;
  --ch-color-canvas: #181818;
  --ch-color-surface: #1e1e1e;
  --ch-color-surface-raised: #202020;
  --ch-color-surface-soft: #252525;
  --ch-color-surface-muted: #262626;
  --ch-color-surface-control: #2b2b2b;
  --ch-color-surface-control-hover: #3c3c3c;
  --ch-color-surface-control-active: #303030;
  --ch-color-surface-pressed: #3a3a3a;
  --ch-color-surface-selected: #2b3440;
  --ch-color-surface-sunken: #171717;
  --ch-color-surface-glass: rgba(24, 24, 27, 0.96);
  --ch-color-overlay: rgba(0, 0, 0, 0.58);
  --ch-color-overlay-soft: rgba(0, 0, 0, 0.5);
  --ch-color-border: #333;
  --ch-color-border-muted: #303030;
  --ch-color-border-strong: #3f3f46;
  --ch-color-border-hover: #555;
  --ch-color-text: #f4f4f5;
  --ch-color-text-strong: #fafafa;
  --ch-color-text-muted: #a1a1aa;
  --ch-color-text-subtle: #71717a;
  --ch-color-text-soft: #888;
  --ch-color-text-inverse: #ffffff;
  --ch-color-text-code: #e5e7eb;
  --ch-color-accent: #60a5fa;
  --ch-color-accent-strong: #3b82f6;
  --ch-color-accent-hover: #2563eb;
  --ch-color-accent-soft: rgba(59, 130, 246, 0.2);
  --ch-color-accent-ring: rgba(96, 165, 250, 0.2);
  --ch-color-accent-ring-strong: rgba(96, 165, 250, 0.7);
  --ch-color-success: #4ade80;
  --ch-color-success-strong: #22c55e;
  --ch-color-success-hover: #16a34a;
  --ch-color-success-bg: rgba(74, 222, 128, 0.13);
  --ch-color-warning: #facc15;
  --ch-color-warning-strong: #f59e0b;
  --ch-color-warning-bg: rgba(250, 204, 21, 0.14);
  --ch-color-attention: #c084fc;
  --ch-color-attention-strong: #a855f7;
  --ch-color-attention-bg: rgba(192, 132, 252, 0.15);
  --ch-color-info: #38bdf8;
  --ch-color-danger: #f87171;
  --ch-color-danger-strong: #dc2626;
  --ch-color-danger-hover: #b91c1c;
  --ch-color-danger-bg: #3f1d1d;
  --ch-color-danger-border: #7f1d1d;
  --ch-color-danger-text: #fecaca;
  --ch-color-discovery-bg: #281f35;
  --ch-color-discovery-border: #4c1d95;
  --ch-color-chip-bg: rgba(255, 255, 255, 0.08);
  --ch-color-chip-bg-muted: rgba(255, 255, 255, 0.07);
  --ch-color-row-hover: rgba(255, 255, 255, 0.06);
  --ch-shadow-popover: 0 18px 44px rgba(0, 0, 0, 0.42);
  --ch-shadow-dialog: 0 24px 80px rgba(0, 0, 0, 0.45);
  --ch-shadow-soft: 0 4px 20px rgba(0, 0, 0, 0.4);
  --ch-shadow-color-soft: rgba(0, 0, 0, 0.32);
  --ch-radius-sm: 6px;
  --ch-radius-md: 8px;
  --ch-radius-lg: 12px;
  --ch-radius-xl: 16px;
  --ch-font-sans: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --ch-font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --ch-motion-fast: 150ms cubic-bezier(0.2, 0, 0, 1);
  --ch-motion-standard: 200ms cubic-bezier(0.2, 0, 0, 1);
  --ch-shadow-button: 0 1px 2px rgba(0, 0, 0, 0.08);
  --ch-shadow-button-hover: 0 4px 12px rgba(0, 0, 0, 0.15);
  --ch-shadow-button-primary: 0 2px 6px rgba(59, 130, 246, 0.35);
  --ch-font-size-xs: 11px;
  --ch-font-size-sm: 12px;
  --ch-font-size-base: 13px;
  --ch-font-size-md: 14px;
  --ch-font-size-lg: 15px;
  --ch-tab-fade-start: #1e1e1e;
  --ch-tab-fade-end: rgba(30, 30, 30, 0);
  --ch-terminal-bg: #1f1f1f;
  --ch-terminal-canvas-filter: none;
  --ch-terminal-fg: #e5e7eb;
  --ch-terminal-cursor: #f4f4f5;
  --ch-terminal-selection: rgba(96, 165, 250, 0.28);
  --ch-terminal-black: #1f2937;
  --ch-terminal-red: #c26b6b;
  --ch-terminal-green: #4f8f63;
  --ch-terminal-yellow: #b8944a;
  --ch-terminal-blue: #5f8fbf;
  --ch-terminal-magenta: #8b6fb2;
  --ch-terminal-cyan: #4f8f98;
  --ch-terminal-white: #d4d4d8;
  --ch-terminal-bright-black: #71717a;
  --ch-terminal-bright-red: #fca5a5;
  --ch-terminal-bright-green: #86efac;
  --ch-terminal-bright-yellow: #fde047;
  --ch-terminal-bright-blue: #93c5fd;
  --ch-terminal-bright-magenta: #d8b4fe;
  --ch-terminal-bright-cyan: #67e8f9;
  --ch-terminal-bright-white: #ffffff;
}

:root[data-theme='light'] {
  color-scheme: light;
  --ch-color-app-bg: #f7f7f5;
  --ch-color-canvas: #fafaf8;
  --ch-color-surface: #fcfcfb;
  --ch-color-surface-raised: #fdfdfc;
  --ch-color-surface-soft: #f2f2f0;
  --ch-color-surface-muted: #ececea;
  --ch-color-surface-control: #f1f1ef;
  --ch-color-surface-control-hover: #e9e9e7;
  --ch-color-surface-control-active: #e1e1de;
  --ch-color-surface-pressed: #d9d9d5;
  --ch-color-surface-selected: #fdfdfc;
  --ch-color-surface-sunken: #f0f0ee;
  --ch-color-surface-glass: rgba(252, 252, 251, 0.96);
  --ch-color-overlay: rgba(24, 24, 24, 0.38);
  --ch-color-overlay-soft: rgba(24, 24, 24, 0.3);
  --ch-color-border: #e2e2df;
  --ch-color-border-muted: #ebebe8;
  --ch-color-border-strong: #d1d1cc;
  --ch-color-border-hover: #b7b7b0;
  --ch-color-text: #262626;
  --ch-color-text-strong: #171717;
  --ch-color-text-muted: #686868;
  --ch-color-text-subtle: #9a9a96;
  --ch-color-text-soft: #777771;
  --ch-color-text-code: #242424;
  --ch-color-accent: #60a5fa;
  --ch-color-accent-strong: #3b82f6;
  --ch-color-accent-hover: #2563eb;
  --ch-color-accent-soft: rgba(59, 130, 246, 0.12);
  --ch-color-accent-ring: rgba(59, 130, 246, 0.15);
  --ch-color-accent-ring-strong: rgba(59, 130, 246, 0.4);
  --ch-color-success: #36734d;
  --ch-color-success-strong: #2f6543;
  --ch-color-success-hover: #28583a;
  --ch-color-success-bg: #e4eee6;
  --ch-color-warning: #906018;
  --ch-color-warning-strong: #81530f;
  --ch-color-warning-bg: #f1e8d6;
  --ch-color-attention: #6d5b8b;
  --ch-color-attention-strong: #5f4f7d;
  --ch-color-attention-bg: #ece8f1;
  --ch-color-info: #4e7185;
  --ch-color-danger: #ad3f3f;
  --ch-color-danger-strong: #9f3636;
  --ch-color-danger-hover: #882d2d;
  --ch-color-danger-bg: #f1dfdf;
  --ch-color-danger-border: #e1c1c1;
  --ch-color-danger-text: #7c2b2b;
  --ch-color-discovery-bg: #ece8f1;
  --ch-color-discovery-border: #c7bdcf;
  --ch-color-chip-bg: rgba(38, 38, 38, 0.06);
  --ch-color-chip-bg-muted: rgba(38, 38, 38, 0.04);
  --ch-color-row-hover: rgba(38, 38, 38, 0.05);
  --ch-shadow-popover: 0 18px 44px rgba(24, 24, 24, 0.11);
  --ch-shadow-dialog: 0 24px 70px rgba(24, 24, 24, 0.13);
  --ch-shadow-soft: 0 12px 30px rgba(24, 24, 24, 0.08);
  --ch-shadow-color-soft: rgba(24, 24, 24, 0.08);
  --ch-radius-sm: 6px;
  --ch-radius-md: 8px;
  --ch-radius-lg: 12px;
  --ch-radius-xl: 16px;
  --ch-font-sans: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --ch-font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --ch-motion-fast: 150ms cubic-bezier(0.2, 0, 0, 1);
  --ch-motion-standard: 200ms cubic-bezier(0.2, 0, 0, 1);
  --ch-shadow-button: 0 1px 2px rgba(0, 0, 0, 0.08);
  --ch-shadow-button-hover: 0 4px 12px rgba(0, 0, 0, 0.15);
  --ch-shadow-button-primary: 0 2px 6px rgba(59, 130, 246, 0.35);
  --ch-font-size-xs: 11px;
  --ch-font-size-sm: 12px;
  --ch-font-size-base: 13px;
  --ch-font-size-md: 14px;
  --ch-font-size-lg: 15px;
  --ch-tab-fade-start: #fcfcfb;
  --ch-tab-fade-end: rgba(252, 252, 251, 0);
  --ch-terminal-bg: #f6f6f4;
  --ch-terminal-canvas-filter: contrast(0.78) saturate(0.55) brightness(1.22);
  --ch-terminal-fg: #262626;
  --ch-terminal-cursor: #33455a;
  --ch-terminal-selection: rgba(77, 102, 128, 0.18);
  --ch-terminal-black: #ececea;
  --ch-terminal-red: #f0d4d0;
  --ch-terminal-green: #dcecdf;
  --ch-terminal-yellow: #efe5c7;
  --ch-terminal-blue: #d8e3ec;
  --ch-terminal-magenta: #e4ddec;
  --ch-terminal-cyan: #dce9ec;
  --ch-terminal-white: #f6f6f4;
  --ch-terminal-bright-black: #777771;
  --ch-terminal-bright-red: #9f3636;
  --ch-terminal-bright-green: #2f6543;
  --ch-terminal-bright-yellow: #81530f;
  --ch-terminal-bright-blue: #3f536b;
  --ch-terminal-bright-magenta: #5f4f7d;
  --ch-terminal-bright-cyan: #4e7185;
  --ch-terminal-bright-white: #fdfdfc;
}

html, body, #app {
  height: 100%;
  overflow: hidden;
}

body {
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  font-family: var(--ch-font-sans);
  font-feature-settings: 'cv02', 'cv03', 'cv04', 'cv11';
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  letter-spacing: -0.01em;
}

h1,
h2,
h3,
h4 {
  color: var(--ch-color-text-strong);
}

button,
input,
select,
textarea {
  font: inherit;
}

/* ==========================================================================
   Unified Design System — Global UI primitives
   ========================================================================== */

/* Default form input / select / textarea */
.ch-input,
.ch-select,
.ch-textarea {
  width: 100%;
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  line-height: 1.4;
  transition: border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), background var(--ch-motion-fast);
}

.ch-input,
.ch-select {
  height: 36px;
  padding: 0 12px;
}

.ch-textarea {
  min-height: 120px;
  padding: 10px 12px;
  resize: vertical;
}

.ch-input:hover,
.ch-select:hover,
.ch-textarea:hover {
  border-color: var(--ch-color-border-hover);
}

.ch-input:focus,
.ch-select:focus,
.ch-textarea:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.ch-input:disabled,
.ch-select:disabled,
.ch-textarea:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* Base button — all .ch-btn variants share these */
.ch-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  padding: 0 14px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
  transition: background var(--ch-motion-fast),
              border-color var(--ch-motion-fast),
              color var(--ch-motion-fast),
              box-shadow var(--ch-motion-fast),
              transform var(--ch-motion-fast);
}

.ch-btn:hover:not(:disabled) {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
}

.ch-btn:active:not(:disabled) {
  background: var(--ch-color-surface-pressed);
  transform: translateY(1px);
}

.ch-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.ch-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Primary CTA */
.ch-btn--primary {
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
  font-weight: 600;
  box-shadow: var(--ch-shadow-button);
}

.ch-btn--primary:hover:not(:disabled) {
  background: var(--ch-color-accent-hover);
  border-color: var(--ch-color-accent-hover);
  box-shadow: var(--ch-shadow-button-primary);
}

.ch-btn--primary:active:not(:disabled) {
  background: var(--ch-color-accent-hover);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

/* Danger button */
.ch-btn--danger {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.ch-btn--danger:hover:not(:disabled) {
  background: var(--ch-color-danger-strong);
  border-color: var(--ch-color-danger-hover);
  color: var(--ch-color-text-inverse);
}

/* Ghost/text button — no border, transparent bg */
.ch-btn--ghost {
  background: transparent;
  border-color: transparent;
  color: var(--ch-color-text-muted);
}

.ch-btn--ghost:hover:not(:disabled) {
  background: var(--ch-color-chip-bg);
  border-color: transparent;
  color: var(--ch-color-text);
}

/* Warning / abort button */
.ch-btn--warning {
  background: var(--ch-color-warning-bg);
  border-color: color-mix(in srgb, var(--ch-color-warning-strong) 50%, var(--ch-color-border-strong));
  color: var(--ch-color-warning);
}

.ch-btn--warning:hover:not(:disabled) {
  background: color-mix(in srgb, var(--ch-color-warning-bg) 60%, var(--ch-color-warning-strong));
  border-color: var(--ch-color-warning-strong);
  color: var(--ch-color-text-strong);
}

/* Size variants */
.ch-btn--sm {
  height: 28px;
  padding: 0 10px;
  font-size: var(--ch-font-size-sm);
  border-radius: var(--ch-radius-sm);
}

.ch-btn--lg {
  height: 40px;
  padding: 0 20px;
  font-size: var(--ch-font-size-md);
  border-radius: var(--ch-radius-lg);
}

/* Icon-only square button */
.ch-btn--icon {
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: var(--ch-radius-md);
}

.ch-btn--icon.ch-btn--sm {
  width: 28px;
  height: 28px;
}

/* Chip / pill button */
.ch-btn--chip {
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
}

/* Segmented control container */
.ch-segmented {
  display: inline-grid;
  grid-auto-flow: column;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
}

.ch-segmented__btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.ch-segmented__btn:hover {
  color: var(--ch-color-text);
}

.ch-segmented__btn.active {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-accent-ring-strong);
  color: var(--ch-color-text);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
}

.ch-segmented__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Label */
.ch-label {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
}

/* Hint text */
.ch-hint {
  margin-top: 6px;
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-xs);
  line-height: 1.4;
}

.app {
  height: var(--visual-viewport-height, 100dvh);
  display: flex;
  flex-direction: column;
  background-color: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  min-height: 0;
}

.error-banner {
  background-color: var(--ch-color-danger-strong);
  color: var(--ch-color-text-inverse);
  padding: 8px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.auth-error-banner {
  background-color: var(--ch-color-warning-bg);
  color: var(--ch-color-warning);
  border: 1px solid var(--ch-color-warning);
  border-width: 0 0 1px 0;
  padding: 10px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  font-weight: 500;
}

.auth-error-banner__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.auth-error-banner__retry {
  background: var(--ch-color-warning);
  color: #1a1a1a;
  border: 1px solid var(--ch-color-warning-strong);
  border-radius: var(--ch-radius-md);
  padding: 6px 14px;
  height: 30px;
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.auth-error-banner__retry:hover {
  background: var(--ch-color-warning-strong);
}

.auth-error-banner__retry:active {
  transform: translateY(1px);
}

.auth-error-banner__retry:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.auth-error-banner__close {
  background: none;
  border: none;
  color: inherit;
  font-size: 22px;
  cursor: pointer;
  padding: 0 6px;
  line-height: 1;
  opacity: 0.8;
  transition: opacity var(--ch-motion-fast);
}

.auth-error-banner__close:hover {
  opacity: 1;
}

.app-mode-bar {
  position: relative;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  max-height: 52px;
  min-height: 0;
  padding: 8px 12px;
  border-bottom: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface);
  overflow: visible;
  transition: max-height 200ms cubic-bezier(0.2, 0, 0, 1), padding 200ms cubic-bezier(0.2, 0, 0, 1), border-color 200ms cubic-bezier(0.2, 0, 0, 1), opacity 160ms ease, transform 200ms cubic-bezier(0.2, 0, 0, 1);
}

.mode-switch {
  display: inline-grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(108px, max-content);
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-sunken);
}

.mode-button {
  height: 30px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  padding: 0 14px;
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  letter-spacing: 0.01em;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
}

.mode-button:hover {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control);
}

.mode-button.active {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-accent-ring-strong);
  color: var(--ch-color-text);
  box-shadow: 0 1px 3px var(--ch-shadow-color-soft);
}

.mode-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.app-mode-tools {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}

.theme-switch {
  position: relative;
  display: inline-grid;
  grid-template-columns: 1fr 1fr;
  align-items: center;
  width: 108px;
  height: 32px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  padding: 3px;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  transition: border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
}

.theme-switch:hover {
  border-color: var(--ch-color-border-hover);
}

.theme-switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.theme-switch-label {
  position: relative;
  z-index: 1;
  text-align: center;
}

.theme-switch-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: calc(50% - 3px);
  height: calc(100% - 6px);
  border-radius: 999px;
  background: var(--ch-color-surface-control-hover);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  transition: transform var(--ch-motion-standard), background var(--ch-motion-fast);
}

.theme-switch[aria-checked='true'] .theme-switch-thumb {
  transform: translateX(100%);
}

.theme-switch[aria-checked='false'] .theme-switch-label:first-child,
.theme-switch[aria-checked='true'] .theme-switch-label:nth-child(2) {
  color: var(--ch-color-text);
}

.error-close {
  background: none;
  border: none;
  color: var(--ch-color-text-inverse);
  font-size: 18px;
  cursor: pointer;
  padding: 4px 8px;
  line-height: 1;
  border-radius: var(--ch-radius-sm);
  transition: opacity var(--ch-motion-fast), background var(--ch-motion-fast);
  opacity: 0.85;
}

.error-close:hover {
  opacity: 1;
  background: rgba(255, 255, 255, 0.15);
}

.terminal-mode-shell {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* When the workspace view is active, keep the terminal shell rendered (so
   iframe content stays live and switching back is instant) but remove it from
   the layout flow and make it non-interactive. Using visibility:hidden instead
   of display:none means the xterm.js canvases keep their pixels and don't need
   a full re-render on tab switch. */
.terminal-mode-shell--hidden {
  position: absolute;
  inset: 0;
  visibility: hidden;
  pointer-events: none;
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--ch-color-text-soft);
}

.empty-state h2 {
  margin-bottom: 8px;
  color: var(--ch-color-text);
}

.loading-state {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background-color: var(--ch-color-app-bg);
  color: var(--ch-color-text-soft);
}

.loading-spinner {
  width: 40px;
  height: 40px;
  border: 4px solid var(--ch-color-border);
  border-top-color: var(--ch-color-accent);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-bottom: 16px;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 520px) {
  .app-mode-bar {
    gap: 8px;
    padding: 6px 8px;
  }

  .mode-switch {
    min-width: 0;
    flex: 1;
    grid-auto-columns: minmax(0, 1fr);
  }

  .mode-button {
    min-width: 0;
    padding: 0 8px;
    font-size: 12px;
  }

  .app-mode-tools {
    gap: 6px;
  }

  .theme-switch {
    width: 96px;
    flex: 0 0 auto;
  }
}

@media (max-width: 768px) {
  .app[data-mode='terminal'] {
    height: var(--stable-viewport-height, 100dvh);
  }

  .app[data-mode='terminal'] .app-mode-bar,
  .app[data-mode='workspace'] .app-mode-bar,
  .app[data-mode='terminal'] .layout-selector--row {
    display: none;
  }
}
</style>
