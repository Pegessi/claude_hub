<template>
  <!-- Toast / notification stack for workspace mode -->
  <div
    v-if="workspaceNotifications.length"
    class="toast-stack"
    role="region"
    aria-label="Notifications"
  >
    <div
      v-for="n in workspaceNotifications"
      :key="n.id"
      :class="['toast', `toast--${n.type}`]"
      role="status"
    >
      <span
        class="toast__icon"
        aria-hidden="true"
      />
      <span class="toast__message">{{ n.message }}</span>
      <button
        type="button"
        class="toast__close"
        :aria-label="'Dismiss ' + n.type + ' notification'"
        @click="dismissToast(n)"
      >
        ×
      </button>
      <div
        v-if="n.autoDismissMs"
        class="toast__timer"
        :style="{ animationDuration: `${n.autoDismissMs}ms` }"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  useWorkspaceRuntimeContext,
} from '../context'
const {
  workspaceNotifications,
  dismissToast,
} = useWorkspaceRuntimeContext()
</script>

<style scoped>
.toast-stack {
  position: fixed;
  top: 72px;
  right: 16px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(420px, calc(100vw - 32px));
  pointer-events: none;
}

.toast {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px 10px 14px;
  border-radius: var(--ch-radius-md);
  border: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
  font-size: 13px;
  line-height: 1.45;
  overflow: hidden;
  pointer-events: auto;
  animation: ws-toast-in 180ms cubic-bezier(0.2, 0, 0, 1);
}

@keyframes ws-toast-in {
  from {
    opacity: 0;
    transform: translateY(-6px) scale(0.98);
  }

  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.toast::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--ch-color-text-muted);
}

.toast__icon {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  margin-top: 1px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.toast__message {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
  padding-top: 1px;
}

.toast__close {
  flex: 0 0 auto;
  background: transparent;
  border: none;
  color: var(--ch-color-text-subtle);
  font-size: 16px;
  line-height: 1;
  padding: 2px;
  margin-top: -1px;
  cursor: pointer;
  transition: color var(--ch-motion-fast);
  border-radius: 3px;
}

.toast__close:hover {
  color: var(--ch-color-text);
  background: var(--ch-color-row-hover);
}

.toast__timer {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 2px;
  background: currentColor;
  opacity: 0.35;
  transform-origin: left center;
  animation-name: ws-toast-timer;
  animation-timing-function: linear;
  animation-fill-mode: forwards;
}

@keyframes ws-toast-timer {
  from {
    width: 100%;
  }

  to {
    width: 0%;
  }
}

.toast--error::before {
  background: var(--ch-color-danger);
}

.toast--error .toast__icon {
  color: var(--ch-color-danger);
}

.toast--error .toast__icon::after {
  content: '!';
}

.toast--warning::before {
  background: var(--ch-color-warning);
}

.toast--warning .toast__icon {
  color: var(--ch-color-warning);
}

.toast--warning .toast__icon::after {
  content: '△';
}

.toast--success::before {
  background: var(--ch-color-success);
}

.toast--success .toast__icon {
  color: var(--ch-color-success);
}

.toast--success .toast__icon::after {
  content: '✓';
}

.toast--info::before {
  background: var(--ch-color-info);
}

.toast--info .toast__icon {
  color: var(--ch-color-info);
}

.toast--info .toast__icon::after {
  content: 'i';
}

.toast--error {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

@media (max-width: 768px) {
  .toast-stack {
    top: 12px;
    right: 8px;
    left: 8px;
    max-width: none;
  }
}
</style>
