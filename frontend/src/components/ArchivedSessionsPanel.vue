<template>
  <Teleport to="body">
    <Transition name="archive-fade">
      <div
        v-if="open"
        class="archive-overlay"
        @click.self="$emit('close')"
      >
        <Transition
          name="archive-slide"
          appear
        >
          <aside
            class="archive-panel"
            role="dialog"
            aria-label="Archived sessions"
          >
            <div class="archive-panel__header">
              <h2 class="archive-panel__title">
                Archived Sessions
              </h2>
              <button
                type="button"
                class="archive-panel__close"
                aria-label="Close archived sessions"
                @click="$emit('close')"
              >
                ×
              </button>
            </div>

            <div class="archive-panel__body">
              <div
                v-if="isLoadingArchived"
                class="archive-panel__loading"
              >
                Loading…
              </div>
              <template v-else>
                <div
                  v-for="tab in archivedTabs"
                  :key="tab.id"
                  class="archive-item"
                >
                  <div class="archive-item__info">
                    <span class="archive-item__name">{{ tab.name || 'Untitled' }}</span>
                    <span
                      class="archive-item__cwd"
                      :title="tab.cwd || ''"
                    >{{ tab.cwd || 'No directory' }}</span>
                    <span class="archive-item__date">Archived {{ relativeTime(tab.archived_at) }} ago</span>
                  </div>
                  <div class="archive-item__actions">
                    <LoadingButton
                      type="button"
                      class="archive-item__restore"
                      :loading="pendingRestoreId === tab.id"
                      loading-label="Restoring…"
                      @click="restore(tab.id)"
                    >
                      Restore
                    </LoadingButton>
                    <button
                      v-if="confirmDeleteId !== tab.id"
                      type="button"
                      class="archive-item__delete"
                      title="Permanently delete"
                      @click="confirmDeleteId = tab.id"
                    >
                      Delete
                    </button>
                    <button
                      v-else
                      type="button"
                      class="archive-item__delete archive-item__delete--confirm"
                      title="Click to confirm permanent deletion"
                      @click="permanentDelete(tab.id)"
                    >
                      Confirm?
                    </button>
                  </div>
                </div>

                <div
                  v-if="archivedTabs.length === 0"
                  class="archive-panel__empty"
                >
                  No archived sessions.
                </div>
              </template>
            </div>
          </aside>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingButton from '@/components/LoadingButton.vue'
import { useTerminalStore } from '@/stores/terminalStore'
import { relativeTime } from '@/utils/time'

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const store = useTerminalStore()
const { archivedTabs, isLoadingArchived } = storeToRefs(store)
const { unarchiveTab, permanentDeleteTab, fetchArchivedTabs } = store

const pendingRestoreId = ref<string | null>(null)
const confirmDeleteId = ref<string | null>(null)

async function restore(tabId: string) {
  pendingRestoreId.value = tabId
  try {
    await unarchiveTab(tabId)
    emit('close')
  } finally {
    pendingRestoreId.value = null
  }
}

async function permanentDelete(tabId: string) {
  await permanentDeleteTab(tabId)
  confirmDeleteId.value = null
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

// Load the archived list whenever the panel opens, and wire Esc-to-close.
watch(
  () => props.open,
  isOpen => {
    if (isOpen) {
      confirmDeleteId.value = null
      void fetchArchivedTabs()
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
.archive-overlay {
  position: fixed;
  inset: 0;
  z-index: 900;
  background: var(--ch-color-overlay);
}

.archive-panel {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 340px;
  max-width: 90vw;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-surface);
  border-right: 1px solid var(--ch-color-border);
  box-shadow: var(--ch-shadow-soft);
}

.archive-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.archive-panel__title {
  margin: 0;
  font-size: var(--ch-font-size-lg);
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.archive-panel__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
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

.archive-panel__close:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.archive-panel__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
}

.archive-panel__loading,
.archive-panel__empty {
  padding: 24px 8px;
  text-align: center;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-subtle);
}

/* Archived items */
.archive-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 10px;
  border-radius: var(--ch-radius-md);
  transition: background var(--ch-motion-fast);
}

.archive-item:hover {
  background: var(--ch-color-row-hover);
}

.archive-item__info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.archive-item__name {
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  color: var(--ch-color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.archive-item__cwd {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.archive-item__date {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.archive-item__actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}

.archive-item__restore {
  padding: 4px 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-xs);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.archive-item__restore:hover {
  background: var(--ch-color-surface-control-hover);
}

.archive-item__delete {
  padding: 4px 10px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-xs);
  cursor: pointer;
  transition:
    background var(--ch-motion-fast),
    color var(--ch-motion-fast);
}

.archive-item__delete:hover {
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
}

.archive-item__delete--confirm {
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
}

.archive-item__delete--confirm:hover {
  background: var(--ch-color-danger);
  color: var(--ch-color-text-inverse);
}

/* Transitions */
.archive-fade-enter-active,
.archive-fade-leave-active {
  transition: opacity var(--ch-motion-standard);
}

.archive-fade-enter-from,
.archive-fade-leave-to {
  opacity: 0;
}

.archive-slide-enter-active,
.archive-slide-leave-active {
  transition: transform var(--ch-motion-standard);
}

.archive-slide-enter-from,
.archive-slide-leave-to {
  transform: translateX(-100%);
}
</style>
