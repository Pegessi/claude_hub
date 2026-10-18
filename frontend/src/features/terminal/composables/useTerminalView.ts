import { onMounted, onUnmounted, ref, watch, computed, ComponentPublicInstance, } from 'vue'
import { storeToRefs } from 'pinia'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useTerminalConnecting, getConnectingState } from '@/composables/useTerminalConnecting'
import {
  decideStatusChangeReplay,
  decideSwitchReplay,
} from '@/utils/terminalSwitchPolicy'
import {
  bumpIframeDocumentGeneration,
  getIframeDocumentGeneration,
  isStalePaneRefreshDone,
  shouldDedupePaneRecovery,
  type PaneRecoveryCorrelation,
} from '@/utils/terminalPaneRecovery'
import { computeCacheUpdate } from '@/utils/terminalCachePolicy'
import type { AgentType, AgentRuntimeStatus } from '@/types'
import {
  SabInputRing,
  buildIframeSabScript,
  sabInputSupported,
  type TerminalKeyItem,
  type TerminalKeyState,
} from '@/features/terminal/state/terminalInputRing'
import { buildTerminalIframeHandlerScript } from '@/features/terminal/state/terminalIframeScript'
import { terminalIframeSrc } from '@/features/terminal/state/terminalIframeSource'
import { createTerminalThemePayload, terminalThemePayloadKey } from '@/features/terminal/state/terminalTheme'
type TerminalHistoryRefreshOptions = {
  reason?: string
  scrollToBottom?: boolean
  preserveUserScroll?: boolean
}
export interface TerminalViewProps {
  tabId: string
  agentType?: AgentType
}
export function useTerminalView(props: TerminalViewProps) {
  type IframeWithSabCache = HTMLIFrameElement & { __sabDrainScript?: HTMLScriptElement }
  const iframeRefs: Record<string, HTMLIFrameElement | null> = {}
  const cachedTabIds = ref<string[]>([])
  const tabRecency = ref<string[]>([])
  const terminalContainer = ref<HTMLElement | null>(null)
  const {
    loadedTabIds,
    timeoutTabIds,
    errorTabIds,
    startConnectingTimer,
    resetTabConnectingState,
    markLoaded,
    markError,
    retryTab,
    clearAllTimers,
  } = useTerminalConnecting((tabId: string) => {
    const iframe = iframeRefs[tabId]
    if (iframe) iframe.src = terminalIframeSrc(tabId)
  })
  const appStore = useAppStore()
  const terminalStore = useTerminalStore()
  const { colorScheme } = storeToRefs(appStore)
  const { layoutType, agentStatuses } = storeToRefs(terminalStore)
  const lastAgentStatus = ref<AgentRuntimeStatus | null>(null)
  const pendingSafeReplay = ref(false)
  const pendingSafeReplayTabId = ref<string | null>(null)
  const iframeDocumentGeneration: Record<string, number> = {}
  let paneRecoveryInFlight: PaneRecoveryCorrelation | null = null
  function iframeWindowForTab(tabId: string): Window | null {
    const iframe = iframeRefs[tabId] || getTerminalState().iframes[tabId]
    return iframe?.contentWindow ?? null
  }
  const contentReady = ref(true)
  const contentError = ref(false)
  const currentAgentStatus = computed<AgentRuntimeStatus | null>(
    () => agentStatuses.value.find(s => s.tab_id === props.tabId)?.status ?? null
  )
  const activeTabState = computed<'connecting' | 'timeout' | 'error' | 'loaded'>(() =>
    getConnectingState(props.tabId, loadedTabIds.value, timeoutTabIds.value, errorTabIds.value),
  )
  const reconnectStatus = computed(() => terminalStore.paneReconnectStatus(props.tabId))
  const reconnectClearTimers = new Map<string, number>()
  const reconnectTouchedTabs = new Set<string>()
  function scheduleReconnectClear(tabId: string, delayMs: number) {
    const existing = reconnectClearTimers.get(tabId)
    if (existing !== undefined) window.clearTimeout(existing)
    const handle = window.setTimeout(() => {
      reconnectClearTimers.delete(tabId)
      terminalStore.clearPaneReconnect(tabId)
    }, delayMs)
    reconnectClearTimers.set(tabId, handle)
  }
  function clearReconnectFeedbackTimers() {
    reconnectClearTimers.forEach(handle => window.clearTimeout(handle))
    reconnectClearTimers.clear()
  }
  watch(
    () => ({ tabId: props.tabId, nonce: reconnectStatus.value.nonce }),
    (next, prev) => {
      if (!prev || prev.tabId !== next.tabId || next.nonce <= prev.nonce) return
      reconnectTouchedTabs.add(next.tabId)
      const previous = reconnectClearTimers.get(next.tabId)
      if (previous !== undefined) {
        window.clearTimeout(previous)
        reconnectClearTimers.delete(next.tabId)
      }
      retryTab(next.tabId)
    },
  )
  let terminalResizeObserver: ResizeObserver | null = null
  let keyboardResizeSettleTimer: number | null = null
  let keyboardResizeSettlesAt = 0
  let lastKeyboardOpenState = false
  let pendingKeyboardResizeAll = false
  const pendingKeyboardResizeTabIds = new Set<string>()
  let pendingResizeRafId: number | null = null
  const pendingResizeTabIds = new Set<string>()
  let pendingResizeAll = false
  let lastThemeKey: string | null = null
  const MOBILE_TERMINAL_BREAKPOINT_PX = 768
  const MOBILE_KEYBOARD_RESIZE_SETTLE_MS = 260
  const MAX_SINGLE_PANE_CACHED_TERMINALS = 4
  const CONTENT_REFRESH_TIMEOUT_MS = 8000
  function makeHistoryRequestId(prefix: string): string {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  }
  function getTerminalState(): TerminalKeyState {
    if (!window.__claudeHub.terminalState) {
      const state: TerminalKeyState = {
        iframes: {},
        ready: {},
        queues: {},
        inputRing: {},
      }
      window.__claudeHub.terminalState = state as unknown as import('@/types').TerminalKeyState
    }
    return window.__claudeHub.terminalState as unknown as TerminalKeyState
  }
  function getOrCreateInputRing(tabId: string): SabInputRing | null {
    if (!sabInputSupported()) return null
    const state = getTerminalState()
    if (!state.inputRing[tabId]) {
      state.inputRing[tabId] = new SabInputRing(tabId)
    }
    return state.inputRing[tabId] || null
  }
  function cacheTabId(tabId: string) {
    if (!tabId) return
    const { cachedTabIds: nextCached, tabRecency: nextRecency, evicted } = computeCacheUpdate(
      { cachedTabIds: cachedTabIds.value, tabRecency: tabRecency.value },
      tabId,
      layoutType.value,
      MAX_SINGLE_PANE_CACHED_TERMINALS,
    )
    evicted.forEach((id) => {
      resetTabConnectingState(id)
      delete iframeDocumentGeneration[id]
      const pendingTimer = reconnectClearTimers.get(id)
      if (pendingTimer !== undefined) {
        window.clearTimeout(pendingTimer)
        reconnectClearTimers.delete(id)
      }
      terminalStore.clearPaneReconnect(id)
    })
    cachedTabIds.value = nextCached
    tabRecency.value = nextRecency
    startConnectingTimer(tabId)
  }
  let tabSwitchGeneration = 0
  let documentReadyListener: ((event: Event) => void) | null = null
  let documentReadyErrorTimer: number | null = null
  function stopDocumentReadyWait() {
    if (documentReadyListener) {
      window.removeEventListener('terminal-history-refresh-done', documentReadyListener)
      documentReadyListener = null
    }
    if (documentReadyErrorTimer !== null) {
      window.clearTimeout(documentReadyErrorTimer)
      documentReadyErrorTimer = null
    }
  }
  function waitForDocumentReady(
    tabId: string,
    requestId: string,
    correlation: PaneRecoveryCorrelation,
  ) {
    stopDocumentReadyWait()
    let settled = false
    function correlationStillValid(): boolean {
      return !isStalePaneRefreshDone(
        correlation,
        tabSwitchGeneration,
        getIframeDocumentGeneration(iframeDocumentGeneration, tabId),
      )
    }
    function onDone(event: Event) {
      const detail = (event as CustomEvent).detail
      if (!detail || detail.tabId !== tabId || detail.requestId !== requestId) return
      if (!correlationStillValid()) return
      settled = true
      stopDocumentReadyWait()
      if (
        paneRecoveryInFlight?.tabId === correlation.tabId &&
        paneRecoveryInFlight?.tabSwitchGeneration === correlation.tabSwitchGeneration &&
        paneRecoveryInFlight?.documentGeneration === correlation.documentGeneration
      ) {
        paneRecoveryInFlight = null
      }
      if (detail.ok === false) {
        contentError.value = true
        return
      }
      contentError.value = false
      contentReady.value = true
      if (detail.deferredRecovery) {
        const stable =
          currentAgentStatus.value === 'idle' || currentAgentStatus.value === 'attention'
        if (stable) {
          postTerminalHistoryRefresh(tabId, {
            reason: 'deferred-status-refresh',
            scrollToBottom: true,
            preserveUserScroll: true,
          })
        } else {
          pendingSafeReplay.value = true
          pendingSafeReplayTabId.value = tabId
        }
      }
      scheduleTerminalResize(tabId)
    }
    documentReadyListener = onDone
    window.addEventListener('terminal-history-refresh-done', onDone)
    documentReadyErrorTimer = window.setTimeout(() => {
      if (settled) return
      if (!correlationStillValid()) return
      contentError.value = true
    }, CONTENT_REFRESH_TIMEOUT_MS)
  }
  function makeRecoveryCorrelation(
    tabId: string,
    tabSwitchGen: number,
    documentGeneration: number,
  ): PaneRecoveryCorrelation {
    return { tabId, tabSwitchGeneration: tabSwitchGen, documentGeneration }
  }
  function revealCachedTabSwitch(tabId: string): void {
    if (!tabId || tabId !== props.tabId) {
      scheduleTerminalResize(tabId)
      return
    }
    contentError.value = false
    contentReady.value = true
    scheduleTerminalResize(tabId)
    const switchAction = decideSwitchReplay(props.agentType, currentAgentStatus.value)
    if (switchAction === 'defer') {
      pendingSafeReplay.value = true
      pendingSafeReplayTabId.value = tabId
    }
  }
  function armBootstrapDocumentWait(
    tabId: string,
    tabSwitchGen: number,
    documentGeneration: number,
  ): void {
    if (!tabId || tabId !== props.tabId) {
      scheduleTerminalResize(tabId)
      return
    }
    if (shouldDedupePaneRecovery(paneRecoveryInFlight, tabId, tabSwitchGen, documentGeneration)) {
      return
    }
    if (
      paneRecoveryInFlight &&
      paneRecoveryInFlight.tabId === tabId &&
      paneRecoveryInFlight.documentGeneration !== documentGeneration
    ) {
      stopDocumentReadyWait()
      paneRecoveryInFlight = null
    }
    contentError.value = false
    contentReady.value = false
    const requestId = makeHistoryRequestId('bootstrap')
    const iframeWindow = iframeWindowForTab(tabId)
    const sent = postTerminalMessage(tabId, {
      type: 'terminal-bootstrap-correlation',
      requestId,
      documentGeneration,
      agentStatus: currentAgentStatus.value,
    })
    const correlation = makeRecoveryCorrelation(tabId, tabSwitchGen, documentGeneration)
    if (!sent || !iframeWindow) {
      contentError.value = true
      return
    }
    paneRecoveryInFlight = correlation
    waitForDocumentReady(tabId, requestId, correlation)
  }
  watch(
    () => props.tabId,
    (newTabId, oldTabId) => {
      cacheTabId(newTabId)
      lastAgentStatus.value = null
      pendingSafeReplay.value = false
      pendingSafeReplayTabId.value = null
      ++tabSwitchGeneration
      requestAnimationFrame(() => {
        if (!oldTabId || oldTabId === newTabId) {
          const docGen = getIframeDocumentGeneration(iframeDocumentGeneration, newTabId)
          if (docGen === 0) {
            contentError.value = false
            contentReady.value = false
            return
          }
          return
        }
        if (isMobileTerminalViewport()) {
          contentError.value = false
          contentReady.value = true
          scheduleTerminalResize(newTabId)
          return
        }
        revealCachedTabSwitch(newTabId)
      })
    },
    { immediate: true }
  )
  watch(currentAgentStatus, (newStatus) => {
    const pendingForCurrentTab =
      pendingSafeReplay.value && pendingSafeReplayTabId.value === props.tabId
    const { replay, clearPending } = decideStatusChangeReplay(
      lastAgentStatus.value,
      newStatus,
      pendingForCurrentTab,
    )
    if (replay) {
      const reason =
        lastAgentStatus.value === 'working' ? 'auto-round-complete' : 'deferred-status-refresh'
      postTerminalHistoryRefresh(props.tabId, {
        reason,
        scrollToBottom: true,
        preserveUserScroll: true,
      })
    }
    if (clearPending) {
      pendingSafeReplay.value = false
      pendingSafeReplayTabId.value = null
    }
    lastAgentStatus.value = newStatus
  })
  watch(layoutType, () => {
    if (layoutType.value !== '1x1' && props.tabId) {
      const { cachedTabIds: nextCached, tabRecency: nextRecency, evicted } = computeCacheUpdate(
        { cachedTabIds: cachedTabIds.value, tabRecency: tabRecency.value },
        props.tabId,
        layoutType.value,
        MAX_SINGLE_PANE_CACHED_TERMINALS,
      )
      evicted.forEach((id) => {
        resetTabConnectingState(id)
        delete iframeDocumentGeneration[id]
      })
      cachedTabIds.value = nextCached
      tabRecency.value = nextRecency
    }
  })
  function registerIframe(el: Element | ComponentPublicInstance | null, tabId: string) {
    const previous = iframeRefs[tabId]
    const state = getTerminalState()
    if (el instanceof HTMLIFrameElement) {
      iframeRefs[tabId] = el as HTMLIFrameElement
      state.iframes[tabId] = el as HTMLIFrameElement
      if (state.ready[tabId]) {
        flushKeyQueue(tabId)
      }
    } else {
      if (state.iframes[tabId] === previous) {
        delete state.iframes[tabId]
        delete state.ready[tabId]
      }
      delete iframeRefs[tabId]
      if (state.inputRing[tabId]) {
        state.inputRing[tabId] = null
        delete state.inputRing[tabId]
      }
    }
  }
  function postTerminalKey(tabId: string, item: TerminalKeyItem): boolean {
    const state = getTerminalState()
    const iframe = state.iframes[tabId]
    if (!iframe || !iframe.contentWindow) return false
    const ring = state.inputRing[tabId]
    if (ring && ring.tryWrite(item)) {
      try {
        iframe.contentWindow.postMessage({ type: '__claudeHubSabNudge', tabId }, '*')
      } catch {
        /* nudge is optional */
      }
      return true
    }
    iframe.contentWindow.postMessage({
      type: 'terminal-key',
      key: item.key,
      ctrl: item.ctrl,
      shift: item.shift,
      tabId,
    }, '*')
    return true
  }
  function queueTerminalKey(tabId: string, item: TerminalKeyItem) {
    const state = getTerminalState()
    if (!state.queues[tabId]) {
      state.queues[tabId] = []
    }
    state.queues[tabId].push(item)
  }
  function flushKeyQueue(tabId: string) {
    const state = getTerminalState()
    const queue = state.queues[tabId]
    if (!queue || queue.length === 0) return
    while (queue.length > 0) {
      const item = queue[0]
      if (!postTerminalKey(tabId, item)) return
      queue.shift()
    }
  }
  function postTerminalTheme(tabId?: string) {
    const payload = createTerminalThemePayload(colorScheme.value)
    const key = terminalThemePayloadKey(payload)
    if (key === lastThemeKey && !tabId) {
      return
    }
    lastThemeKey = key
    const targetTabIds = tabId ? [tabId] : Object.keys(iframeRefs)
    for (const id of targetTabIds) {
      const iframe = iframeRefs[id]
      if (!iframe?.contentWindow) continue
      iframe.contentWindow.postMessage({
        type: 'terminal-theme',
        payload,
      }, '*')
    }
  }
  function postTerminalResize(tabId?: string) {
    const targetTabIds = tabId ? [tabId] : Object.keys(iframeRefs)
    for (const id of targetTabIds) {
      if (id !== props.tabId) continue
      const iframe = iframeRefs[id]
      if (!iframe?.contentWindow) continue
      iframe.contentWindow.postMessage({
        type: 'terminal-resize',
      }, '*')
    }
  }
  function flushPendingResize() {
    pendingResizeRafId = null
    if (pendingResizeAll) {
      pendingResizeAll = false
      pendingResizeTabIds.clear()
      postTerminalResize()
      return
    }
    const tabIds = Array.from(pendingResizeTabIds)
    pendingResizeTabIds.clear()
    for (const id of tabIds) {
      postTerminalResize(id)
    }
  }
  function enqueueResize(tabId?: string) {
    if (tabId) {
      pendingResizeTabIds.add(tabId)
    } else {
      pendingResizeAll = true
      pendingResizeTabIds.clear()
    }
    if (pendingResizeRafId === null) {
      pendingResizeRafId = requestAnimationFrame(flushPendingResize)
    }
  }
  function postTerminalMessage(tabId: string, message: Record<string, unknown>): boolean {
    const iframe = iframeRefs[tabId] || getTerminalState().iframes[tabId]
    if (!iframe?.contentWindow) return false
    if (!loadedTabIds.value.has(tabId)) return false
    iframe.contentWindow.postMessage({
      ...message,
      tabId,
    }, '*')
    return true
  }
  function postTerminalHistoryRefresh(
    tabId: string,
    options: TerminalHistoryRefreshOptions = {}
  ): string | null {
    const requestId = `hist-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    const sent = postTerminalMessage(tabId, {
      type: 'terminal-history-refresh',
      reason: options.reason || 'manual',
      scrollToBottom: options.scrollToBottom !== false,
      preserveUserScroll: options.preserveUserScroll === true,
      requestId,
    })
    return sent ? requestId : null
  }
  function retryContentRefresh(tabId: string) {
    stopDocumentReadyWait()
    paneRecoveryInFlight = null
    contentError.value = false
    contentReady.value = false
    const docGen = getIframeDocumentGeneration(iframeDocumentGeneration, tabId)
    armBootstrapDocumentWait(tabId, tabSwitchGeneration, docGen)
  }
  function postTerminalScrollBottom(tabId: string): boolean {
    return postTerminalMessage(tabId, {
      type: 'terminal-scroll-bottom',
    })
  }
  function isMobileTerminalViewport() {
    if (typeof window === 'undefined') return false
    return (
      window.innerWidth <= MOBILE_TERMINAL_BREAKPOINT_PX ||
      (typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches)
    )
  }
  function refreshTerminalHistory(tabId?: string) {
    const targetTabId = tabId || window.__claudeHub.activePaneTabId || props.tabId
    if (!targetTabId) return
    postTerminalHistoryRefresh(targetTabId, {
      reason: 'manual',
      scrollToBottom: true,
    })
    window.setTimeout(() => {
      postTerminalResize(targetTabId)
      postTerminalScrollBottom(targetTabId)
    }, 100)
    window.setTimeout(() => postTerminalScrollBottom(targetTabId), 400)
  }
  function isMobileKeyboardResizeActive() {
    if (typeof window === 'undefined') return false
    if (window.innerWidth > MOBILE_TERMINAL_BREAKPOINT_PX) return false
    const now = window.performance.now()
    const root = document.documentElement
    const keyboardOpen = root.dataset.keyboardOpen === 'true'
    if (keyboardOpen !== lastKeyboardOpenState) {
      lastKeyboardOpenState = keyboardOpen
      keyboardResizeSettlesAt = now + MOBILE_KEYBOARD_RESIZE_SETTLE_MS
    }
    return keyboardOpen || now < keyboardResizeSettlesAt
  }
  function flushKeyboardResizeQueue() {
    keyboardResizeSettleTimer = null
    if (pendingKeyboardResizeAll) {
      pendingKeyboardResizeAll = false
      pendingKeyboardResizeTabIds.clear()
      postTerminalResize()
      return
    }
    const tabIds = Array.from(pendingKeyboardResizeTabIds)
    pendingKeyboardResizeTabIds.clear()
    for (const id of tabIds) {
      postTerminalResize(id)
    }
  }
  function queueKeyboardSettledResize(tabId?: string) {
    if (tabId) {
      pendingKeyboardResizeTabIds.add(tabId)
    } else {
      pendingKeyboardResizeAll = true
    }
    if (keyboardResizeSettleTimer !== null) {
      window.clearTimeout(keyboardResizeSettleTimer)
    }
    keyboardResizeSettleTimer = window.setTimeout(flushKeyboardResizeQueue, MOBILE_KEYBOARD_RESIZE_SETTLE_MS)
  }
  function scheduleTerminalResize(tabId?: string, options: { coalesceMobileKeyboard?: boolean } = {}) {
    if (typeof window === 'undefined') return
    if (options.coalesceMobileKeyboard && isMobileKeyboardResizeActive()) {
      queueKeyboardSettledResize(tabId)
      return
    }
    enqueueResize(tabId)
  }
  function onIframeLoad(event: Event, tabId: string) {
    const iframe = event.target as HTMLIFrameElement
    if (!iframe || !iframe.contentDocument) return
    registerIframe(iframe, tabId)
    markLoaded(tabId)
    if (terminalStore.settlePaneReconnect(tabId, 'success')) {
      scheduleReconnectClear(tabId, 2000)
    }
    try {
      const ring = getOrCreateInputRing(tabId)
      if (ring && iframe.contentWindow) {
        try {
          Object.defineProperty(iframe.contentWindow, '__CLAUDE_HUB_SAB_BUFFER__', {
            value: ring.buffer,
            writable: false,
            enumerable: false,
            configurable: true,
          })
          const sabScript = iframe.contentDocument.createElement('script')
          sabScript.textContent = `
  (function () {
    function wait(cb) {
      if (typeof sendText === 'function') { cb(); return; }
      var n = 0;
      var iv = setInterval(function () {
        n++;
        if (typeof sendText === 'function' || n > 200) {
          clearInterval(iv);
          if (typeof sendText === 'function') cb();
        }
      }, 10);
    }
    wait(function () {
  ${buildIframeSabScript(tabId)}
    });
  })();
  `
          ;(iframe as IframeWithSabCache).__sabDrainScript = sabScript
        } catch (err) {
          console.warn('Unable to set up SAB fast input path for tab', tabId, err)
        }
      }
      const script = iframe.contentDocument.createElement('script')
      script.textContent = buildTerminalIframeHandlerScript(props.agentType)
      iframe.contentDocument.head.appendChild(script)
      const sabDrainScript = (iframe as IframeWithSabCache).__sabDrainScript as HTMLScriptElement | undefined
      if (sabDrainScript) {
        iframe.contentDocument.head.appendChild(sabDrainScript)
        delete (iframe as IframeWithSabCache).__sabDrainScript
      }
      postTerminalTheme(tabId)
      scheduleTerminalResize(tabId)
      const docGen = bumpIframeDocumentGeneration(iframeDocumentGeneration, tabId)
      if (tabId === props.tabId) {
        armBootstrapDocumentWait(tabId, tabSwitchGeneration, docGen)
      }
    } catch (e) {
      console.error('Error injecting script into iframe:', e)
      if (tabId === props.tabId) {
        contentError.value = true
      }
    }
  }
  function onIframeError(_event: Event, tabId: string) {
    markError(tabId)
    if (terminalStore.settlePaneReconnect(tabId, 'error')) {
      terminalStore.pushNotification({
        type: 'error',
        message: 'Terminal reconnect failed — please try again',
        autoDismissMs: 8000,
      })
      scheduleReconnectClear(tabId, 6000)
    }
  }
  watch(colorScheme, () => {
    lastThemeKey = null
    requestAnimationFrame(() => {
      postTerminalTheme()
      scheduleTerminalResize()
    })
  })
  function handleMessage(event: MessageEvent) {
    if (!event.data) return
    if (event.data.type === 'terminal-ready') {
      const tabId = event.data.tabId
      if (tabId) {
        getTerminalState().ready[tabId] = true
        getOrCreateInputRing(tabId)
        flushKeyQueue(tabId)
        scheduleTerminalResize(tabId)
      }
    }
    if (event.data.type === 'terminal-not-ready') {
      const tabId = event.data.tabId
      if (tabId) {
        getTerminalState().ready[tabId] = false
      }
    }
    if (event.data.type === 'terminal-history-refresh-done') {
      const tabId = event.data.tabId as string | undefined
      if (!tabId) return
      const expectedSource = iframeWindowForTab(tabId)
      if (!expectedSource || event.source !== expectedSource) return
      window.dispatchEvent(new CustomEvent('terminal-history-refresh-done', {
        detail: event.data,
      }))
    }
    if (event.data.type === 'terminal-select-copied') {
      window.dispatchEvent(new CustomEvent('terminal-select-copied', {
        detail: event.data,
      }))
    }
  }
  onMounted(() => {
    if (typeof window !== 'undefined') {
      window.__claudeHub.registerTerminalIframe = registerIframe
      window.__claudeHub.refreshTerminalHistory = refreshTerminalHistory
      window.__claudeHub.sendTerminalKey = function(key: string, ctrl = false, shift = false) {
        const activePaneTabId = window.__claudeHub.activePaneTabId
        const targetTabId = activePaneTabId || props.tabId
        if (!targetTabId) return false
        const item = { key, ctrl, shift }
        const state = getTerminalState()
        if (state.ready[targetTabId] && postTerminalKey(targetTabId, item)) {
          return true
        }
        if (state.iframes[targetTabId]) {
          queueTerminalKey(targetTabId, item)
        } else {
          console.warn('No iframe found for tab:', targetTabId)
          queueTerminalKey(targetTabId, item)
        }
        return true
      }
      window.__claudeHub.sendTerminalText = function(text: string, tabId?: string) {
        const targetTabId = tabId || window.__claudeHub.activePaneTabId || props.tabId
        if (!targetTabId || !text) return false
        const state = getTerminalState()
        if (!state.ready[targetTabId]) return false
        return postTerminalMessage(targetTabId, {
          type: 'terminal-text',
          text,
        })
      }
      window.__claudeHub.setTerminalSelectMode = function(enabled: boolean, tabId?: string) {
        const targetTabId = tabId || window.__claudeHub.activePaneTabId || props.tabId
        if (!targetTabId) return
        postTerminalMessage(targetTabId, {
          type: 'terminal-select-mode',
          enabled: !!enabled,
        })
      }
      window.addEventListener('message', handleMessage)
      if (terminalContainer.value && typeof ResizeObserver !== 'undefined') {
        terminalResizeObserver = new ResizeObserver(() => {
          const activeId = window.__claudeHub.activePaneTabId
          if (activeId === props.tabId || activeId == null) {
            scheduleTerminalResize(props.tabId, { coalesceMobileKeyboard: true })
          }
        })
        terminalResizeObserver.observe(terminalContainer.value)
      }
      scheduleTerminalResize(props.tabId)
    }
  })
  onUnmounted(() => {
    window.removeEventListener('message', handleMessage)
    pendingSafeReplay.value = false
    pendingSafeReplayTabId.value = null
    stopDocumentReadyWait()
    paneRecoveryInFlight = null
    if (window.__claudeHub.refreshTerminalHistory === refreshTerminalHistory) {
      delete window.__claudeHub.refreshTerminalHistory
    }
    if (window.__claudeHub.registerTerminalIframe === registerIframe) {
      delete window.__claudeHub.registerTerminalIframe
    }
    const sendKeyFn = window.__claudeHub.sendTerminalKey
    if (sendKeyFn) {
      delete window.__claudeHub.sendTerminalKey
    }
    if (window.__claudeHub.sendTerminalText) {
      delete window.__claudeHub.sendTerminalText
    }
    if (window.__claudeHub.setTerminalSelectMode) {
      delete window.__claudeHub.setTerminalSelectMode
    }
    terminalResizeObserver?.disconnect()
    terminalResizeObserver = null
    if (pendingResizeRafId !== null) {
      cancelAnimationFrame(pendingResizeRafId)
      pendingResizeRafId = null
    }
    pendingResizeAll = false
    pendingResizeTabIds.clear()
    if (keyboardResizeSettleTimer !== null) {
      window.clearTimeout(keyboardResizeSettleTimer)
      keyboardResizeSettleTimer = null
    }
    pendingKeyboardResizeAll = false
    pendingKeyboardResizeTabIds.clear()
    lastThemeKey = null
    clearAllTimers()
    clearReconnectFeedbackTimers()
    reconnectTouchedTabs.forEach(tabId => terminalStore.clearPaneReconnect(tabId))
    reconnectTouchedTabs.clear()
  })
  return {
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
  }
}
