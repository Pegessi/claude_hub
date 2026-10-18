<template>
  <div
    class="chat-sidebar__item"
    :class="{ active: active, dragging: dragging }"
    :data-runtime-status="status"
    :data-tab-id="tab.id"
    :data-drop-position="dropPosition"
    @pointerdown="emit('row-pointerdown', $event, tab)"
    @click="emit('row-click', $event, tab)"
  >
    <ChatSidebarWorkingEffect
      v-if="status === 'working'"
      :seed="tab.id"
    />
    <button
      v-if="!renaming"
      type="button"
      class="chat-sidebar__item-main"
      :aria-current="active ? 'page' : undefined"
      :title="`${tab.name || 'Untitled'} — ${tab.cwd || 'No directory'}. Double-click to rename; drag to reorder`"
      @click.stop="emit('row-click', $event, tab)"
      @dblclick.stop="emit('rename', tab)"
    >
      <span
        class="chat-sidebar__item-status"
        :data-status="status"
        :data-unread="tab.is_unread ? 'true' : 'false'"
        role="img"
        :aria-label="statusLabel"
        :title="statusLabel"
      />
      <span class="chat-sidebar__item-name">{{ tab.name || 'Untitled' }}</span>
      <span class="chat-sidebar__item-time">{{ relativeTime(tab.created_at) }}</span>
    </button>
    <input
      v-else
      :ref="setRenameInputRef"
      v-model="renameName"
      type="text"
      class="chat-sidebar__rename-input"
      aria-label="Rename session"
      @blur="emit('save-rename', tab.id)"
      @keyup.enter="emit('save-rename', tab.id)"
      @keyup.escape="emit('cancel-rename', tab.id)"
    >
    <!-- Same ⋯ actions as the TabBar. Reordering stays on the row's
         direct pointer-drag interaction instead of menu commands. -->
    <TabActionsMenu
      class="chat-sidebar__item-menu"
      :tab="tab"
      variant="sidebar"
      @rename="emit('rename', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import TabActionsMenu from '@/components/TabActionsMenu.vue'
import ChatSidebarWorkingEffect from '@/components/ChatSidebarWorkingEffect.vue'
import { relativeTime } from '@/utils/time'
import type { TerminalTab } from '@/types'

defineProps<{
  tab: TerminalTab
  active: boolean
  dragging: boolean
  dropPosition?: 'before' | 'after'
  status: string
  statusLabel: string
  renaming: boolean
  setRenameInputRef: (element: unknown) => void
}>()
const renameName = defineModel<string>('renameName', { required: true })
const emit = defineEmits<{
  'row-pointerdown': [event: PointerEvent, tab: TerminalTab]
  'row-click': [event: MouseEvent, tab: TerminalTab]
  rename: [tab: TerminalTab]
  'save-rename': [tabId: string]
  'cancel-rename': [tabId: string]
}>()
</script>

<style scoped>
/* Chat rows: a plain container; the clickable surface is the inner main
   button so the ⋯ menu can sit beside it as a valid sibling control. */
.chat-sidebar__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 7px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  transition:
    background var(--ch-motion-fast),
    border-color var(--ch-motion-fast),
    box-shadow var(--ch-motion-fast),
    color var(--ch-motion-fast);
  cursor: grab;
  touch-action: pan-y;
}

