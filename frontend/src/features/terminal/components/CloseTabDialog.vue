<template>
  <div
    v-if="visible"
    class="modal-overlay"
    data-drawer-swipe-ignore
    @click.self="$emit('close')"
  >
    <div class="modal">
      <h3>Close Terminal</h3>
      <p class="confirm-message">
        Are you sure you want to close "{{ tab?.name }}"?
      </p>
      <div class="modal-actions">
        <button
          type="button"
          class="ch-btn"
          @click="$emit('close')"
        >
          Cancel
        </button>
        <LoadingButton
          type="button"
          class="ch-btn ch-btn--danger"
          :disabled="loading"
          :loading="pending"
          loading-label="Closing tab"
          @click="$emit('confirm')"
        >
          {{ loading ? 'Closing...' : 'Close' }}
        </LoadingButton>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import type { TerminalTab } from '@/types'

defineProps<{
  visible: boolean
  tab: TerminalTab | null
  loading: boolean
  pending: boolean
}>()

defineEmits<{
  (event: 'close'): void
  (event: 'confirm'): void
}>()
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--ch-color-overlay);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}

.modal {
  background-color: var(--ch-color-surface);
  border-radius: var(--ch-radius-lg);
  padding: 24px;
  min-width: 400px;
  max-width: min(90vw, 640px);
  border: 1px solid var(--ch-color-border);
  box-shadow: var(--ch-shadow-dialog);
}

.modal h3 {
  margin: 0 0 20px;
  color: var(--ch-color-text-strong);
  font-size: var(--ch-font-size-lg);
}

.confirm-message {
  color: var(--ch-color-text-muted);
  margin: 16px 0;
  line-height: 1.5;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
}

@media (max-width: 640px) {
  .modal-overlay {
    align-items: flex-start;
    justify-content: flex-start;
    padding: 10px;
  }

  .modal {
    min-width: 0;
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 16px;
    border-radius: var(--ch-radius-md);
  }

  .modal-actions {
    position: sticky;
    bottom: -1px;
    background-color: var(--ch-color-surface);
    padding-top: 12px;
  }

  .modal-actions .ch-btn {
    flex: 1;
  }
}
</style>
