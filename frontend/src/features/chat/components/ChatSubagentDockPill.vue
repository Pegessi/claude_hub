<template>
  <div
    v-if="counts.total > 0"
    ref="rootRef"
    class="subagent-dock"
    :class="{ 'is-rail': railMode }"
    data-testid="subagent-dock"
    @keydown.esc="onEscape"
  >
    <button
      ref="triggerRef"
      type="button"
      class="subagent-dock-pill"
      :class="{ 'is-running': counts.running > 0, 'is-open': open || railMode }"
      data-testid="subagent-dock-pill"
      :aria-haspopup="railFits ? undefined : 'dialog'"
      :aria-expanded="railFits ? railMode : open"
      :aria-controls="railMode ? railId : (open ? revealId : undefined)"
      :aria-label="`Sub-agents: ${label}`"
      :title="railFits ? (railMode ? 'Collapse sub-agents' : 'Show sub-agents') : undefined"
      @click.stop="toggle"
    >
      <span
        class="subagent-dock-dot"
        aria-hidden="true"
      />
      <span class="subagent-dock-label">{{ label }}</span>
      <span
        v-if="railFits"
        class="subagent-dock-chevron"
        :class="{ 'is-open': railMode }"
        aria-hidden="true"
      />
    </button>

    <!-- Wide panes: the chat column leaves an empty right gutter, so the
         sub-agents rest there in full instead of hiding behind the pill. -->
    <section
      v-if="railMode"
      :id="railId"
      class="subagent-rail"
      data-testid="subagent-rail"
      aria-label="Sub-agents"
      :style="{ width: `${railWidth}px` }"
    >
      <ul class="subagent-rail-rows">
        <li
          v-for="chip in railChips"
          :key="chip.key"
          :class="{ 'is-history': chip.group === 'earlier' }"
        >
          <ChatSubagentRow
            :chip="chip"
            :now-ms="nowMs"
            @jump="emit('jump', $event)"
            @open="onNativeOpen"
          />
        </li>
      </ul>
      <button
        v-if="hiddenCount > 0 || showAll"
        type="button"
        class="subagent-rail-more"
        data-testid="subagent-rail-more"
        @click="showAll = !showAll"
      >
        {{ showAll ? 'Show less' : `${hiddenCount} more` }}
      </button>
    </section>

    <div
      v-if="open && !railFits && !isMobile"
      :id="popoverId"
      class="subagent-popover"
      data-testid="subagent-popover"
      role="dialog"
      aria-label="Sub-agents"
      @click.stop
    >
      <header class="subagent-popover-head">
        <strong>Sub-agents</strong>
        <span class="subagent-popover-count">{{ counts.total }}</span>
      </header>
      <div class="subagent-popover-body">
        <ChatSubagentList
          :chips="chips"
          :now-ms="nowMs"
          @jump="onJump"
          @open="onNativeOpen"
        />
      </div>
    </div>

    <!-- Mobile: the same pill lives in the pane chrome beside the session
         name, and reveals the list as a bottom sheet. -->
    <ChatSubagentSheet
      v-if="open && isMobile"
      :id="sheetId"
      :chips="chips"
      :now-ms="nowMs"
      @close="close(true)"
      @jump="onJump"
      @open="onNativeOpen"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useViewport } from '@/composables/useViewport'
import { subagentDockCounts, type SubagentDockRow, type SubagentJumpChip } from '../presentation/subagentJump'
import { useNativeSubagentDock } from '../controllers/nativeSubagentDock'
import ChatSubagentList from './ChatSubagentList.vue'
import ChatSubagentRow from './ChatSubagentRow.vue'
import ChatSubagentSheet from './ChatSubagentSheet.vue'

const props = defineProps<{
  chips: SubagentDockRow[]
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
}>()

/** Chat column max width (ChatTimeline) plus its side padding. */
const CHAT_COLUMN_PX = 900
const RAIL_MIN_PX = 230
const RAIL_MAX_PX = 300
const RAIL_LIMIT = 5
const COLLAPSED_KEY = 'claude-hub:subagent-rail-collapsed'

const uid = Math.random().toString(36).slice(2, 8)
const popoverId = `subagent-popover-${uid}`
const railId = `subagent-rail-${uid}`
const sheetId = `subagent-sheet-${uid}`
// One pill for every pane size; only the reveal differs (gutter rail or
// popover on desktop, bottom sheet on phones).
const { isMobile } = useViewport()
const nativeDock = useNativeSubagentDock()
const open = ref(false)
const showAll = ref(false)
const nowMs = ref(Date.now())
const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const paneWidth = ref(0)
const userCollapsed = ref(readCollapsed())

