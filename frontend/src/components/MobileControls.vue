<template>
  <div v-if="isMobile" class="mobile-controls-overlay">
    <div v-if="isExpanded" class="controls-panel">
      <!-- Main row: Esc, Tab, arrows, Enter -->
      <div class="controls-row">
        <button
          class="control-btn"
          :class="{ pressed: pressedKeys.has('Escape') }"
          @mousedown="handlePress('Escape')"
          @mouseup="handleRelease('Escape')"
          @mouseleave="handleRelease('Escape')"
          @touchstart.prevent="handlePress('Escape')"
          @touchend.prevent="handleRelease('Escape')"
        >Esc</button>
        <button
          class="control-btn"
          :class="{ pressed: pressedKeys.has('Tab') }"
          @mousedown="handlePress('Tab')"
          @mouseup="handleRelease('Tab')"
          @mouseleave="handleRelease('Tab')"
          @touchstart.prevent="handlePress('Tab')"
          @touchend.prevent="handleRelease('Tab')"
        >Tab</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowUp') }"
          @mousedown="handlePress('ArrowUp')"
          @mouseup="handleRelease('ArrowUp')"
          @mouseleave="handleRelease('ArrowUp')"
          @touchstart.prevent="handlePress('ArrowUp')"
          @touchend.prevent="handleRelease('ArrowUp')"
        >&#x2191;</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowDown') }"
          @mousedown="handlePress('ArrowDown')"
          @mouseup="handleRelease('ArrowDown')"
          @mouseleave="handleRelease('ArrowDown')"
          @touchstart.prevent="handlePress('ArrowDown')"
          @touchend.prevent="handleRelease('ArrowDown')"
        >&#x2193;</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowLeft') }"
          @mousedown="handlePress('ArrowLeft')"
          @mouseup="handleRelease('ArrowLeft')"
          @mouseleave="handleRelease('ArrowLeft')"
          @touchstart.prevent="handlePress('ArrowLeft')"
          @touchend.prevent="handleRelease('ArrowLeft')"
        >&#x2190;</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowRight') }"
          @mousedown="handlePress('ArrowRight')"
          @mouseup="handleRelease('ArrowRight')"
          @mouseleave="handleRelease('ArrowRight')"
          @touchstart.prevent="handlePress('ArrowRight')"
          @touchend.prevent="handleRelease('ArrowRight')"
        >&#x2192;</button>
        <button
          class="control-btn control-btn-enter"
          :class="{ pressed: pressedKeys.has('Enter') }"
          @mousedown="handlePress('Enter')"
          @mouseup="handleRelease('Enter')"
          @mouseleave="handleRelease('Enter')"
          @touchstart.prevent="handlePress('Enter')"
          @touchend.prevent="handleRelease('Enter')"
        >Enter</button>
      </div>

      <!-- Modifier row: Ctrl, Shift -->
      <div class="controls-row controls-row-modifiers">
        <button
          class="control-btn control-btn-wide"
          :class="{ active: ctrlHeld, pressed: pressedKeys.has('ctrl') }"
          @mousedown="toggleCtrl()"
          @touchstart.prevent="toggleCtrl()"
        >Ctrl{{ ctrlHeld ? ' ON' : '' }}</button>
        <button
          class="control-btn control-btn-wide"
          :class="{ active: shiftHeld, pressed: pressedKeys.has('shift') }"
          @mousedown="toggleShift()"
          @touchstart.prevent="toggleShift()"
        >Shift{{ shiftHeld ? ' ON' : '' }}</button>
      </div>

      <!-- Shortcut row: common combos -->
      <div class="controls-row controls-row-shortcuts">
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('ctrl-c') }"
          @mousedown="handleShortcut('c')"
          @touchstart.prevent="handleShortcut('c')"
        >Ctrl+C</button>
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('ctrl-d') }"
          @mousedown="handleShortcut('d')"
          @touchstart.prevent="handleShortcut('d')"
        >Ctrl+D</button>
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('ctrl-l') }"
          @mousedown="handleShortcut('l')"
          @touchstart.prevent="handleShortcut('l')"
        >Ctrl+L</button>
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('ctrl-a') }"
          @mousedown="handleShortcut('a')"
          @touchstart.prevent="handleShortcut('a')"
        >Ctrl+A</button>
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('ctrl-e') }"
          @mousedown="handleShortcut('e')"
          @touchstart.prevent="handleShortcut('e')"
        >Ctrl+E</button>
        <button
          class="control-btn control-btn-shortcut"
          :class="{ pressed: pressedKeys.has('shift-tab') }"
          @mousedown="handleShiftTab()"
          @touchstart.prevent="handleShiftTab()"
        >S-Tab</button>
      </div>
    </div>
    <button
      class="toggle-btn"
      :class="{ expanded: isExpanded, pressed: pressedKeys.has('toggle') }"
      @mousedown="pressedKeys.add('toggle')"
      @mouseup="pressedKeys.delete('toggle'); isExpanded = !isExpanded"
      @mouseleave="pressedKeys.delete('toggle')"
      @touchstart.prevent="pressedKeys.add('toggle')"
      @touchend.prevent="pressedKeys.delete('toggle'); isExpanded = !isExpanded"
    >
      {{ isExpanded ? '&#x2715;' : '&#x2328;' }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, reactive } from 'vue'

