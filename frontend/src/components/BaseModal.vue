<template>
  <Transition name="base-modal-fade">
    <div
      v-if="open"
      class="base-modal__overlay"
      @click.self="requestClose"
    >
      <div
        class="base-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title || undefined"
      >
        <div
          v-if="$slots.header || title"
          class="base-modal__header"
        >
          <slot name="header">
            <h3 class="base-modal__title">
              {{ title }}
            </h3>
          </slot>
        </div>

        <div class="base-modal__body">
          <slot />
        </div>

        <div
          v-if="$slots.footer"
          class="base-modal__footer"
        >
          <slot name="footer" />
        </div>
      </div>
    </div>
  </Transition>
</template>

<script setup lang="ts">
/*
 * BaseModal — the shared, token-driven modal shell (backdrop + centered dialog).
 *
 * Owns the chrome so consumers stop hand-rolling overlays: backdrop, centering,
 * ESC-to-close, backdrop-click-to-close, entrance/exit motion, and a11y
 * (role=dialog, aria-modal). Consumers supply only content via slots.
 *
 * Props:
 *   open  — visibility; v-model-friendly (use `v-model:open`). Also emits `close`.
 *   title — optional; rendered in the header when no `header` slot is provided,
 *           and used as the dialog's aria-label.
 *
 * Emits:
 *   update:open (boolean) — for v-model:open
 *   close                 — fired on ESC or backdrop self-click
 *
 * Slots: header (falls back to `title`), default (body), footer.
 *
 * Motion: enter ~180ms cubic-bezier(0.2,0,0,1), leave ~120ms ease, via the
 * --ch-motion-drawer / --ch-motion-fast tokens with literal fallbacks. A local
 * prefers-reduced-motion guard neutralizes the transition, so the component is
 * self-sufficient regardless of any app-level guard.
 *
 * Every design-token reference carries a literal fallback (var(--token, <value>)).
 *
 * Example:
 *   <BaseModal v-model:open="showDialog" title="Confirm">
 *     <p>Are you sure?</p>
 *     <template #footer><button @click="showDialog = false">Cancel</button></template>
 *   </BaseModal>
 */
import { watch, onBeforeUnmount } from 'vue'

const props = withDefaults(defineProps<{
  open?: boolean
  title?: string
}>(), {
  open: false,
  title: '',
})

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'close'): void
}>()

function requestClose() {
  emit('update:open', false)
  emit('close')
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    requestClose()
  }
}

watch(() => props.open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('keydown', handleKeydown)
  } else {
    document.removeEventListener('keydown', handleKeydown)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.base-modal__overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: var(--ch-space-4, 16px);
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  background: var(--ch-color-overlay, rgba(0, 0, 0, 0.58));
}

.base-modal {
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  width: min(520px, 100%);
  max-height: calc(100dvh - var(--ch-space-4, 16px) * 2);
  overflow: hidden;
  padding: var(--ch-space-5, 24px);
  border: 1px solid var(--ch-color-border, #333);
  border-radius: var(--ch-radius-lg, 10px);
  background: var(--ch-color-surface, #1e1e1e);
  box-shadow: var(--ch-shadow-dialog, 0 24px 80px rgba(0, 0, 0, 0.45));
  color: var(--ch-color-text, #f4f4f5);
}

.base-modal__header {
  flex-shrink: 0;
  margin-bottom: var(--ch-space-4, 16px);
}

.base-modal__title {
  margin: 0;
  font-size: var(--ch-font-lg, 15px);
  font-weight: var(--ch-weight-semibold, 600);
  color: var(--ch-color-text-strong, #fafafa);
}

.base-modal__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.base-modal__footer {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--ch-space-2, 8px);
  margin-top: var(--ch-space-4, 16px);
}

/* Enter ~180ms cubic-bezier(0.2,0,0,1); leave ~120ms ease (token-driven). */
.base-modal-fade-enter-active {
  transition: opacity var(--ch-motion-drawer, 180ms cubic-bezier(0.2, 0, 0, 1));
}

.base-modal-fade-leave-active {
  transition: opacity var(--ch-motion-fast, 120ms ease);
}

.base-modal-fade-enter-active .base-modal {
  transition: transform var(--ch-motion-drawer, 180ms cubic-bezier(0.2, 0, 0, 1));
}

.base-modal-fade-leave-active .base-modal {
  transition: transform var(--ch-motion-fast, 120ms ease);
}

.base-modal-fade-enter-from,
.base-modal-fade-leave-to {
  opacity: 0;
}

.base-modal-fade-enter-from .base-modal,
.base-modal-fade-leave-to .base-modal {
  transform: translateY(-8px) scale(0.98);
}

/* Self-sufficient reduced-motion guard: neutralize this component's own motion
   regardless of any app-level guard. */
@media (prefers-reduced-motion: reduce) {
  .base-modal-fade-enter-active,
  .base-modal-fade-leave-active,
  .base-modal-fade-enter-active .base-modal,
  .base-modal-fade-leave-active .base-modal {
    transition: none;
  }

  .base-modal-fade-enter-from .base-modal,
  .base-modal-fade-leave-to .base-modal {
    transform: none;
  }
}
</style>
