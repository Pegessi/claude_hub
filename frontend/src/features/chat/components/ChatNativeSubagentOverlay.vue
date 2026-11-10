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
        @keydown.tab="onTab"
      >
        <header class="native-subagent-head">
          <span class="native-subagent-badge">Native sub-agent</span>
          <strong class="native-subagent-name">{{ row.name }}</strong>
          <span class="native-subagent-type">{{ row.agentType }}</span>
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
          <span v-if="lookup">Turn #{{ lookup.ordinal }}</span>
          <span
            class="native-subagent-thread"
            :title="row.threadId"
          >{{ row.threadId }}</span>
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
            v-if="lookup"
            type="button"
            class="native-subagent-locate"
            @click="locate"
          >
            Show in transcript
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
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { requireChatOwner, chatTimelineKey } from '../owners'
import { useNativeSubagentDock } from '../controllers/nativeSubagentDock'
import { findNativeSubthread } from '../presentation/nativeSubagentRows'
import { subthreadStatusClass, subthreadStatusLabel } from '../presentation/chatSubagentPresentation'
import ChatSubthreadBody from './ChatSubthreadBody.vue'

/**
 * The dock's detail window for a provider-native child.
 *
 * A native child is not a Chat tab, so "open" means read: this renders the
 * exact rows the inline region shows (`ChatSubthreadBody`), and offers the
 * transcript as the place to continue (the pane keeps its inline region too).
 *
 * It is mounted by `StructuredPane` because that subtree owns the timeline and
 * image owners the body requires, while the pill that opens it lives in the
 * pane chrome; the shared `NativeSubagentDock` state connects the two.
 */
const dock = useNativeSubagentDock()
const { turns, revealSubthread } = requireChatOwner(chatTimelineKey)

const row = computed(() => dock?.openRow.value ?? null)
const lookup = computed(() => (row.value
  ? findNativeSubthread(turns.value, row.value.turnKey, row.value.threadId)
  : null))

// Status is read from the live child, never from the row snapshot taken when
// the window opened, so a running child finishes inside the open window.
const liveState = computed(() => (lookup.value
  ? subthreadStatusClass(lookup.value.thread)
  : row.value?.state))
const liveStateLabel = computed(() => (lookup.value
  ? subthreadStatusLabel(lookup.value.thread)
  : row.value?.stateLabel ?? ''))

const windowRef = ref<HTMLElement | null>(null)
const closeRef = ref<HTMLButtonElement | null>(null)
let previousBodyOverflow = ''

function close() {
  dock?.close()
}

/** Hand the reader to the inline region: expand its turn/process and scroll. */
function locate() {
  const target = row.value
  if (!target) return
  void revealSubthread(target.turnKey, target.threadId)
  close()
}

// Keep Tab inside the modal window; the backdrop covers the transcript.
function onTab(event: KeyboardEvent) {
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
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

// A tap on static content moves focus to <body>, so the window's own Esc
// handler stops seeing keys; the document-level one keeps the promise.
function onDismissKey(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !row.value) return
  event.preventDefault()
  event.stopPropagation()
  close()
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
  document.addEventListener('keydown', onDismissKey, true)
}

onBeforeUnmount(() => {
  if (typeof document?.removeEventListener === 'function') {
    document.removeEventListener('keydown', onDismissKey, true)
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

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
}

@media (max-width: 640px) {
  .native-subagent-backdrop {
    align-items: flex-end;
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
