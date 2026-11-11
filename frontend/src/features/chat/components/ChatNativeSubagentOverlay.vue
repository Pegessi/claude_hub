<template>
  <Teleport to="body">
    <div
      v-if="row"
      class="native-subagent-backdrop"
      data-testid="native-subagent-overlay"
      @click.self="close"
    >
      <section
        ref="windowRef"
        class="native-subagent-window"
        role="dialog"
        aria-modal="true"
        :aria-label="`Sub-agent ${row.name}`"
      >
        <header class="native-subagent-head">
          <span class="native-subagent-badge">{{ row.kind === 'hub' ? 'Hub sub-agent' : 'Native sub-agent' }}</span>
          <strong
            class="native-subagent-name"
            :title="displayName"
          >{{ displayName }}</strong>
          <span
            v-if="row.agentType !== 'Native sub-agent'"
            class="native-subagent-type"
          >{{ row.agentType }}</span>
          <span
            class="tool-status"
            :class="liveState"
          >{{ liveStateLabel }}</span>
          <button
            ref="closeRef"
            type="button"
            class="native-subagent-close"
            data-testid="native-subagent-close"
            aria-label="Close sub-agent details"
            @click="close"
          >
            Done
          </button>
        </header>

        <p class="native-subagent-meta">
          <span v-if="row.kind === 'native' && lookup">Turn #{{ lookup.ordinal }}</span>
          <span v-else-if="row.kind === 'hub' && lookup">{{ lookup.ordinal }} {{ lookup.ordinal === 1 ? 'turn' : 'turns' }}</span>
          <span
            class="native-subagent-thread"
            :title="row.kind === 'hub' ? row.subagentId : row.threadId"
          >{{ row.kind === 'hub' ? row.shortId : row.threadId }}</span>
        </p>

        <div class="native-subagent-body">
          <ChatSubthreadBody
            v-if="lookup"
            :turn="lookup.turn"
            :thread="lookup.thread"
          />
          <p
            v-else
            class="native-subagent-missing"
          >
            This sub-agent is no longer part of the conversation.
          </p>
        </div>

        <footer class="native-subagent-foot">
          <button
            v-if="row.kind === 'native' && lookup"
            type="button"
            class="native-subagent-locate"
            @click="locate"
          >
            Show in transcript
          </button>
          <button
            v-else-if="liveHubChip && liveHubChip.canJump"
            type="button"
            class="native-subagent-locate"
            data-testid="hub-subagent-open-session"
            @click="openHubSession"
          >
            {{ liveHubChip.archived ? 'Restore & open' : 'Open session' }}
          </button>
          <button
            type="button"
            class="native-subagent-done"
            @click="close"
          >
            Close
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'

import { API_BASE } from '@/stores/terminal/helpers'
import type { AgentStreamEvent } from '@/types'
import { requireChatOwner, chatTimelineKey } from '../owners'
import { useNativeSubagentDock } from '../controllers/nativeSubagentDock'
import { useSubagentDock } from '../controllers/subagentDock'
import { buildHubSubagentLookup } from '../presentation/hubSubagentThread'
import { findNativeSubthread } from '../presentation/nativeSubagentRows'
import { subagentDisplayName } from '../presentation/subagentJump'
import { subthreadStatusClass, subthreadStatusLabel } from '../presentation/chatSubagentPresentation'
import ChatSubthreadBody from './ChatSubthreadBody.vue'

/**
 * The dock's unified detail window for both provider-native and Hub sub-agents.
 *
 * Native children render directly from the parent turn's timeline (`ChatSubthreadBody`)
 * and offer `Show in transcript`. Hub children (`claude-hub agent run`) fetch
 * their compacted stream events on demand, project into the same `ChatSubthreadBody`,
 * and offer an optional `Open session` / `Restore & open` action to switch to
 * the child Chat tab.
 */
const dock = useNativeSubagentDock()
const hubDock = useSubagentDock()
const { turns, revealSubthread, ensureSubthreadDetailsLoaded } = requireChatOwner(chatTimelineKey)

const row = computed(() => dock?.openRow.value ?? null)
const liveHubChip = computed(() => {
  const current = row.value
  if (!current || current.kind !== 'hub') return null
  return hubDock?.chips.value.find(chip => chip.subagentId === current.subagentId) ?? current
})
const displayName = computed(() => {
  const current = row.value
  if (!current) return ''
  return current.kind === 'hub' ? subagentDisplayName(current.name) : current.name
})