const isMobile = ref(false)
const isExpanded = ref(false)
const ctrlHeld = ref(false)
const shiftHeld = ref(false)
const pressedKeys = reactive(new Set<string>())

function checkIsMobile() {
  isMobile.value = window.innerWidth <= 768 || /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent)
}

function handlePress(key: string) {
  pressedKeys.add(key)
  if ((window as any).__sendTerminalKey && !['ctrl', 'shift', 'toggle'].includes(key)) {
    (window as any).__sendTerminalKey(key, ctrlHeld.value, shiftHeld.value)
  }
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
  background-color: rgba(30, 30, 30, 0.95);
  border: 1px solid #444;
  border-radius: 12px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  max-width: 420px;
  margin-bottom: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
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
  background-color: #3c3c3c;
  color: #fff;
  border: 1px solid #555;
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
  background-color: #555;
  transform: scale(0.92);
  box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.4);
  border-color: #666;
}

.control-btn.active {
  background-color: #3b82f6;
  border-color: #60a5fa;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.3);
}

.control-btn.active:active,
.control-btn.active.pressed {
  background-color: #2563eb;
  border-color: #3b82f6;
}

.control-btn-arrow {
  font-size: 18px;
  min-width: 38px;
  padding: 10px 6px;
}

.control-btn-enter {
  background-color: #22c55e;
  border-color: #16a34a;
  min-width: 58px;
}

.control-btn-enter:active,
.control-btn-enter.pressed {
  background-color: #16a34a;
  border-color: #15803d;
}

.control-btn-wide {
  flex: 1;
  max-width: 120px;
  font-size: 12px;
}

.control-btn-shortcut {
  background-color: #2d2d5e;
  border-color: #4a4a8a;
  font-size: 11px;
  min-width: 50px;
  padding: 8px 6px;
  white-space: nowrap;
}

.control-btn-shortcut:active,
.control-btn-shortcut.pressed {
  background-color: #4a4a8a;
  border-color: #6a6aaa;
}

.toggle-btn {
  pointer-events: auto;
  background-color: #2d2d2d;
  color: #fff;
  border: 1px solid #444;
  border-radius: 50%;
  width: 56px;
  height: 56px;
  font-size: 24px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  transition: background-color 0.1s, transform 0.1s, box-shadow 0.1s;
}

.toggle-btn:hover {
  background-color: #3c3c3c;
}

.toggle-btn:active,
.toggle-btn.pressed {
  transform: scale(0.88);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
  background-color: #444;
}

.toggle-btn.expanded {
  background-color: #444;
}

.toggle-btn.expanded:active,
.toggle-btn.expanded.pressed {
  background-color: #555;
}

@media (min-width: 769px) {
  .mobile-controls-overlay {
    display: none;
  }
}
</style>
