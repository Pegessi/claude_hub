<template>
  <div
    ref="terminalContainer"
    class="terminal-container"
  >
    <iframe
      v-for="cachedTabId in cachedTabIds"
      :key="cachedTabId"
      :ref="(el) => registerIframe(el, cachedTabId)"
      :src="terminalIframeSrc(cachedTabId)"
      class="terminal-iframe"
      :class="{ active: cachedTabId === tabId, 'content-pending': cachedTabId === tabId && !contentReady }"
      frameborder="0"
      allowfullscreen
      scrolling="yes"
      @load="onIframeLoad($event, cachedTabId)"
      @error="onIframeError($event, cachedTabId)"
    />
    <!-- Connecting / timeout / error overlay: shown only while the active tab's
         iframe has not yet loaded. Pure chrome — never covers the xterm canvas
         once loaded, and does not alter terminal palette or rendering.
         - connecting: spinner + "Connecting…"            (role=status, polite)
         - timeout:    "Taking longer than expected" + Retry (role=alert, assertive)
         - error:      "Terminal failed to connect" + Retry  (role=alert, assertive) -->
    <div
      v-if="activeTabState !== 'loaded'"
      class="terminal-connecting-overlay"
      :role="activeTabState === 'connecting' ? 'status' : 'alert'"
      :aria-live="activeTabState === 'connecting' ? 'polite' : 'assertive'"
    >
      <template v-if="activeTabState === 'connecting'">
        <span
          class="terminal-connecting-spinner"
          aria-hidden="true"
        />
        <span class="terminal-connecting-text">Connecting…</span>
      </template>
      <template v-else-if="activeTabState === 'timeout'">
        <span
          class="terminal-error-icon"
          aria-hidden="true"
        >⏳</span>
        <span class="terminal-connecting-text">Taking longer than expected</span>
        <button
          type="button"
          class="terminal-retry-button"
          @click="retryTab(tabId)"
        >
          Retry
        </button>
      </template>
      <template v-else>
        <span
          class="terminal-error-icon"
          aria-hidden="true"
        >⚠</span>
        <span class="terminal-connecting-text">Terminal failed to connect</span>
        <button
          type="button"
          class="terminal-retry-button"
          @click="retryTab(tabId)"
        >
          Retry
        </button>
      </template>
    </div>
    <!-- Content-refresh failure overlay: shown when the tab-switch history
         refresh did not complete within CONTENT_REFRESH_TIMEOUT_MS. The pane
         stays hidden (contentReady=false) so the user never sees a stale or
         partial frame; this overlay offers a Retry button to re-trigger the
         history refresh. Only shown when the iframe itself has loaded
         (activeTabState === 'loaded') so it does not collide with the
         connecting overlay. -->
    <div
      v-if="contentError && activeTabState === 'loaded'"
      class="terminal-connecting-overlay"
      role="alert"
      aria-live="assertive"
    >
      <span
        class="terminal-error-icon"
        aria-hidden="true"
      >⏳</span>
      <span class="terminal-connecting-text">Terminal content failed to load</span>
      <button
        type="button"
        class="terminal-retry-button"
        @click="retryContentRefresh(tabId)"
      >
        Retry
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useTerminalView, type TerminalViewProps } from '@/features/terminal/composables/useTerminalView'

const props = defineProps<TerminalViewProps>()
const {
  activeTabState,
  cachedTabIds,
  contentError,
  contentReady,
  onIframeError,
  onIframeLoad,
  registerIframe,
  retryContentRefresh,
  retryTab,
  terminalContainer,
  terminalIframeSrc,
} = useTerminalView(props)
</script>

<style scoped src="@/features/terminal/styles/terminal-view.css"></style>
