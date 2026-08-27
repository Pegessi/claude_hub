<template>
  <div
    class="hmr-harness"
    data-testid="terminal-hmr-harness"
  >
    <TerminalView
      v-if="mounted"
      :key="remountKey"
      :tab-id="activeTabId"
      :agent-type="agentType"
      class="hmr-harness-terminal"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import TerminalView from '@/components/TerminalView.vue'
import type { AgentRuntimeStatus, AgentType } from '@/types'
import { useTerminalStore } from '@/stores/terminalStore'

interface HarnessSnapshot {
  tabId: string
  altTabId: string | null
  activeTabId: string
  remountKey: number
  remountGeneration: number
  remountSettled: boolean
  lastRemountOldIframeDetached: boolean
  iframeNavGeneration: number
  iframeNavSettled: boolean
  terminalMounted: boolean
  contentPending: boolean
  activeIframeSrc: string | null
}

interface TerminalHmrHarnessApi {
  tabId: string
  altTabId: string | null
  remountTerminalView: () => Promise<HarnessSnapshot>
  reloadIframe: () => Promise<HarnessSnapshot>
  switchCachedTab: () => Promise<HarnessSnapshot>
  readHarnessState: () => HarnessSnapshot
  seedAgentStatus: (status: AgentRuntimeStatus) => Promise<void>
}

declare global {
  interface Window {
    __claudeHubHmrHarness?: TerminalHmrHarnessApi
  }
}

const HARNESS_WAIT_MS = 20_000

const params = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '')
const primaryTabId = params.get('tabId') ?? ''
const secondaryTabId = params.get('altTabId') ?? ''
const agentType = (params.get('agentType') ?? 'terminal') as AgentType

const mounted = ref(true)
const remountKey = ref(0)
const remountGeneration = ref(0)
const remountSettled = ref(true)
const iframeNavGeneration = ref(0)
const iframeNavSettled = ref(true)
const lastRemountOldIframeDetached = ref(true)
const activeTabId = ref(primaryTabId)

function activeIframe(): HTMLIFrameElement | null {
  return document.querySelector('.hmr-harness-terminal iframe.active') as HTMLIFrameElement | null
}

function waitUntil(predicate: () => boolean, timeoutMs = HARNESS_WAIT_MS): Promise<void> {
  return new Promise((resolve, reject) => {
    const started = performance.now()
    const tick = () => {
      if (predicate()) {
        resolve()
        return
      }
      if (performance.now() - started > timeoutMs) {
        reject(new Error('harness waitUntil timeout'))
        return
      }
      requestAnimationFrame(tick)
    }
    tick()
  })
}

function readHarnessState(): HarnessSnapshot {
  const iframe = activeIframe()
  return {
    tabId: primaryTabId,
    altTabId: secondaryTabId || null,
    activeTabId: activeTabId.value,
    remountKey: remountKey.value,
    remountGeneration: remountGeneration.value,
    remountSettled: remountSettled.value,
    lastRemountOldIframeDetached: lastRemountOldIframeDetached.value,
    iframeNavGeneration: iframeNavGeneration.value,
    iframeNavSettled: iframeNavSettled.value,
    terminalMounted: mounted.value,
    contentPending: iframe?.classList.contains('content-pending') ?? false,
    activeIframeSrc: iframe?.src ?? null,
  }
}

async function switchCachedTab(): Promise<HarnessSnapshot> {
  if (!secondaryTabId) {
    throw new Error('altTabId query param required for switchCachedTab')
  }
  activeTabId.value = activeTabId.value === primaryTabId ? secondaryTabId : primaryTabId
  await nextTick()
  await waitUntil(() => !!activeIframe())
  return readHarnessState()
}

async function remountTerminalView(): Promise<HarnessSnapshot> {
  remountSettled.value = false
  const oldIframe = activeIframe()

  mounted.value = false
  await nextTick()
  remountKey.value += 1
  remountGeneration.value += 1
  mounted.value = true
  await nextTick()

  await waitUntil(() => {
    const iframe = activeIframe()
    if (!iframe) return false
    if (oldIframe?.isConnected && iframe === oldIframe) return false
    return true
  })

  lastRemountOldIframeDetached.value = oldIframe ? !oldIframe.isConnected : true
  remountSettled.value = true
  return readHarnessState()
}

async function reloadIframe(): Promise<HarnessSnapshot> {
  iframeNavSettled.value = false
  const iframe = activeIframe()
  if (!iframe) {
    throw new Error('no active iframe to reload')
  }

  const navGen = iframeNavGeneration.value + 1
  iframeNavGeneration.value = navGen

  const targetUrl = new URL(iframe.src, window.location.href)
  targetUrl.searchParams.set('_hmrNav', String(navGen))

  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      iframe.removeEventListener('load', onLoad)
      reject(new Error('iframe reload timeout'))
    }, HARNESS_WAIT_MS)

    const onLoad = () => {
      if (iframeNavGeneration.value !== navGen) return
      const src = iframe.src || ''
      if (!src || src === 'about:blank' || src.startsWith('about:blank')) return
      const navMarker = `_hmrNav=${navGen}`
      if (!src.includes(navMarker)) return
      try {
        const docHref = iframe.contentWindow?.location.href ?? ''
        if (!docHref || docHref.includes('about:blank') || !docHref.includes(navMarker)) return
      } catch {
        // Document not readable yet (transient cross-origin) — wait for a later load.
        return
      }
      window.clearTimeout(timer)
      iframe.removeEventListener('load', onLoad)
      resolve()
    }

    iframe.addEventListener('load', onLoad)
    iframe.src = 'about:blank'
    iframe.src = targetUrl.toString()
  })

  iframeNavSettled.value = true
  return readHarnessState()
}

async function seedAgentStatus(status: AgentRuntimeStatus): Promise<void> {
  const store = useTerminalStore()
  store.startAgentStatusPolling()
  const sampledAt = new Date().toISOString()
  const next = store.agentStatuses.filter((entry) => entry.tab_id !== primaryTabId)
  next.push({
    tab_id: primaryTabId,
    tab_name: 'hmr-harness',
    agent_type: agentType,
    status,
    status_text: status,
    tmux_session: `claude-hub-${primaryTabId.slice(0, 8)}`,
    sampled_at: sampledAt,
  })
  store.agentStatuses = next
  await nextTick()
}

onMounted(() => {
  if (typeof window === 'undefined') return
  window.__claudeHubHmrHarness = {
    tabId: primaryTabId,
    altTabId: secondaryTabId || null,
    remountTerminalView,
    reloadIframe,
    switchCachedTab,
    readHarnessState,
    seedAgentStatus,
  }
})

onUnmounted(() => {
  if (typeof window !== 'undefined' && window.__claudeHubHmrHarness?.tabId === primaryTabId) {
    delete window.__claudeHubHmrHarness
  }
})
</script>

<style scoped>
.hmr-harness {
  width: 100vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #111;
}

.hmr-harness-terminal {
  flex: 1;
  min-height: 0;
}
</style>
