<template>
  <div
    v-if="isMobile"
    class="mobile-controls-overlay"
  >
    <Transition name="mobile-controls-panel">
      <div
        v-if="isExpanded"
        class="controls-panel"
      >
        <!-- Main row: Esc, Tab, arrows, Enter -->
        <div class="controls-row">
          <button
            type="button"
            class="control-btn"
            :class="{ pressed: pressedKeys.has('Escape') }"
            @pointerdown.prevent="handlePress('Escape')"
            @pointerup.prevent="handleRelease('Escape')"
            @pointercancel.prevent="handleRelease('Escape')"
            @pointerleave="handleRelease('Escape')"
          >
            Esc
          </button>
          <button
            type="button"
            class="control-btn"
            :class="{ pressed: pressedKeys.has('Tab') }"
            @pointerdown.prevent="handlePress('Tab')"
            @pointerup.prevent="handleRelease('Tab')"
            @pointercancel.prevent="handleRelease('Tab')"
            @pointerleave="handleRelease('Tab')"
          >
            Tab
          </button>
          <button
            type="button"
            class="control-btn control-btn-arrow"
            :class="{ pressed: pressedKeys.has('ArrowUp') }"
            @pointerdown.prevent="handlePress('ArrowUp')"
            @pointerup.prevent="handleRelease('ArrowUp')"
            @pointercancel.prevent="handleRelease('ArrowUp')"
            @pointerleave="handleRelease('ArrowUp')"
          >
            &#x2191;
          </button>
          <button
            type="button"
            class="control-btn control-btn-arrow"
            :class="{ pressed: pressedKeys.has('ArrowDown') }"
            @pointerdown.prevent="handlePress('ArrowDown')"
            @pointerup.prevent="handleRelease('ArrowDown')"
            @pointercancel.prevent="handleRelease('ArrowDown')"
            @pointerleave="handleRelease('ArrowDown')"
          >
            &#x2193;
          </button>
          <button
            type="button"
            class="control-btn control-btn-arrow"
            :class="{ pressed: pressedKeys.has('ArrowLeft') }"
            @pointerdown.prevent="handlePress('ArrowLeft')"
            @pointerup.prevent="handleRelease('ArrowLeft')"
            @pointercancel.prevent="handleRelease('ArrowLeft')"
            @pointerleave="handleRelease('ArrowLeft')"
          >
            &#x2190;
          </button>
          <button
            type="button"
            class="control-btn control-btn-arrow"
            :class="{ pressed: pressedKeys.has('ArrowRight') }"
            @pointerdown.prevent="handlePress('ArrowRight')"
            @pointerup.prevent="handleRelease('ArrowRight')"
            @pointercancel.prevent="handleRelease('ArrowRight')"
            @pointerleave="handleRelease('ArrowRight')"
          >
            &#x2192;
          </button>
          <button
            type="button"
            class="control-btn control-btn-enter"
            :class="{ pressed: pressedKeys.has('Enter') }"
            @pointerdown.prevent="handlePress('Enter')"
            @pointerup.prevent="handleRelease('Enter')"
            @pointercancel.prevent="handleRelease('Enter')"
            @pointerleave="handleRelease('Enter')"
          >
            Enter
          </button>
        </div>

        <!-- Modifier row: Ctrl, Shift -->
        <div class="controls-row controls-row-modifiers">
          <button
            type="button"
            class="control-btn control-btn-wide"
            :class="{ active: ctrlHeld, pressed: pressedKeys.has('ctrl') }"
            @pointerdown.prevent="toggleCtrl()"
          >
            Ctrl{{ ctrlHeld ? ' ON' : '' }}
          </button>
          <button
            type="button"
            class="control-btn control-btn-wide"
            :class="{ active: shiftHeld, pressed: pressedKeys.has('shift') }"
            @pointerdown.prevent="toggleShift()"
          >
            Shift{{ shiftHeld ? ' ON' : '' }}
          </button>
        </div>

        <!-- Shortcut row: common combos -->
        <div class="controls-row controls-row-shortcuts">
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-c') }"
            @pointerdown.prevent="handleShortcut('c')"
          >
            Ctrl+C
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-v') }"
            @pointerdown.prevent="handleShortcut('v')"
          >
            Ctrl+V
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-d') }"
            @pointerdown.prevent="handleShortcut('d')"
          >
            Ctrl+D
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-l') }"
            @pointerdown.prevent="handleShortcut('l')"
          >
            Ctrl+L
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-a') }"
            @pointerdown.prevent="handleShortcut('a')"
          >
            Ctrl+A
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('ctrl-e') }"
            @pointerdown.prevent="handleShortcut('e')"
          >
            Ctrl+E
          </button>
          <button
            type="button"
            class="control-btn control-btn-shortcut"
            :class="{ pressed: pressedKeys.has('shift-tab') }"
            @pointerdown.prevent="handleShiftTab()"
          >
            S-Tab
          </button>
        </div>
      </div>
    </Transition>
    <button
      type="button"
      class="toggle-btn"
      :class="{ expanded: isExpanded, pressed: pressedKeys.has('toggle') }"
      @pointerdown.prevent="pressedKeys.add('toggle')"
      @pointerup.prevent="pressedKeys.delete('toggle'); isExpanded = !isExpanded"
      @pointercancel.prevent="pressedKeys.delete('toggle')"
      @pointerleave="pressedKeys.delete('toggle')"
    >
      {{ isExpanded ? '&#x2715;' : '&#x2328;' }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, reactive } from 'vue'

