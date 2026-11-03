<template>
  <!--
    Workspace picker for the New Agent flow. Replaces the native <select>
    (which cannot host per-row actions): each row selects the workspace while
    the trailing delete button removes it through the existing workspace
    deletion flow owned by the parent. State has one owner — the parent's
    selectedWorkspaceId / handleWorkspaceChange — so this component only
    reports intent via select/delete events.
  -->
  <div class="workspace-picker">
    <button
      ref="triggerRef"
      type="button"
      class="workspace-picker__trigger"
      :disabled="disabled"
      aria-haspopup="menu"
      :aria-expanded="open"
      aria-label="Select workspace"
      @click="toggle"
    >
      <span
        class="workspace-picker__value"
        :title="selectedName"
      >
        {{ selectedName }}
      </span>
      <span
        class="workspace-picker__chevron"
        aria-hidden="true"
      >▾</span>
    </button>
    <div
      v-if="open"
      ref="panelRef"
      class="workspace-picker__panel"
      role="menu"
      aria-label="Workspaces"
    >
      <p
        v-if="items.length === 0"
        class="workspace-picker__empty"
      >
        No workspaces yet. Create one with New Workspace.
      </p>
      <div
        v-for="item in items"
        :key="item.id"
        class="workspace-picker__row"
      >
        <button
          type="button"
          class="workspace-picker__option"
          role="menuitemradio"
          :aria-checked="item.id === selectedId"
          :title="item.name"
          @click="selectItem(item)"
        >
          <span
            class="workspace-picker__check"
            aria-hidden="true"
          >{{ item.id === selectedId ? '✓' : '' }}</span>          <span class="workspace-picker__name">{{ item.name }}</span>
        </button>
        <button
          type="button"
          class="workspace-picker__delete"
          role="menuitem"
          :aria-label="deleteLabel(item.name)"
          :title="deleteLabel(item.name)"
          :disabled="deleting"
          @click.stop="deleteItem(item)"
        >
          <span aria-hidden="true">🗑</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  ref,
  watch,
} from 'vue'
import type { Workspace } from '@/types'

interface Props {
  items: Pick<Workspace, 'id' | 'name'>[]
  selectedId: string | null
  // True while a workspace switch is in flight.
  disabled?: boolean
  // True while a workspace deletion is in flight (single shared pending key).
  deleting?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  disabled: false,
  deleting: false,
})

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'delete', workspace: Pick<Workspace, 'id' | 'name'>): void
}>()

const open = ref(false)
const triggerRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)

const selectedName = computed(() =>
  props.items.find(item => item.id === props.selectedId)?.name ?? 'Select workspace'
)

function deleteLabel(name: string): string {
  return `Delete workspace "${name}"`
}

async function focusTrigger() {
  await nextTick()
  triggerRef.value?.focus?.()
}

function toggle() {
  if (open.value) {
    open.value = false
  } else if (!props.disabled) {
    open.value = true
  }
}

function selectItem(item: Pick<Workspace, 'id' | 'name'>) {
  open.value = false
  void focusTrigger()
  if (item.id !== props.selectedId) emit('select', item.id)
}

function deleteItem(item: Pick<Workspace, 'id' | 'name'>) {
  // Close before the blocking window.confirm so the popover never sits over
  // the native dialog; the parent owns confirmation and the DELETE request.
  open.value = false
  void focusTrigger()
  emit('delete', item)
}

// Listeners are bound only while the popover is open, mirroring the session
// actions menu: outside pointerdown or Escape dismisses without acting.
function handleDocumentPointerDown(event: PointerEvent) {
  if (!open.value) return
  const target = event.target
  if (!(target instanceof Node)) return
  if (triggerRef.value?.contains(target) || panelRef.value?.contains(target)) return
  open.value = false
}

function handleDocumentKeydown(event: KeyboardEvent) {
  if (!open.value || event.key !== 'Escape') return
  event.stopPropagation()
  open.value = false
  void focusTrigger()
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('pointerdown', handleDocumentPointerDown)
    document.addEventListener('keydown', handleDocumentKeydown)
  } else {
    document.removeEventListener('pointerdown', handleDocumentPointerDown)
    document.removeEventListener('keydown', handleDocumentKeydown)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  document.removeEventListener('keydown', handleDocumentKeydown)
})
</script>

<style scoped src="../styles/workspace-picker.css"></style>
