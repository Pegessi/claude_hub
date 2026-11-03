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
      :aria-controls="open ? panelId : undefined"
      :aria-label="`Select workspace: ${selectedName}`"
      @click="toggle"
    >
      <span
        class="workspace-picker__value"
        :title="selectedName"
      >
        {{ selectedName }}
      </span>
      <span
        v-if="!disabled"
        class="workspace-picker__chevron"
        aria-hidden="true"
      >▾</span>
    </button>
    <div
      v-if="open"
      :id="panelId"
      ref="panelRef"
      class="workspace-picker__panel"
      role="menu"
      aria-label="Workspaces"
      @keydown="handlePanelKeydown"
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
          :ref="(element) => collectItemButton(element, item.id, 'select')"
          type="button"
          class="workspace-picker__option"
          role="menuitemradio"
          :aria-checked="item.id === selectedId"
          :tabindex="rovingIndex(item.id, 'select')"
          :disabled="deleting"
          :title="item.name"
          @click="selectItem(item)"
        >
          <span
            class="workspace-picker__check"
            aria-hidden="true"
          >{{ item.id === selectedId ? '✓' : '' }}</span>
          <span class="workspace-picker__name">{{ item.name }}</span>
        </button>
        <button
          :ref="(element) => collectItemButton(element, item.id, 'delete')"
          type="button"
          class="workspace-picker__delete"
          role="menuitem"
          :tabindex="rovingIndex(item.id, 'delete')"
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
  // True while a workspace switch or deletion is in flight.
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

// Stable panel id links the trigger's aria-controls to the open menu.
const panelId = 'workspace-picker-panel'

const open = ref(false)
const triggerRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)

const nativeButtonCtor: typeof HTMLButtonElement | null =
  typeof globalThis.HTMLButtonElement === 'function'
    ? globalThis.HTMLButtonElement
    : null

const selectedName = computed(() =>
  props.items.find(item => item.id === props.selectedId)?.name ?? 'Select workspace'
)

// Roving-tabindex order follows DOM order: each row's select button then its
// delete button. Only the focused menu item keeps tabIndex 0, so Tab leaves
// the menu while arrows move inside it (standard menu pattern).
interface ItemButton {
  key: string
  element: HTMLButtonElement
}
const itemButtons = ref<ItemButton[]>([])

function itemKey(id: string, kind: 'select' | 'delete') {
  return `${kind}:${id}`
}

function collectItemButton(
  element: Element | { $el?: Element } | null,
  id: string,
  kind: 'select' | 'delete',
) {
  // Vue function refs may receive a component instance when keyed onto one;
  // the rows host native buttons only, but keep the unwrap defensive. Guard
  // the constructor itself: node-based unit tests have no HTMLButtonElement.
  const button = (element as { $el?: Element } | null)?.$el ?? element
  const key = itemKey(id, kind)
  const index = itemButtons.value.findIndex(entry => entry.key === key)
  const isButton = nativeButtonCtor !== null && button instanceof nativeButtonCtor
  if (isButton) {
    if (index === -1) {
      itemButtons.value.push({ key, element: button as HTMLButtonElement })
    } else {
      itemButtons.value[index].element = button as HTMLButtonElement
    }
  } else if (index !== -1) {
    itemButtons.value.splice(index, 1)
  }
}

const focusedItemKey = ref<string | null>(null)

function orderedKeys(): string[] {
  return props.items.flatMap(item => [itemKey(item.id, 'select'), itemKey(item.id, 'delete')])
}

function initialFocusedKey(): string {
  const selected = props.selectedId
    ? itemKey(props.selectedId, 'select')
    : null
  const keys = orderedKeys()
  return selected && keys.includes(selected) ? selected : (keys[0] ?? '')
}

function rovingIndex(id: string, kind: 'select' | 'delete'): 0 | -1 {
  return focusedItemKey.value === itemKey(id, kind) ? 0 : -1
}

function focusItem(key: string) {
  focusedItemKey.value = key
  itemButtons.value.find(entry => entry.key === key)?.element.focus()
}

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
    // Set the roving anchor synchronously so tabindex is correct on the first
    // rendered frame; real DOM focus follows after the v-if panel mounts.
    focusedItemKey.value = initialFocusedKey()
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

// Menu keyboard contract: arrows move across rows and their delete actions,
// Home/End jump to the ends, Enter/Space activate natively, Escape closes.
function handlePanelKeydown(event: KeyboardEvent) {
  const keys = orderedKeys()
  if (keys.length === 0) return
  const currentIndex = keys.indexOf(focusedItemKey.value ?? '')
  let nextIndex = currentIndex
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    nextIndex = currentIndex === -1 ? 0 : (currentIndex + 1) % keys.length
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    nextIndex = currentIndex === -1
      ? keys.length - 1
      : (currentIndex - 1 + keys.length) % keys.length
  } else if (event.key === 'Home') {
    event.preventDefault()
    nextIndex = 0
  } else if (event.key === 'End') {
    event.preventDefault()
    nextIndex = keys.length - 1
  } else if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    open.value = false
    void focusTrigger()
    return
  } else {
    return
  }
  focusItem(keys[nextIndex])
}

// Listeners are bound only while the popover is open, mirroring the session
// actions menu: outside pointerdown dismisses without acting. Escape is owned
// by the panel keydown handler while focus is inside and by this document
// handler otherwise.
function handleDocumentPointerDown(event: PointerEvent) {
  if (!open.value) return
  const target = event.target
  if (!(target instanceof Node)) return
  if (triggerRef.value?.contains(target) || panelRef.value?.contains(target)) return
  open.value = false
}

function handleDocumentKeydown(event: KeyboardEvent) {
  if (!open.value || event.key !== 'Escape') return
  const panel = panelRef.value
  if (panel?.contains && panel.contains(document.activeElement)) return
  event.stopPropagation()
  open.value = false
  void focusTrigger()
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('pointerdown', handleDocumentPointerDown)
    document.addEventListener('keydown', handleDocumentKeydown)
    // Move real focus into the mounted menu row that toggle() anchored.
    void nextTick().then(() => {
      const key = focusedItemKey.value
      if (open.value && key) {
        itemButtons.value.find(entry => entry.key === key)?.element.focus()
      }
    })
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