type TerminalKeySender = (key: string, ctrl?: boolean, shift?: boolean) => void
type WindowWithTerminalKey = Window & { __sendTerminalKey?: TerminalKeySender }

const ARROW_KEYS = new Set(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'])
const REPEAT_START_DELAY_MS = 320
const REPEAT_INTERVAL_MS = 75

const isMobile = ref(false)
const isExpanded = ref(false)
const ctrlHeld = ref(false)
const shiftHeld = ref(false)
const pressedKeys = reactive(new Set<string>())
let repeatStartTimer: number | null = null
let repeatIntervalTimer: number | null = null
let repeatKey: string | null = null

function checkIsMobile() {
  isMobile.value = window.innerWidth <= 768 || /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent)
}

function sendTerminalKey(key: string) {
  const sender = (window as WindowWithTerminalKey).__sendTerminalKey
  if (sender && !['ctrl', 'shift', 'toggle'].includes(key)) {
    sender(key, ctrlHeld.value, shiftHeld.value)
  }
}

function clearArrowRepeat() {
  if (repeatStartTimer !== null) {
    window.clearTimeout(repeatStartTimer)
    repeatStartTimer = null
  }
  if (repeatIntervalTimer !== null) {
    window.clearInterval(repeatIntervalTimer)
    repeatIntervalTimer = null
  }
  repeatKey = null
}

function startArrowRepeat(key: string) {
  if (!ARROW_KEYS.has(key)) return
  clearArrowRepeat()
  repeatKey = key
  repeatStartTimer = window.setTimeout(() => {
    if (repeatKey !== key || !pressedKeys.has(key)) return
    sendTerminalKey(key)
    repeatIntervalTimer = window.setInterval(() => {
      if (repeatKey !== key || !pressedKeys.has(key)) {
        clearArrowRepeat()
        return
      }
      sendTerminalKey(key)
    }, REPEAT_INTERVAL_MS)
  }, REPEAT_START_DELAY_MS)
}

function handlePress(key: string) {
  if (pressedKeys.has(key)) return
  pressedKeys.add(key)
  sendTerminalKey(key)
  startArrowRepeat(key)
  // Auto-release Ctrl/Shift after sending a key with modifier
  if (ctrlHeld.value && !['ctrl', 'shift', 'toggle'].includes(key)) {
    ctrlHeld.value = false
  }
  if (shiftHeld.value && !['ctrl', 'shift', 'toggle'].includes(key)) {
    shiftHeld.value = false
  }
}

function handleRelease(key: string) {
  pressedKeys.delete(key)
  if (repeatKey === key) {
    clearArrowRepeat()
  }
}

// Shortcut button: send Ctrl+letter directly
function handleShortcut(letter: string) {
  const key = `ctrl-${letter}`
  pressedKeys.add(key)
  if ((window as any).__sendTerminalKey) {
    (window as any).__sendTerminalKey(letter, true, false)
  }
  // Visual feedback: remove pressed state after a short delay
  setTimeout(() => {
    pressedKeys.delete(key)
  }, 120)
}

// Shift+Tab shortcut
function handleShiftTab() {
  pressedKeys.add('shift-tab')
  if ((window as any).__sendTerminalKey) {
    (window as any).__sendTerminalKey('Tab', false, true)
  }
  setTimeout(() => {
    pressedKeys.delete('shift-tab')
  }, 120)
}

function toggleCtrl() {
  ctrlHeld.value = !ctrlHeld.value
}

function toggleShift() {
  shiftHeld.value = !shiftHeld.value
}

onMounted(() => {
  checkIsMobile()
  window.addEventListener('resize', checkIsMobile)
})

onUnmounted(() => {
  window.removeEventListener('resize', checkIsMobile)
  clearArrowRepeat()
})
</script>

<style scoped>
.mobile-controls-overlay {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  pointer-events: none;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  padding: 12px;
  padding-bottom: calc(12px + env(safe-area-inset-bottom, 0));
}

