<template>
  <aside
    class="chat-sidebar"
    :class="{ collapsed: sidebarCollapsed }"
    aria-label="Chat sessions"
  >
    <!-- Header: title + collapse toggle -->
    <div class="chat-sidebar__header">
      <span
        v-if="!sidebarCollapsed"
        class="chat-sidebar__title"
      >Chats</span>
      <button
        type="button"
        class="chat-sidebar__icon-btn"
        :title="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        :aria-label="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        @click="toggleSidebar"
      >
        <svg
          class="chat-sidebar__chevron"
          :class="{ flipped: sidebarCollapsed }"
          viewBox="0 0 16 16"
          width="16"
          height="16"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M10 12.5 5.5 8 10 3.5l.9.9L7.3 8l3.6 3.6z"
          />
        </svg>
      </button>
    </div>

    <!-- Expanded body -->
    <template v-if="!sidebarCollapsed">
      <div class="chat-sidebar__search">
        <svg
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M11.4 10.6a5.5 5.5 0 1 0-.8.8l3 3 .8-.8-3-3zM7 11.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"
          />
        </svg>
        <input
          v-model="filterText"
          type="text"
          class="chat-sidebar__search-input"
          placeholder="Filter chats..."
          aria-label="Filter chats"
        >
      </div>

      <div class="chat-sidebar__body">
        <div
          v-for="group in filteredGroups"
          :key="group.cwd"
          class="chat-sidebar__group"
        >
          <button
            type="button"
            class="chat-sidebar__group-header"
            :aria-expanded="!collapsedGroups.has(group.cwd)"
            @click="toggleGroup(group.cwd)"
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
              :class="{ collapsed: collapsedGroups.has(group.cwd) }"
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
            v-if="!collapsedGroups.has(group.cwd)"
            class="chat-sidebar__group-items"
          >
            <button
              v-for="tab in group.tabs"
              :key="tab.id"
              type="button"
              class="chat-sidebar__item"
              :class="{ active: tab.id === activeTabId }"
              @click="setActiveTab(tab.id)"
            >
              <span
                class="chat-sidebar__item-status"
                :data-status="getTabStatus(tab)"
                :data-unread="tab.is_unread ? 'true' : 'false'"
                role="img"
                :aria-label="`Session status: ${getTabStatusLabel(tab)}`"
                :title="`Session status: ${getTabStatusLabel(tab)}`"
              />
              <span class="chat-sidebar__item-name">{{ tab.name || 'Untitled' }}</span>
              <span class="chat-sidebar__item-time">{{ relativeTime(tab.created_at) }}</span>
              <span
                class="chat-sidebar__item-archive"
                title="Archive session"
                aria-label="Archive session"
                @click.stop="archiveTab(tab.id)"
              >
                <svg
                  viewBox="0 0 16 16"
                  width="13"
                  height="13"
                  aria-hidden="true"
                >
                  <path
                    fill="currentColor"
                    d="M2 3.5h12v2h-.6l-.8 7.5H3.4L2.6 5.5H2v-2zm1.7 2 .6 6.5h7.4l.6-6.5H3.7zM6 7h1v4H6V7zm3 0h1v4H9V7zM4 4.5h8v-1H4v1z"
                  />
                </svg>
              </span>
            </button>
          </div>
        </div>

        <div
          v-if="filteredGroups.length === 0"
          class="chat-sidebar__empty"
        >
          {{ filterText ? 'No matching chats' : 'No chat sessions yet' }}
        </div>
      </div>
    </template>

    <!-- Collapsed body: icon-only quick access -->
    <div
      v-else
      class="chat-sidebar__body chat-sidebar__body--icons"
    >
      <button
        type="button"
        class="chat-sidebar__icon-btn chat-sidebar__icon-btn--big"
        title="Show chats"
        aria-label="Show chats"
        @click="toggleSidebar"
      >
        <svg
          viewBox="0 0 16 16"
          width="18"
          height="18"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M8 2a6.5 6.5 0 0 0-6.5 6.5c0 1.2.3 2.3.9 3.3L2 14l2.4-.5c1.1.6 2.3 1 3.6 1A6.5 6.5 0 1 0 8 2zm0 1a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM5.5 7a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm5 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2z"
          />
        </svg>
      </button>
    </div>

    <!-- Footer: archived sessions browser entry -->
    <div class="chat-sidebar__footer">
      <button
        type="button"
        class="chat-sidebar__archived-btn"
        :title="sidebarCollapsed ? 'View archived sessions' : ''"
        @click="$emit('open-archive')"
      >
        <svg
          viewBox="0 0 16 16"
          width="15"
          height="15"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M2 3.5h12v2h-.6l-.8 7.5H3.4L2.6 5.5H2v-2zm1.7 2 .6 6.5h7.4l.6-6.5H3.7zM6 7h1v4H6V7zm3 0h1v4H9V7zM4 4.5h8v-1H4v1z"
          />
        </svg>
        <span
          v-if="!sidebarCollapsed"
          class="chat-sidebar__archived-label"
        >Archived ({{ archivedTabs.length }})</span>
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import { cwdLabel } from '@/utils/chatGroups'
import { relativeTime } from '@/utils/time'
import { useTabStatus } from '@/composables/useTabStatus'

defineEmits<{
  (e: 'open-archive'): void
}>()

