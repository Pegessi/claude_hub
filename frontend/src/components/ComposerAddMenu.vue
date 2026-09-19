<template>
  <div
    ref="rootEl"
    class="composer-add-menu"
    @keydown="handleKeydown"
    @focusout="handleFocusOut"
  >
    <button
      ref="triggerEl"
      type="button"
      class="composer-add-trigger"
      aria-label="Add to chat"
      title="Add to chat"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="toggle"
      @keydown.down.stop.prevent="showMenu(false)"
      @keydown.up.stop.prevent="showMenu(true)"
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <path d="M12 5v14M5 12h14" />
      </svg>
    </button>
    <div
      v-if="open"
      ref="menuEl"
      class="composer-add-options"
      role="menu"
      aria-label="Add to chat"
    >
      <button
        type="button"
        role="menuitem"
        :aria-disabled="Boolean(attachmentDisabledReason)"
        :title="attachmentDisabledReason || 'Attach images (PNG, JPEG, GIF, WebP)'"
        @click="select('attachment')"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l8-8M7 13l7-7" />
        </svg>
        <span>Add attachment</span>
      </button>
      <button
        type="button"
        role="menuitem"
        title="Create a scheduled message in this Chat"
        @click="select('schedule')"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <circle
            cx="12"
            cy="12"
            r="8"
          />
          <path d="M12 8v5l3 2" />
        </svg>
        <span>Create scheduled task</span>
      </button>
      <button
        v-if="showGoal"
        type="button"
        role="menuitem"
        :aria-disabled="Boolean(goalDisabledReason)"
        :title="goalDisabledReason || 'Keep working toward an outcome'"
        @click="select('goal')"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M5 21V4c4-4 10 4 14 0v10c-4 4-10-4-14 0" />
        </svg>
        <span>Set a Goal</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps<{
  open: boolean
  showGoal: boolean
  attachmentDisabledReason?: string | null
  goalDisabledReason?: string | null
}>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  attachment: []
  goal: []
  schedule: []
}>()
const rootEl = ref<HTMLElement | null>(null)
const triggerEl = ref<HTMLButtonElement | null>(null)
const menuEl = ref<HTMLElement | null>(null)

function items() {
  return [...(menuEl.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])]
}

function showMenu(last = false) {
  emit('update:open', true)
  void nextTick(() => {
    const options = items()
    options[last ? options.length - 1 : 0]?.focus()
  })
}

function close(restoreFocus = false) {
  if (restoreFocus) triggerEl.value?.focus()
  emit('update:open', false)
}

function toggle() {
  if (props.open) close()
  else showMenu()
}

function select(action: 'attachment' | 'goal' | 'schedule') {
  if (action === 'attachment' && props.attachmentDisabledReason) return
  if (action === 'goal' && props.goalDisabledReason) return
  // Restore focus before opening the dialog so its return target survives
  // removal of the menu item from the DOM. Keep file picking synchronous.
  close(true)
  if (action === 'attachment') emit('attachment')
  else if (action === 'goal') emit('goal')
  else emit('schedule')
}

function handleKeydown(event: KeyboardEvent) {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    close(true)
  } else if (event.key === 'Tab') {
    // Let Tab leave from the stable trigger, not an item about to unmount.
    close(true)
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    const options = items()
    const current = options.findIndex(item => item === document.activeElement)
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
      : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length
    options[index]?.focus()
  }
}

function handleOutsidePointer(event: PointerEvent) {
  if (props.open && !rootEl.value?.contains(event.target as Node)) close()
}

function handleFocusOut(event: FocusEvent) {
  if (props.open && !rootEl.value?.contains(event.relatedTarget as Node | null)) close()
}

onMounted(() => document.addEventListener('pointerdown', handleOutsidePointer))
onUnmounted(() => document.removeEventListener('pointerdown', handleOutsidePointer))
</script>

<style scoped>
.composer-add-menu { position: relative; flex-shrink: 0; }
.composer-add-trigger { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border: 0; border-radius: var(--ch-radius-sm); color: var(--ch-color-text-muted); background: transparent; cursor: pointer; }
.composer-add-trigger:hover, .composer-add-trigger[aria-expanded='true'] { color: var(--ch-color-text); background: var(--ch-color-surface-control-hover); }
.composer-add-options { position: absolute; bottom: calc(100% + 8px); left: 0; z-index: 6; display: grid; gap: 2px; width: 200px; max-width: calc(100vw - 48px); padding: 5px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface); box-shadow: var(--ch-shadow-md); }
.composer-add-options button { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 0; border-radius: var(--ch-radius-sm); color: var(--ch-color-text); background: transparent; font: inherit; font-size: 13px; text-align: left; cursor: pointer; }
.composer-add-options svg { flex: 0 0 auto; color: var(--ch-color-text-muted); }
.composer-add-options button:hover, .composer-add-options button:focus-visible { background: var(--ch-color-surface-control-hover); }
.composer-add-options button[aria-disabled='true'] { opacity: .45; cursor: not-allowed; }
button:focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 1px; }
@media (max-width: 640px), (pointer: coarse) { .composer-add-trigger { width: 44px; height: 44px; } .composer-add-options button { min-height: 44px; } }
</style>