const hubEvents = shallowRef<AgentStreamEvent[]>([])
let hubFetchToken = 0
let hubPollTimer: ReturnType<typeof setInterval> | null = null

function stopHubPolling() {
  if (hubPollTimer !== null) {
    clearInterval(hubPollTimer)
    hubPollTimer = null
  }
}

async function fetchHubEvents(tabId: string, token: number) {
  try {
    const response = await fetch(
      `${API_BASE}/workspaces/tabs/${encodeURIComponent(tabId)}/stream/events?since_sequence=-1&limit=2000&compact=true&persisted_only=true`,
    )
    if (!response.ok) return
    const page = (await response.json()) as { events?: AgentStreamEvent[] }
    if (token === hubFetchToken && Array.isArray(page.events)) {
      hubEvents.value = page.events
    }
  } catch {
    // Keep the summary fallback when the child stream cannot be read.
  }
}

watch(
  row,
  current => {
    stopHubPolling()
    hubFetchToken += 1
    hubEvents.value = []
    if (!current) return
    if (current.kind === 'native') {
      ensureSubthreadDetailsLoaded?.(current.turnKey, current.threadId)
      return
    }
    if (current.tabId) {
      const token = hubFetchToken
      const tabId = current.tabId
      void fetchHubEvents(tabId, token)
      if (current.state === 'running') {
        hubPollTimer = setInterval(() => void fetchHubEvents(tabId, token), 2500)
      }
    }
  },
  { immediate: true },
)

watch(
  () => liveHubChip.value?.state,
  (state, previous) => {
    const current = liveHubChip.value
    if (!current?.tabId) return
    if (state !== 'running') stopHubPolling()
    if (previous && state !== previous) {
      void fetchHubEvents(current.tabId, hubFetchToken)
    }
  },
)

const lookup = computed(() => {
  const current = row.value
  if (!current) return null
  if (current.kind === 'hub') {
    return liveHubChip.value ? buildHubSubagentLookup(liveHubChip.value, hubEvents.value) : null
  }
  return findNativeSubthread(turns.value, current.turnKey, current.threadId)
})

// Status is read from the live child, never from the row snapshot taken when
// the window opened, so a running child finishes inside the open window.
const liveState = computed(() => (lookup.value
  ? subthreadStatusClass(lookup.value.thread)
  : row.value?.state))
const liveStateLabel = computed(() => {
  if (liveHubChip.value) return liveHubChip.value.stateLabel
  return lookup.value
    ? subthreadStatusLabel(lookup.value.thread)
    : row.value?.stateLabel ?? ''
})

const windowRef = ref<HTMLElement | null>(null)
const closeRef = ref<HTMLButtonElement | null>(null)
let previousBodyOverflow = ''

function close() {
  dock?.close()
}

/** Hand the reader to the inline region: expand its turn/process and scroll. */
function locate() {
  const target = row.value
  if (!target || target.kind !== 'native') return
  void revealSubthread(target.turnKey, target.threadId)
  close()
}

function openHubSession() {
  const target = liveHubChip.value
  if (!target) return
  close()
  void hubDock?.jump(target)
}

/** Whether another modal surface (the image/attachment lightbox is a real
 *  `<dialog>` in the top layer) owns the keyboard right now. */
function topLayerOwnsKeys(): boolean {
  return typeof document !== 'undefined' && document.querySelector('dialog[open]') !== null
}

// A tap on static content moves focus to <body>, so a handler bound to the
// window would stop seeing keys; the document-level one keeps both promises:
// Escape closes from anywhere, and Tab cannot leave the dialog even when focus
// started outside it. A lightbox opened from inside the window is a top-layer
// `<dialog>` and keeps its own Escape and Tab.
function onDocumentKey(event: KeyboardEvent) {
  if (!row.value || topLayerOwnsKeys()) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    close()
    return
  }
  if (event.key !== 'Tab') return
  const root = windowRef.value
  if (!root) return
  // The body is prose and tool cards: links, <details> summaries and anything
  // with an explicit tabindex are focus stops too, not just the footer buttons.
  const focusables = [...root.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
  )].filter(element => element.getClientRects().length > 0)
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement
  if (!root.contains(active)) {
    // Focus escaped (a tap on static text): put it back on an edge so the
    // next Tab keeps walking inside the dialog.
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus()
    return
  }
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

