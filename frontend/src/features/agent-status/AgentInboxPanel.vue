<template>
  <div
    class="agent-inbox"
    :data-expanded="open"
  >
    <button
      type="button"
      class="inbox-trigger"
      :data-needs="needsCount > 0"
      :aria-expanded="open"
      :title="triggerTitle"
      @click="toggle"
    >
      <span class="inbox-trigger-dot" />
      <span class="inbox-trigger-label">Needs you</span>
      <span
        v-if="needsCount > 0"
        class="inbox-trigger-badge"
        :aria-label="`${needsCount} need you`"
      >{{ needsCount }}</span>
    </button>

    <aside
      v-if="open && !isMobile"
      class="inbox-panel"
      :aria-label="panelLabel"
    >
      <header class="inbox-panel-header">
        <span class="inbox-panel-title">{{ panelLabel }}</span>
        <button
          type="button"
          class="inbox-panel-close"
          aria-label="Close the needs-you list"
          @click="close()"
        >
          ×
        </button>
      </header>
      <AgentInboxContent
        ref="contentRef"
        @open="selectTab"
        @close="close"
      />
      <button
        type="button"
        class="inbox-panel-resize"
        title="Resize the needs-you list"
        aria-hidden="true"
        @pointerdown="startResize"
      />
    </aside>

    <Teleport
      v-if="open && isMobile"
      to="body"
    >
      <div
        class="inbox-sheet-backdrop"
        data-testid="inbox-sheet-backdrop"
        @click.self="close()"
        @keydown.esc.stop="close()"
      >
        <div
          class="inbox-sheet"
          data-testid="inbox-sheet"
          role="dialog"
          aria-modal="true"
          :aria-label="panelLabel"
          @keydown.tab="trapTab"
        >
          <span
            class="inbox-sheet-grip"
            aria-hidden="true"
          />
          <header class="inbox-sheet-head">
            <strong>{{ panelLabel }}</strong>
            <span
              v-if="needsCount > 0"
              class="inbox-sheet-count"
            >{{ needsCount }}</span>
            <button
              ref="sheetCloseRef"
              type="button"
              class="inbox-sheet-close"
              @click="close()"
            >
              Done
            </button>
          </header>
          <div class="inbox-sheet-body">
            <AgentInboxContent
              ref="sheetContentRef"
              variant="sheet"
              @open="selectTab"
              @close="close"
            />
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useViewport } from '@/composables/useViewport'
import AgentInboxContent from './AgentInboxContent.vue'
import { buildInbox, needsYouCount } from './inboxModel'

/**
 * The top bar's single needs-you entry.
 *
 * It replaces the two permanent `Status N` / `Agents N` pills: those counted
 * every tab, so the number was never zero and never meant anything. Here the
 * badge appears only when a session actually needs the user, and the panel
 * separates "needs you" from "working" and "recently done" instead of showing
 * one flat status list twice.
 */
const store = useTerminalStore()
const appStore = useAppStore()
const { tabs, agentStatuses } = storeToRefs(store)
const { mode } = storeToRefs(appStore)
const { isMobile } = useViewport()

// Never restored from storage: an `aria-modal` phone sheet that opens itself
// on reload would trap focus before the user asked for it. The panel is one
// click away instead.
const open = ref(false)
const contentRef = ref<{ focus?: () => void } | null>(null)
// The sheet renders its own content instance, so it needs its own ref: the
// keyboard contract lives in the content container, not in the Done button.
const sheetContentRef = ref<{ focus?: () => void } | null>(null)
const sheetCloseRef = ref<HTMLButtonElement | null>(null)
const nowMs = ref(Date.now())
// The surface is a demand, not a mailbox: what it lists is sessions blocked on
// the user, and its own copy says "Nothing needs you". "Inbox" is already the
// composer's message tray (``ChatMessageInbox``), a different mailbox entirely.
const panelLabel = 'Needs you'

const needsCount = computed(() =>
  needsYouCount(buildInbox(tabs.value, agentStatuses.value, { scope: 'all', nowMs: nowMs.value })),
)

const triggerTitle = computed(() =>
  needsCount.value > 0
    ? `${needsCount.value} session${needsCount.value === 1 ? '' : 's'} waiting for you`
    : 'Nothing needs you right now',
)

let statusPollingActive = false
let clockTimer: number | null = null
let resizeState: { startX: number; startWidth: number; pointerId: number } | null = null

function toggle() {
  if (open.value) {
    close()
    return
  }
  open.value = true
  // Both presentations refresh: the row a phone user wants to see must not
  // wait up to 5 seconds for the next status poll.
  void refreshInbox()
}

function close() {
  open.value = false
}

async function refreshInbox() {
  nowMs.value = Date.now()
  await Promise.all([store.fetchTabs(), store.fetchAgentStatuses()])
}

function selectTab(tabId: string) {
  // Opening a row is the whole point of the inbox: it is the same navigation
  // a deep link performs, and it clears the row's unread state.
  store.setActiveTab(tabId)
  close()
}

function setStatusPolling(active: boolean) {
  if (active === statusPollingActive) return
  statusPollingActive = active
  if (active) {
    void store.fetchTabs()
    store.startAgentStatusPolling()
  } else {
    store.stopAgentStatusPolling()
  }
}