const counts = computed(() => subagentDockCounts(props.chips))
const revealId = computed(() => (isMobile.value ? sheetId : popoverId))
const label = computed(() =>
  counts.value.running > 0 ? `${counts.value.running} running` : `${counts.value.history} done`,
)

// Free space on each side of the centred chat column, minus a breathing gap.
// The rail hangs from the chrome's right edge (8px pane inset) and keeps a
// 16px gap from the column.
const gutter = computed(() => (paneWidth.value - CHAT_COLUMN_PX) / 2 - 24)
const railFits = computed(() => !isMobile.value && gutter.value >= RAIL_MIN_PX)
const railMode = computed(() => railFits.value && !userCollapsed.value)
const railWidth = computed(() => Math.min(RAIL_MAX_PX, Math.floor(gutter.value)))

// Active first (chips already put running rows in the "active" group), then
// the most recent history, capped so the rail never runs down the page.
const orderedChips = computed(() => [
  ...props.chips.filter(chip => chip.group === 'active'),
  ...props.chips.filter(chip => chip.group === 'earlier'),
])
const railChips = computed(() =>
  showAll.value ? orderedChips.value : orderedChips.value.slice(0, RAIL_LIMIT),
)
const hiddenCount = computed(() => orderedChips.value.length - railChips.value.length)

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

function setCollapsed(value: boolean) {
  userCollapsed.value = value
  try {
    localStorage.setItem(COLLAPSED_KEY, value ? '1' : '0')
  } catch {
    // Storage is optional; the choice still holds for this page view.
  }
}

function toggle() {
  nowMs.value = Date.now()
  if (railFits.value) {
    setCollapsed(!userCollapsed.value)
    return
  }
  if (open.value) close(false)
  else open.value = true
}

function close(restoreFocus: boolean) {
  if (!open.value) return
  open.value = false
  if (restoreFocus) void nextTick(() => triggerRef.value?.focus())
}

// Esc only belongs to the dock while its popover is open; otherwise (rail or
// closed pill) let it reach ancestor handlers such as stopping the turn.
function onEscape(event: KeyboardEvent) {
  if (!open.value) return
  event.stopPropagation()
  close(true)
}

function onJump(chip: SubagentJumpChip) {
  close(false)
  emit('jump', chip)
}

// A native child has no Chat tab: the pane's Chat body renders the detail
// window (it owns the timeline), so this chrome control only requests it.
// The window is rendered by the Chat body, so the dock owns focus restoration
// on dismissal: the row that opened it when still mounted, else this pill.
function onNativeOpen(chip: SubagentDockRow, source: HTMLElement | null) {
  close(false)
  nativeDock?.open(chip, source, triggerRef.value)
}

function onOutsidePointer(event: PointerEvent) {
  if (rootRef.value && !rootRef.value.contains(event.target as Node)) close(false)
}

// The reveal's own Esc handler only sees keys pressed while focus is inside it,
// and tapping static content (popover body, sheet header) moves focus to
// `body`. A document-level Escape keeps the promise that Esc closes the reveal
// wherever focus is — the same precedent as the other overlays.
function onDismissKey(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !open.value) return
  event.preventDefault()
  event.stopPropagation()
  close(true)
}

// Dismissal plumbing for whichever reveal is actually on screen: an
// outside-pointer only suits the anchored popover, Escape suits both.
function syncDismissListeners() {
  if (open.value && !isMobile.value) {
    document.addEventListener('pointerdown', onOutsidePointer, true)
  } else {
    document.removeEventListener('pointerdown', onOutsidePointer, true)
  }
  if (open.value) {
    document.addEventListener('keydown', onDismissKey, true)
  } else {
    document.removeEventListener('keydown', onDismissKey, true)
  }
}

watch(open, syncDismissListeners)

// A popover must not silently become a sheet (or the reverse) mid-view, so a
// breakpoint change dismisses the reveal and lets the user reopen it.
watch(isMobile, () => {
  if (open.value) close(true)
  syncDismissListeners()
})

// The pill disappears with the last sub-agent; never leave the popover open.
watch(() => counts.value.total, total => {
  if (total === 0) open.value = false
})

// Switching to the rail supersedes an open popover.
watch(railFits, fits => {
  if (fits) open.value = false
})

// Relative times in the always-visible rail should not freeze.
const clock = setInterval(() => {
  nowMs.value = Date.now()
}, 30_000)