.chat-sidebar__item:hover {
  background: var(--ch-color-row-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__item.active {
  background: var(--ch-color-surface-selected);
  border-color: var(--ch-color-border-selected);
  color: var(--ch-color-text-selected);
  box-shadow:
    0 0 0 1px var(--ch-color-accent-halo),
    0 0 18px -7px var(--ch-color-session-glow);
}

/* Like Maso, a working row is marked only by the light pulse travelling its
   border (ChatSidebarWorkingEffect), so a selected working row drops its
   outline glow and keeps just a faint border beside the selected fill. */
.chat-sidebar__item.active[data-runtime-status='working'] {
  border-color: color-mix(in srgb, var(--ch-color-border-selected) 20%, transparent);
  box-shadow: none;
}

.chat-sidebar__item[data-runtime-status='working'] > .chat-sidebar__item-main,
.chat-sidebar__item[data-runtime-status='working'] > .chat-sidebar__rename-input {
  position: relative;
  z-index: 1;
}

.chat-sidebar__item.dragging {
  opacity: 0.45;
  cursor: grabbing;
}

:global(body.chat-sidebar-dragging) {
  cursor: grabbing !important;
  user-select: none;
}

.chat-sidebar__item[data-drop-position]::before {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  background: var(--ch-color-accent);
  pointer-events: none;
}

.chat-sidebar__item[data-drop-position='before']::before {
  top: -1px;
}

.chat-sidebar__item[data-drop-position='after']::before {
  bottom: -1px;
}

.chat-sidebar__item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: none;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.chat-sidebar__item-main:focus-visible {
  outline: none;
  box-shadow: inset 0 0 0 2px var(--ch-color-accent-ring);
}

/* While the absolute ⋯ menu is shown, reserve its column inside the main
   button so the longest names ellipsize instead of running under it. */
.chat-sidebar__item:hover .chat-sidebar__item-main,
.chat-sidebar__item:focus-within .chat-sidebar__item-main {
  padding-right: 26px;
}

.chat-sidebar__item-status {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  align-self: center;
  background: var(--ch-color-text-subtle);
}

/* Colors mirror the TabBar's .tab-indicator so the two surfaces agree. */
.chat-sidebar__item-status[data-status='working'] {
  background: var(--ch-color-warning);
  box-shadow: 0 0 6px var(--ch-color-warning-bg);
}

.chat-sidebar__item-status[data-status='idle'] {
  background: var(--ch-color-success);
}

.chat-sidebar__item-status[data-status='attention'] {
  background: var(--ch-color-attention);
  box-shadow: 0 0 6px var(--ch-color-attention-bg);
}

.chat-sidebar__item-status[data-status='offline'] {
  background: var(--ch-color-text-subtle);
}

/* An unread completed turn keeps the explicit success treatment, but occupies
   the same 8px slot as every other status light. The compact checkmark carries
   "completed" while the tight pulse reinforces that it is still unread. */
.chat-sidebar__item-status[data-status='idle'][data-unread='true'] {
  width: 8px;
  height: 8px;
  border: 1px solid var(--ch-color-success);
  background: var(--ch-color-success-strong);
  box-shadow:
    0 0 0 1px color-mix(in srgb, var(--ch-color-success) 36%, transparent),
    0 0 8px color-mix(in srgb, var(--ch-color-success) 42%, transparent);
}

.chat-sidebar__item-status[data-status='idle'][data-unread='true']::before {
  content: '✓';
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--ch-color-sidebar-bg);
  font-size: 6px;
  font-weight: 900;
  line-height: 1;
}

.chat-sidebar__item-status[data-status='idle'][data-unread='true']::after {
  content: '';
  position: absolute;
  inset: -3px;
  border: 1px solid color-mix(in srgb, var(--ch-color-success) 72%, transparent);
  border-radius: 50%;
  pointer-events: none;
  animation: sidebar-complete-unread-pulse 1.8s cubic-bezier(0.2, 0, 0, 1) infinite;
}

@keyframes sidebar-complete-unread-pulse {
  0% { transform: scale(0.78); opacity: 0.9; }
  72%, 100% { transform: scale(1.18); opacity: 0.08; }
}

@media (prefers-reduced-motion: reduce) {
  .chat-sidebar__item-status[data-status='idle'][data-unread='true']::after {
    animation: none;
    transform: scale(1);
    opacity: 0.55;
  }
}

.chat-sidebar__item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.chat-sidebar__item.active .chat-sidebar__item-name {
  font-weight: 600;
}

.chat-sidebar__item-time {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__item.active .chat-sidebar__item-time {
  color: var(--ch-color-text-muted);
}

/* Hover-revealed ⋯ actions menu (same actions as the TabBar). Absolute,
   over the timestamp; focus-within keeps it reachable via keyboard. */
.chat-sidebar__item > :deep(.chat-sidebar__item-menu) {
  position: absolute;
  right: 4px;
  z-index: 1;
  display: none;
}

.chat-sidebar__item:hover > :deep(.chat-sidebar__item-menu),
.chat-sidebar__item:focus-within > :deep(.chat-sidebar__item-menu) {
  display: inline-flex;
}

/* Hide the timestamp while the actions menu is shown to avoid overlap */
.chat-sidebar__item:hover .chat-sidebar__item-time,
.chat-sidebar__item:focus-within .chat-sidebar__item-time {
  visibility: hidden;
}

.chat-sidebar__rename-input {
  flex: 1;
  min-width: 0;
  /* Align with the name text inside the main button (8px dot + 6px gap). */
  margin-left: 14px;
  height: 22px;
  padding: 1px 6px;
  border: 1px solid var(--ch-color-accent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  outline: none;
}
</style>