const store = useTerminalStore()
const {
  chatTabsByCwd,
  activeTabId,
  sidebarCollapsed,
  archivedTabs,
  agentStatuses,
} = storeToRefs(store)
const { toggleSidebar, setActiveTab, archiveTab } = store

// Tab-status logic is shared with the TabBar via useTabStatus.
const { getTabStatus, getTabStatusLabel } = useTabStatus(agentStatuses)

const filterText = ref('')
const collapsedGroups = ref<Set<string>>(new Set())

function toggleGroup(cwd: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(cwd)) {
    next.delete(cwd)
  } else {
    next.add(cwd)
  }
  collapsedGroups.value = next
}

const filteredGroups = computed(() => {
  const query = filterText.value.trim().toLowerCase()
  if (!query) return chatTabsByCwd.value
  return chatTabsByCwd.value
    .map(group => ({
      cwd: group.cwd,
      tabs: group.tabs.filter(
        tab =>
          (tab.name || '').toLowerCase().includes(query) ||
          (tab.cwd || '').toLowerCase().includes(query)
      ),
    }))
    .filter(group => group.tabs.length > 0)
})
</script>

<style scoped>
.chat-sidebar {
  display: flex;
  flex-direction: column;
  width: 240px;
  min-width: 240px;
  height: 100%;
  background: var(--ch-color-surface);
  border-right: 1px solid var(--ch-color-border-muted);
  transition:
    width var(--ch-motion-standard),
    min-width var(--ch-motion-standard);
  overflow: hidden;
}

.chat-sidebar.collapsed {
  width: 48px;
  min-width: 48px;
}

/* Header */
.chat-sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  padding: 10px 8px 8px;
}

.chat-sidebar__title {
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  color: var(--ch-color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding-left: 6px;
}

.chat-sidebar__icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  transition:
    background var(--ch-motion-fast),
    color var(--ch-motion-fast);
}

.chat-sidebar__icon-btn:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__icon-btn--big {
  width: 36px;
  height: 36px;
}

.chat-sidebar__chevron {
  transition: transform var(--ch-motion-fast);
}

.chat-sidebar__chevron.flipped {
  transform: rotate(180deg);
}

/* Search */
.chat-sidebar__search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 10px 8px;
  padding: 0 8px;
  height: 30px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__search:focus-within {
  border-color: var(--ch-color-accent);
}

.chat-sidebar__search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  outline: none;
}

.chat-sidebar__search-input::placeholder {
  color: var(--ch-color-text-subtle);
}

/* Body */
.chat-sidebar__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 8px;
}

.chat-sidebar__body--icons {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 8px;
}

/* Groups */
.chat-sidebar__group {
  margin-bottom: 2px;
}

.chat-sidebar__group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__group-header:hover {
  background: var(--ch-color-surface-control-hover);
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
  gap: 1px;
  margin: 2px 0 4px;
}

/* Chat rows */
.chat-sidebar__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__item:hover {
  background: var(--ch-color-row-hover);
}

.chat-sidebar__item.active {
  background: var(--ch-color-surface-selected);
  color: var(--ch-color-text-strong);
}

.chat-sidebar__item-status {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  align-self: center;
  background: var(--ch-color-text-subtle);
  box-shadow: 0 0 0 1px var(--ch-color-border-muted);
}

.chat-sidebar__item-status[data-status='working'] {
  background: var(--ch-color-accent);
  animation: sidebar-status-pulse 1.2s ease-in-out infinite;
}

.chat-sidebar__item-status[data-status='idle'] {
  background: var(--ch-color-success);
}

.chat-sidebar__item-status[data-status='attention'] {
  background: var(--ch-color-warning);
}

.chat-sidebar__item-status[data-status='offline'] {
  background: var(--ch-color-text-subtle);
}

/* Unread ping: an accent ring that expands outward from the dot and fades,
   repeating. Shown only for tabs with an unread completed turn. */
.chat-sidebar__item-status[data-unread='true']::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  border-radius: 50%;
  border: 1.5px solid var(--ch-color-accent);
  animation: sidebar-unread-ping 1.6s cubic-bezier(0, 0, 0.2, 1) infinite;
}

@keyframes sidebar-unread-ping {
  0% { transform: scale(1); opacity: 0.9; }
  75%, 100% { transform: scale(2.8); opacity: 0; }
}

@keyframes sidebar-status-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.85); }
}

.chat-sidebar__item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.chat-sidebar__item-time {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__item.active .chat-sidebar__item-time {
  color: var(--ch-color-text-muted);
}

/* Hover-revealed archive shortcut */
.chat-sidebar__item-archive {
  position: absolute;
  right: 4px;
  display: none;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__item:hover .chat-sidebar__item-archive {
  display: inline-flex;
}

.chat-sidebar__item-archive:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

/* Hide the timestamp when the archive button is revealed to avoid overlap */
.chat-sidebar__item:hover .chat-sidebar__item-time {
  visibility: hidden;
}

.chat-sidebar__empty {
  padding: 16px 8px;
  text-align: center;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-subtle);
}

/* Footer */
.chat-sidebar__footer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px;
}

.chat-sidebar__archived-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 8px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__archived-btn:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__archived-label {
  flex: 1;
  text-align: left;
}

/* Mobile: sidebar is hidden to save the limited width */
@media (max-width: 768px) {
  .chat-sidebar {
    display: none;
  }
}
</style>
