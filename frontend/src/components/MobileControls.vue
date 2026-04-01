<template>
  <div v-if="isMobile" class="mobile-controls-overlay">
    <div v-if="isExpanded" class="controls-panel">
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
          class="control-btn control-btn-enter"
          :class="{ pressed: pressedKeys.has('Enter') }"
          @mousedown="handlePress('Enter')"
          @mouseup="handleRelease('Enter')"
          @mouseleave="handleRelease('Enter')"
          @touchstart.prevent="handlePress('Enter')"
          @touchend.prevent="handleRelease('Enter')"
        >Enter</button>
        <button
          class="control-btn"
          :class="{ active: ctrlHeld, pressed: pressedKeys.has('ctrl') }"
          @mousedown="toggleCtrl()"
          @touchstart.prevent="toggleCtrl()"
        >Ctrl</button>
        <button
          class="control-btn"
          :class="{ active: shiftHeld, pressed: pressedKeys.has('shift') }"
          @mousedown="toggleShift()"
          @touchstart.prevent="toggleShift()"
        >Shift</button>
      </div>
      <div class="controls-row">
        <button
          class="control-btn"
          :class="{ pressed: pressedKeys.has('PageUp') }"
          @mousedown="handlePress('PageUp')"
          @mouseup="handleRelease('PageUp')"
          @mouseleave="handleRelease('PageUp')"
          @touchstart.prevent="handlePress('PageUp')"
          @touchend.prevent="handleRelease('PageUp')"
        >PgUp</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowUp') }"
          @mousedown="handlePress('ArrowUp')"
          @mouseup="handleRelease('ArrowUp')"
          @mouseleave="handleRelease('ArrowUp')"
          @touchstart.prevent="handlePress('ArrowUp')"
          @touchend.prevent="handleRelease('ArrowUp')"
        >↑</button>
        <button
          class="control-btn"
          :class="{ pressed: pressedKeys.has('PageDown') }"
          @mousedown="handlePress('PageDown')"
          @mouseup="handleRelease('PageDown')"
          @mouseleave="handleRelease('PageDown')"
          @touchstart.prevent="handlePress('PageDown')"
          @touchend.prevent="handleRelease('PageDown')"
        >PgDn</button>
      </div>
      <div class="controls-row">
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowLeft') }"
          @mousedown="handlePress('ArrowLeft')"
          @mouseup="handleRelease('ArrowLeft')"
          @mouseleave="handleRelease('ArrowLeft')"
          @touchstart.prevent="handlePress('ArrowLeft')"
          @touchend.prevent="handleRelease('ArrowLeft')"
        >←</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowDown') }"
          @mousedown="handlePress('ArrowDown')"
          @mouseup="handleRelease('ArrowDown')"
          @mouseleave="handleRelease('ArrowDown')"
          @touchstart.prevent="handlePress('ArrowDown')"
          @touchend.prevent="handleRelease('ArrowDown')"
        >↓</button>
        <button
          class="control-btn control-btn-arrow"
          :class="{ pressed: pressedKeys.has('ArrowRight') }"
          @mousedown="handlePress('ArrowRight')"
          @mouseup="handleRelease('ArrowRight')"
          @mouseleave="handleRelease('ArrowRight')"
          @touchstart.prevent="handlePress('ArrowRight')"
          @touchend.prevent="handleRelease('ArrowRight')"
        >→</button>
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
      {{ isExpanded ? '✕' : '⌨' }}
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
}

function handleRelease(key: string) {
  pressedKeys.delete(key)
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
  gap: 8px;
  width: 100%;
  max-width: 380px;
  margin-bottom: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}

.controls-row {
  display: flex;
  gap: 8px;
  justify-content: center;
}

.control-btn {
  background-color: #3c3c3c;
  color: #fff;
  border: 1px solid #555;
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  min-width: 70px;
  text-align: center;
  transition: background-color 0.08s, transform 0.08s, box-shadow 0.08s, border-color 0.08s;
  touch-action: manipulation;
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
  font-size: 20px;
  min-width: 55px;
  padding: 12px;
}

.control-btn-enter {
  background-color: #22c55e;
  border-color: #16a34a;
  min-width: 80px;
}

.control-btn-enter:active,
.control-btn-enter.pressed {
  background-color: #16a34a;
  border-color: #15803d;
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