// The dock unmounts with zero sub-agents, so observe the pane, not the dock.
let observer: ResizeObserver | null = null
let observedPane: Element | null = null

function observePane() {
  const pane = rootRef.value?.closest('.terminal-pane') ?? null
  if (!pane || pane === observedPane) return
  observer?.disconnect()
  observedPane = pane
  // clientWidth matches the ResizeObserver contentRect (no border) used below.
  paneWidth.value = pane.clientWidth
  observer = new ResizeObserver(entries => {
    for (const entry of entries) paneWidth.value = entry.contentRect.width
  })
  observer.observe(pane)
}

onMounted(observePane)
watch(rootRef, () => void nextTick(observePane))

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onOutsidePointer, true)
  document.removeEventListener('keydown', onDismissKey, true)
  observer?.disconnect()
  clearInterval(clock)
})
</script>

<style scoped>
/* A flex box, not a block: a block wrapper adds a line box around the
   inline-flex pill, making it taller and lower than its chrome siblings. */
.subagent-dock {
  position: relative;
  display: flex;
  align-items: center;
  flex: none;
}

/* In rail mode the rail anchors to the positioned pane chrome, so its right
   edge lines up with the chrome row rather than with the pill. */
.subagent-dock.is-rail {
  position: static;
}

/* Matches the 24px raised pill used by the session name beside it. */
.subagent-dock-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: var(--pane-chrome-control-size, 24px);
  padding: 0 9px;
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border-muted);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.5;
  white-space: nowrap;
  cursor: pointer;
  transition:
    color var(--ch-motion-fast),
    border-color var(--ch-motion-fast);
}

.subagent-dock-pill:hover,
.subagent-dock-pill.is-open {
  color: var(--ch-color-text-strong);
  border-color: var(--ch-color-border-strong);
}

.subagent-dock-pill:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 1px;
}

.subagent-dock-pill.is-running {
  color: var(--ch-color-text);
  border-color: var(--ch-color-accent-ring);
}

.subagent-dock-chevron {
  width: 5px;
  height: 5px;
  margin: -2px 0 0 1px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  opacity: 0.7;
  transform: rotate(45deg);
  transition: transform var(--ch-motion-fast);
}

.subagent-dock-chevron.is-open {
  margin-top: 2px;
  transform: rotate(-135deg);
}

.subagent-rail {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 4;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(70vh, 560px);
  padding: 4px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: color-mix(in srgb, var(--ch-color-surface-raised) 88%, transparent);
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 16px var(--ch-shadow-color-soft);
  overflow-y: auto;
  animation: subagent-rail-in var(--ch-motion-base, 160ms) ease-out;
}

.subagent-rail-rows {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 0;
  padding: 0;
  list-style: none;
}

/* The rail is narrow: the action label only appears on hover/focus. */
.subagent-rail :deep(.subagent-action) {
  display: none;
}

.subagent-rail :deep(.subagent-row:hover .subagent-action),
.subagent-rail :deep(.subagent-row:focus-visible .subagent-action) {
  display: inline;
}

.subagent-rail .is-history {
  opacity: 0.72;
  transition: opacity var(--ch-motion-fast);
}

.subagent-rail .is-history:hover,
.subagent-rail .is-history:focus-within {
  opacity: 1;
}

.subagent-rail-more {
  align-self: flex-start;
  margin: 2px 6px 3px;
  padding: 2px 4px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.subagent-rail-more:hover {
  color: var(--ch-color-text);
}

.subagent-rail-more:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
}

@keyframes subagent-rail-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}

.subagent-dock-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.is-running .subagent-dock-dot {
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 0 var(--ch-color-accent-ring);
  animation: subagent-dock-pulse 1.8s ease-out infinite;
}

@keyframes subagent-dock-pulse {
  70% {
    box-shadow: 0 0 0 5px transparent;
  }

  100% {
    box-shadow: 0 0 0 0 transparent;
  }
}

.subagent-popover {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 10;
  width: min(360px, 80vw);
  max-height: min(420px, 70vh);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-popover);
  overflow: hidden;
}

.subagent-popover-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 12px 7px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 12px;
  color: var(--ch-color-text-strong);
}

.subagent-popover-count {
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
}

.subagent-popover-body {
  overflow-y: auto;
  padding: 6px;
}

@media (prefers-reduced-motion: reduce) {
  .subagent-dock-pill {
    transition: none;
  }

  .is-running .subagent-dock-dot,
  .subagent-rail {
    animation: none;
  }
}
</style>