.controls-panel {
  pointer-events: auto;
  background-color: var(--ch-color-surface-glass);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 12px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  max-width: 420px;
  margin-bottom: 12px;
  box-shadow: var(--ch-shadow-soft);
}

.mobile-controls-panel-enter-active,
.mobile-controls-panel-leave-active {
  transition: opacity 140ms ease, transform 140ms ease;
  transform-origin: right bottom;
  will-change: opacity, transform;
}

.mobile-controls-panel-enter-from,
.mobile-controls-panel-leave-to {
  opacity: 0;
  transform: translateY(8px) scale(0.985);
}

.controls-row {
  display: flex;
  gap: 5px;
  justify-content: center;
}

.controls-row-modifiers {
  gap: 8px;
}

.controls-row-shortcuts {
  gap: 4px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  justify-content: flex-start;
  padding-bottom: 2px;
}

.control-btn {
  background-color: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
  border: 1px solid var(--ch-color-border-hover);
  border-radius: 8px;
  padding: 10px 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  min-width: 42px;
  text-align: center;
  transition: background-color 0.08s, transform 0.08s, box-shadow 0.08s, border-color 0.08s;
  touch-action: manipulation;
  flex-shrink: 0;
}

.control-btn:active,
.control-btn.pressed {
  background-color: var(--ch-color-border-hover);
  transform: scale(0.92);
  box-shadow: inset 0 2px 8px var(--ch-shadow-color-soft);
  border-color: var(--ch-color-text-subtle);
}

.control-btn.active {
  background-color: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent);
  color: var(--ch-color-text-inverse);
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.control-btn.active:active,
.control-btn.active.pressed {
  background-color: var(--ch-color-accent-hover);
  border-color: var(--ch-color-accent-strong);
}

.control-btn-arrow {
  font-size: 18px;
  min-width: 38px;
  padding: 10px 6px;
}

.control-btn-enter {
  background-color: var(--ch-color-success-strong);
  border-color: var(--ch-color-success-hover);
  color: var(--ch-color-text-inverse);
  min-width: 58px;
}

.control-btn-enter:active,
.control-btn-enter.pressed {
  background-color: var(--ch-color-success-hover);
  border-color: var(--ch-color-success-hover);
}

.control-btn-wide {
  flex: 1;
  max-width: 120px;
  font-size: 12px;
}

.control-btn-shortcut {
  background-color: var(--ch-color-accent-soft);
  border-color: var(--ch-color-border-hover);
  font-size: 11px;
  min-width: 50px;
  padding: 8px 6px;
  white-space: nowrap;
}

.control-btn-shortcut:active,
.control-btn-shortcut.pressed {
  background-color: var(--ch-color-border-hover);
  border-color: var(--ch-color-accent);
}

.toggle-btn {
  pointer-events: auto;
  background-color: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 50%;
  width: 56px;
  height: 56px;
  font-size: 24px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px var(--ch-shadow-color-soft);
  transition: background-color 0.1s, transform 0.1s, box-shadow 0.1s;
}

.toggle-btn:hover {
  background-color: var(--ch-color-surface-control-hover);
}

.toggle-btn:active,
.toggle-btn.pressed {
  transform: scale(0.88);
  box-shadow: 0 2px 6px var(--ch-shadow-color-soft);
  background-color: var(--ch-color-border-strong);
}

.toggle-btn.expanded {
  background-color: var(--ch-color-border-strong);
}

.toggle-btn.expanded:active,
.toggle-btn.expanded.pressed {
  background-color: var(--ch-color-border-hover);
}

@media (min-width: 769px) {
  .mobile-controls-overlay {
    display: none;
  }
}

:global(html[data-keyboard-open='true'] .mobile-controls-overlay) {
  bottom: 0;
  padding: 6px;
  padding-bottom: max(6px, env(safe-area-inset-bottom, 0));
}

:global(html[data-keyboard-open='true'] .controls-panel) {
  gap: 4px;
  max-width: none;
  margin-bottom: 6px;
  padding: 6px;
  border-radius: 8px;
}

:global(html[data-keyboard-open='true'] .controls-row) {
  gap: 4px;
}

:global(html[data-keyboard-open='true'] .control-btn) {
  min-width: 34px;
  padding: 7px 6px;
  border-radius: 6px;
  font-size: 12px;
}

:global(html[data-keyboard-open='true'] .control-btn-arrow) {
  min-width: 32px;
  padding: 6px 5px;
  font-size: 16px;
}

:global(html[data-keyboard-open='true'] .control-btn-enter) {
  min-width: 50px;
}

:global(html[data-keyboard-open='true'] .control-btn-shortcut) {
  min-width: 44px;
  padding: 6px 5px;
  font-size: 10px;
}

:global(html[data-keyboard-open='true'] .toggle-btn) {
  width: 42px;
  height: 42px;
  font-size: 20px;
}
</style>
