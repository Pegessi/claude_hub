<template>
  <div
    class="chat-sidebar__group"
    :class="{
      'chat-sidebar__group--pinned': group.pinned,
      'chat-sidebar__group--dragging': !group.pinned && dragging,
    }"
    :data-group-key="group.key"
    :data-group-drop-position="dropPosition"
  >
    <button
      v-if="group.pinned"
      type="button"
      class="chat-sidebar__group-header chat-sidebar__pinned-header"
      title="Pins are saved on this Hub. Click to collapse"
      data-pointer-drop="group"
      :aria-expanded="!group.collapsed"
      :aria-controls="`chat-group-${group.key}`"
      @click="emit('toggle-pinned')"
    >
      <span aria-hidden="true">⌖</span>
      <span class="chat-sidebar__group-label">Pinned</span>
      <span class="chat-sidebar__group-count">{{ group.tabs.length }}</span>
      <svg
        class="chat-sidebar__group-chevron"
        :class="{ collapsed: group.collapsed }"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M4.5 6 8 9.5 11.5 6l.9.9L8 11.3 3.6 6.9z"
        />
      </svg>
    </button>
    <button
      v-else
      type="button"
      class="chat-sidebar__group-header"
      data-pointer-drop="group"
      :aria-expanded="!group.collapsed"
      :aria-controls="`chat-group-${group.key}`"
      :title="`${group.cwd}. Click to collapse; drag to reorder workspaces`"
      @pointerdown="emit('group-pointerdown', $event, group)"
      @click="emit('group-click', group.cwd)"
    >
      <svg
        viewBox="0 0 16 16"
        width="14"
        height="14"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M1.5 3.5v9h13v-7h-6l-1.5-2h-5.5zm1 1h3.7l1.5 2h5.3v5h-10.5v-7z"
        />
      </svg>
      <span
        class="chat-sidebar__group-label"
        :title="group.cwd"
      >{{ cwdLabel(group.cwd) }}</span>
      <span class="chat-sidebar__group-count">{{ group.tabs.length }}</span>
      <svg
        class="chat-sidebar__group-chevron"
        :class="{ collapsed: group.collapsed }"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M4.5 6 8 9.5 11.5 6l.9.9L8 11.3 3.6 6.9z"
        />
      </svg>
    </button>

    <div
      :id="`chat-group-${group.key}`"
      class="chat-sidebar__group-items"
    >
      <slot />
      <button
        v-if="!group.pinned && !group.collapsed && !filterText.trim() && group.tabs.length > CHAT_GROUP_PREVIEW_SIZE"
        type="button"
        class="chat-sidebar__show-more"
        :aria-expanded="group.expanded"
        @click="emit('toggle-expanded', group.cwd)"
      >
        {{ group.expanded ? 'Show less' : `Show more (${group.hiddenCount})` }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { CHAT_GROUP_PREVIEW_SIZE, cwdLabel, type ChatSidebarGroup } from '@/utils/chatGroups'

defineProps<{
  group: ChatSidebarGroup
  dragging: boolean
  dropPosition?: 'before' | 'after'
  filterText: string
}>()
const emit = defineEmits<{
  'toggle-pinned': []
  'group-pointerdown': [event: PointerEvent, group: ChatSidebarGroup]
  'group-click': [cwd: string]
  'toggle-expanded': [cwd: string]
}>()
</script>

<style scoped>
/* Groups */
.chat-sidebar__group {
  position: relative;
  margin-bottom: 8px;
}

.chat-sidebar__group--pinned {
  position: sticky;
  top: 0;
  z-index: 2;
  max-height: 40vh;
  overflow-y: auto;
  background: var(--ch-color-sidebar-bg);
  border-bottom: 1px solid var(--ch-color-border-muted);
  margin-bottom: 6px;
}

.chat-sidebar__group-header.chat-sidebar__pinned-header {
  cursor: pointer;
}

.chat-sidebar__show-more {
  padding: 6px 12px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  text-align: left;
  font-size: var(--ch-font-size-xs);
  cursor: pointer;
}

.chat-sidebar__show-more:hover {
  background: var(--ch-color-row-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__group-header {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 7px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  cursor: pointer;
  touch-action: pan-y;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__group-header:hover {
  background: var(--ch-color-surface-control-hover);
}

.chat-sidebar__group-header:not(.chat-sidebar__pinned-header) {
  cursor: grab;
}

.chat-sidebar__group-header:not(.chat-sidebar__pinned-header):active {
  cursor: grabbing;
}

.chat-sidebar__group-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.chat-sidebar__group-count {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__group-chevron {
  color: var(--ch-color-text-subtle);
  transition: transform var(--ch-motion-fast);
}

.chat-sidebar__group-chevron.collapsed {
  transform: rotate(-90deg);
}

.chat-sidebar__group-items {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 3px 0 6px;
}

.chat-sidebar__group--dragging {
  opacity: 0.45;
}

:global(body.chat-sidebar-group-dragging) {
  cursor: grabbing !important;
  user-select: none;
}

.chat-sidebar__group[data-group-drop-position] > .chat-sidebar__group-header::before {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  z-index: 3;
  height: 2px;
  border-radius: 999px;
  background: var(--ch-color-accent);
  box-shadow: 0 0 8px var(--ch-color-accent-halo);
  pointer-events: none;
}

.chat-sidebar__group[data-group-drop-position='before'] > .chat-sidebar__group-header::before {
  top: -4px;
}

.chat-sidebar__group[data-group-drop-position='after'] > .chat-sidebar__group-header::before {
  bottom: -4px;
}
</style>