function syncBodyLock(open: boolean) {
  // Leaf harnesses mount this subtree with a document that has no body; the
  // lock is only meaningful in a real page.
  if (typeof document === 'undefined' || !document.body) return
  if (open) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    void nextTick(() => closeRef.value?.focus())
    return
  }
  document.body.style.overflow = previousBodyOverflow
}

watch(() => Boolean(row.value), syncBodyLock, { immediate: true })

if (typeof document?.addEventListener === 'function') {
  document.addEventListener('keydown', onDocumentKey, true)
}

onBeforeUnmount(() => {
  stopHubPolling()
  if (dock?.openRow.value) dock.openRow.value = null
  if (typeof document?.removeEventListener === 'function') {
    document.removeEventListener('keydown', onDocumentKey, true)
  }
  if (document?.body) document.body.style.overflow = previousBodyOverflow
})
</script>
<style scoped>
.native-subagent-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
  background: var(--ch-color-backdrop, var(--ch-color-overlay));
  backdrop-filter: blur(var(--ch-backdrop-blur, 0));
}

/* A gutter panel on desktop: it reads as the rail's own window rather than a
   modal over the conversation. Phones get the app's bottom-sheet shape. */
.native-subagent-window {
  width: min(560px, 100%);
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-left: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-dialog);
  animation: native-subagent-in var(--ch-motion-base, 160ms) ease-out;
}

@keyframes native-subagent-in {
  from {
    transform: translateX(16px);
    opacity: 0;
  }
}

.native-subagent-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px 9px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 13px;
  color: var(--ch-color-text-strong);
}

.native-subagent-badge {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.native-subagent-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.native-subagent-type {
  flex: none;
  font-size: 10px;
  color: var(--ch-color-text-subtle);
}

.native-subagent-close {
  margin-left: auto;
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

.native-subagent-close:focus-visible,
.native-subagent-locate:focus-visible,
.native-subagent-done:focus-visible {
  outline: 2px solid var(--ch-color-accent);
}

.native-subagent-meta {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0;
  padding: 8px 14px 0;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
}

.native-subagent-thread {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.native-subagent-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 14px 12px;
}

.native-subagent-missing {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.native-subagent-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px max(10px, env(safe-area-inset-bottom, 0px));
  border-top: 1px solid var(--ch-color-border-muted);
}

.native-subagent-locate,
.native-subagent-done {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: none;
  color: var(--ch-color-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.native-subagent-done {
  margin-left: auto;
  color: var(--ch-color-accent);
  border-color: transparent;
}

.tool-status {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  text-transform: uppercase;
}

.tool-status.completed {
  background-color: var(--ch-color-success-bg, rgba(46, 160, 67, 0.12));
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.failed {
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.running {
  background-color: var(--ch-color-warning-bg, rgba(224, 168, 0, 0.12));
  color: var(--ch-color-warning, #e0a800);
}

.tool-status.cancelled {
  background-color: var(--ch-color-surface-muted, rgba(139, 148, 158, 0.12));
  color: var(--ch-color-text-muted, #8b949e);
}

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
}

@media (max-width: 640px) {
  .native-subagent-backdrop {
    align-items: flex-end;
  }

  /* The name is the one thing worth reading here: let the row wrap instead of
     crushing it to an ellipsis behind four fixed siblings. */
  .native-subagent-head {
    flex-wrap: wrap;
  }

  .native-subagent-name {
    flex: 1 1 100%;
    order: 1;
  }

  .native-subagent-window {
    width: 100%;
    height: auto;
    max-height: 78vh;
    border-left: 0;
    border-top: 1px solid var(--ch-color-border-muted);
    border-radius: var(--ch-radius-xl) var(--ch-radius-xl) 0 0;
    animation: native-subagent-sheet-in var(--ch-motion-standard) ease-out;
  }

  @keyframes native-subagent-sheet-in {
    from {
      transform: translateY(24px);
      opacity: 0;
    }
  }
}

@media (prefers-reduced-motion: reduce) {
  .native-subagent-window {
    animation: none;
  }
}
</style>
