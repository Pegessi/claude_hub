<template>
  <Teleport to="body">
    <Transition name="msd-fade">
      <div
        v-if="open"
        class="msd-backdrop"
        @click.self="emit('close')"
      >
        <Transition
          name="msd-slide"
          appear
        >
          <div
            class="msd-panel"
            role="dialog"
            aria-modal="true"
            aria-label="Sessions"
          >
            <div class="msd-header">
              <h2 class="msd-title">
                Sessions
              </h2>
              <button
                type="button"
                class="msd-close"
                aria-label="Close"
                @click="emit('close')"
              >
                ×
              </button>
            </div>

            <div class="msd-search">
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
                class="msd-search-input"
                placeholder="Filter chats..."
                aria-label="Filter chats"
              >
            </div>

            <div class="msd-groups">
              <div
                v-for="group in filteredGroups"
                :key="group.cwd"
                class="msd-group"
              >
                <button
                  type="button"
                  class="msd-group-header"
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
                    class="msd-group-label"
                    :title="group.cwd"
                  >{{ cwdLabel(group.cwd) }}</span>
                  <span class="msd-group-count">{{ group.tabs.length }}</span>
                  <svg
                    class="msd-group-chevron"
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
                  class="msd-group-items"
                >
                  <button
                    v-for="tab in group.tabs"
                    :key="tab.id"
                    type="button"
                    class="msd-item"
                    :class="{ active: tab.id === activeTabId }"
                    @click="selectTab(tab.id)"
                  >
                    <span class="msd-item-name">{{ tab.name || 'Untitled' }}</span>
                    <span class="msd-item-time">{{ relativeTime(tab.created_at) }}</span>
                  </button>
                </div>
              </div>

              <div
                v-if="filteredGroups.length === 0"
                class="msd-empty"
              >
                {{ filterText ? 'No matching chats' : 'No chat sessions yet' }}
              </div>
            </div>

            <div class="msd-footer">
              <button
                type="button"
                class="msd-archived-link"
                @click="openArchive"
              >
                Archived ({{ archivedTabs.length }})
              </button>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import { cwdLabel } from '@/utils/chatGroups'
import { relativeTime } from '@/utils/time'

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-archive'): void
}>()

const store = useTerminalStore()
const { chatTabsByCwd, activeTabId, archivedTabs } = storeToRefs(store)
const { setActiveTab } = store

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

function selectTab(id: string) {
  setActiveTab(id)
  emit('close')
}

function openArchive() {
  emit('open-archive')
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

// Wire Esc-to-close while the drawer is open.
watch(
  () => props.open,
  isOpen => {
    if (isOpen) {
      window.addEventListener('keydown', handleKeydown)
    } else {
      window.removeEventListener('keydown', handleKeydown)
    }
  }
)

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.msd-backdrop {
  position: fixed;
  inset: 0;
  background: var(--ch-color-overlay);
  z-index: 850;
}

.msd-panel {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: min(300px, 85vw);
  z-index: 850;
  background: var(--ch-color-surface, #fff);
  display: flex;
  flex-direction: column;
  box-shadow: var(--ch-shadow-soft);
}

/* Header */
.msd-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  padding: 12px 12px 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.msd-title {
  margin: 0;
  font-size: var(--ch-font-size-lg);
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.msd-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  min-width: 44px;
  min-height: 44px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition:
    background var(--ch-motion-fast),
    color var(--ch-motion-fast);
}

.msd-close:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

/* Search */
.msd-search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 10px 12px;
  padding: 0 8px;
  height: 36px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-subtle);
}

.msd-search:focus-within {
  border-color: var(--ch-color-accent);
}

.msd-search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  outline: none;
}

.msd-search-input::placeholder {
  color: var(--ch-color-text-subtle);
}

/* Groups */
.msd-groups {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 8px;
}

.msd-group {
  margin-bottom: 2px;
}

.msd-group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-height: 44px;
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

.msd-group-header:hover {
  background: var(--ch-color-surface-control-hover);
}

.msd-group-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.msd-group-count {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.msd-group-chevron {
  color: var(--ch-color-text-subtle);
  transition: transform var(--ch-motion-fast);
}

.msd-group-chevron.collapsed {
  transform: rotate(-90deg);
}

.msd-group-items {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 2px 0 4px;
}

/* Chat rows */
.msd-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-height: 44px;
  padding: 6px 8px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.msd-item:hover {
  background: var(--ch-color-row-hover);
}

.msd-item.active {
  background: var(--ch-color-surface-selected);
  color: var(--ch-color-text-strong);
}

.msd-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.msd-item-time {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.msd-item.active .msd-item-time {
  color: var(--ch-color-text-muted);
}

.msd-empty {
  padding: 16px 8px;
  text-align: center;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-subtle);
}

/* Footer */
.msd-footer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px;
}

.msd-archived-link {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 44px;
  padding: 7px 8px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.msd-archived-link:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

/* Transitions */
.msd-fade-enter-active,
.msd-fade-leave-active {
  transition: opacity var(--ch-motion-standard);
}

.msd-fade-enter-from,
.msd-fade-leave-to {
  opacity: 0;
}

.msd-slide-enter-active,
.msd-slide-leave-active {
  transition: transform var(--ch-motion-standard);
}

.msd-slide-enter-from,
.msd-slide-leave-to {
  transform: translateX(-100%);
}
</style>