function trapTab(event: KeyboardEvent) {
  const sheet = (event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled])',
  )
  const focusables = [...sheet].filter(element => element.getClientRects().length > 0)
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function startResize(event: PointerEvent) {
  const panel = (event.currentTarget as HTMLElement).closest('.inbox-panel')
  if (!(panel instanceof HTMLElement)) return
  event.preventDefault()
  resizeState = { startX: event.clientX, startWidth: panel.offsetWidth, pointerId: event.pointerId }
  window.addEventListener('pointermove', handleResize)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function handleResize(event: PointerEvent) {
  if (!resizeState) return
  const panel = document.querySelector<HTMLElement>('.inbox-panel')
  if (!panel) return
  const next = resizeState.startWidth + resizeState.startX - event.clientX
  const clamped = Math.min(Math.max(next, 300), Math.min(560, window.innerWidth - 24))
  panel.style.width = `${clamped}px`
}

function stopResize() {
  resizeState = null
  window.removeEventListener('pointermove', handleResize)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
}

watch(open, value => {
  if (value) {
    // Move focus into the surface so the keyboard contract (↑/↓, Enter, →,
    // Esc) works immediately instead of only after clicking a row.
    void nextTickFocus()
  }
})

async function nextTickFocus() {
  await Promise.resolve()
  // Focus the list container in both presentations: that is where the panel's
  // keydown contract (↑/↓, Enter, →, Esc) is handled. Focus on the Done button
  // would silently disable every one of those keys.
  const target = isMobile.value ? sheetContentRef.value : contentRef.value
  target?.focus?.()
}

// Status polling lives with this surface: it is the only consumer of the
// per-tab status payload for the top bar, and it stops when the panel goes
// away or the app leaves terminal mode.
watch(() => mode.value === 'terminal', setStatusPolling, { immediate: true })

onMounted(() => {
  clockTimer = window.setInterval(() => {
    nowMs.value = Date.now()
  }, 30000)
})

onUnmounted(() => {
  setStatusPolling(false)
  stopResize()
  if (clockTimer !== null) window.clearInterval(clockTimer)
  clockTimer = null
})
</script>

<style scoped>
/* Not decorative: the trigger is a control, the badge is information. */
.agent-inbox {
  position: relative;
  display: flex;
  align-self: center;
  flex: 0 0 auto;
}

.inbox-trigger {
  height: 30px;
  box-sizing: border-box;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background-color: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  padding: 0 10px;
  font-size: var(--ch-font-size-sm);
  font-family: var(--ch-font-sans);
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.inbox-trigger:hover,
.agent-inbox[data-expanded='true'] .inbox-trigger {
  background-color: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
}

.inbox-trigger:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.inbox-trigger-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--ch-color-text-muted);
}

/* Amber is the one accent the Hub reserves for "live"/"waiting on you". */
.inbox-trigger[data-needs='true'] .inbox-trigger-dot {
  background: var(--ch-color-warning);
}

.inbox-trigger-label {
  color: inherit;
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
}

.inbox-trigger-badge {
  min-width: 18px;
  height: 18px;
  border-radius: 999px;
  background: var(--ch-color-warning);
  color: var(--ch-color-surface);
  font-size: var(--ch-font-size-xs);
  font-weight: 700;
  line-height: 18px;
  text-align: center;
}

.inbox-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 120;
  width: min(400px, calc(100vw - 16px));
  max-height: min(70vh, 560px, calc(100vh - 72px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-popover);
  backdrop-filter: blur(14px);
}

.inbox-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 10px 8px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.inbox-panel-title {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
}

.inbox-panel-close {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-md);
  cursor: pointer;
}

.inbox-panel-close:hover {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.inbox-panel-close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.inbox-panel-resize {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 16px;
  height: 16px;
  border: 0;
  background: transparent;
  cursor: nesw-resize;
}

.inbox-panel-resize::before {
  content: '';
  position: absolute;
  left: 5px;
  bottom: 5px;
  width: 7px;
  height: 7px;
  border-left: 1px solid var(--ch-color-border-hover);
  border-bottom: 1px solid var(--ch-color-border-hover);
}

.inbox-sheet-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: flex-end;
  background: var(--ch-color-backdrop, var(--ch-color-overlay));
}

.inbox-sheet {
  width: 100%;
  height: 100%;
  max-height: 100vh;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-xl) var(--ch-radius-xl) 0 0;
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-dialog);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}

.inbox-sheet-grip {
  align-self: center;
  width: 36px;
  height: 4px;
  margin-top: 8px;
  border-radius: 999px;
  background: var(--ch-color-border-strong, var(--ch-color-border));
}

.inbox-sheet-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 14px;
  color: var(--ch-color-text-strong);
}

.inbox-sheet-count {
  margin-right: auto;
  font-size: 12px;
  color: var(--ch-color-warning);
  font-variant-numeric: tabular-nums;
}

.inbox-sheet-close {
  min-height: 32px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--ch-radius-md);
  background: none;
  color: var(--ch-color-accent);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.inbox-sheet-close:focus-visible {
  outline: 2px solid var(--ch-color-accent);
}

.inbox-sheet-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

@media (max-width: 768px), (pointer: coarse) {
  .inbox-trigger {
    height: 34px;
    gap: 5px;
    padding: 0 8px;
  }
}
</style>
