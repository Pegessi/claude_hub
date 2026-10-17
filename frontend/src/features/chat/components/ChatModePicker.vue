<template>
  <div
    ref="modePickerEl"
    class="composer-mode-picker"
  >
    <button
      ref="modeTriggerEl"
      type="button"
      class="composer-mode-trigger"
      aria-haspopup="menu"
      :aria-expanded="isModeMenuOpen"
      :aria-label="`Chat mode: ${currentModeLabel}`"
      :title="currentModeOption?.description || `${currentModeLabel} mode`"
      :disabled="modeInteractionLocked || isUpdatingMode"
      @click="toggleModeMenu"
    >
      <span class="composer-mode-trigger-label">{{ currentModeLabel }}</span>
      <span
        class="composer-mode-chevron"
        aria-hidden="true"
      >▴</span>
    </button>
    <div
      v-if="isModeMenuOpen"
      ref="modeMenuEl"
      class="composer-mode-menu"
      role="menu"
      aria-label="Chat mode"
    >
      <button
        v-for="option in modeOptions"
        :key="option.id"
        type="button"
        class="composer-mode-menu-item"
        role="menuitemradio"
        :aria-checked="currentModeId === option.id"
        :disabled="option.id === 'plan' && Boolean(goalPlanReason)"
        :title="option.id === 'plan' && goalPlanReason ? goalPlanReason : (option.description || `${option.label} mode`)"
        @click="selectMode(option.id)"
      >
        <span>{{ option.label }}</span>
        <span
          v-if="currentModeId === option.id"
          class="composer-mode-check"
          aria-hidden="true"
        >✓</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatSettingsKey, chatDeliveryKey, chatGoalKey } from '../owners'
const { modePickerEl, modeTriggerEl, isModeMenuOpen, currentModeLabel, currentModeOption, isUpdatingMode, toggleModeMenu, modeMenuEl, modeOptions, currentModeId, selectMode } = requireChatOwner(chatSettingsKey)
const { modeInteractionLocked } = requireChatOwner(chatDeliveryKey)
const { goalPlanReason } = requireChatOwner(chatGoalKey)
</script>

<style scoped>
.composer-mode-picker {
  position: relative;
  flex: 0 0 auto;
}

.composer-mode-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: auto;
  max-width: 210px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.composer-mode-trigger:hover:not(:disabled),

.composer-mode-trigger[aria-expanded='true'] {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-trigger-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-mode-chevron {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-size: 9px;
  line-height: 1;
}

.composer-mode-trigger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-mode-trigger:focus-visible,

.composer-mode-menu-item:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-mode-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 7px);
  z-index: 8;
  width: max-content;
  min-width: max(180px, 100%);
  max-width: min(280px, calc(100vw - 32px));
  max-height: min(420px, 65vh);
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-md, 0 10px 30px rgb(0 0 0 / 26%));
}

.composer-mode-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  min-height: 34px;
  padding: 6px 9px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.composer-mode-menu-item:hover,

.composer-mode-menu-item:focus-visible {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-menu-item[aria-checked='true'] {
  color: var(--ch-color-accent);
}

.composer-mode-check {
  flex: 0 0 auto;
  font-size: 11px;
}
@media (max-width: 640px) {

  .composer-mode-trigger {
    min-height: 44px;
    height: 44px;
  }

  .composer-mode-trigger {
    max-width: 170px;
    padding: 0 7px;
  }

  .composer-mode-menu {
    max-width: min(220px, calc(100vw - 24px));
  }

  .composer-mode-menu-item {
    min-height: 44px;
  }
}
</style>
