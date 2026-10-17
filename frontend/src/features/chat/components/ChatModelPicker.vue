<template>
  <div
    ref="modelPickerEl"
    class="composer-mode-picker composer-model-picker"
  >
    <button
      ref="modelTriggerEl"
      type="button"
      class="composer-mode-trigger"
      aria-haspopup="dialog"
      :aria-expanded="isModelMenuOpen"
      :aria-label="`Model and thinking effort: ${combinedModelLabel}`"
      :title="combinedModelLabel"
      :disabled="modeInteractionLocked || isUpdatingModel || isUpdatingReasoningEffort"
      @click="toggleModelMenu"
    >
      <span class="composer-mode-trigger-label">{{ combinedModelLabel }}</span>
      <span
        class="composer-mode-chevron"
        aria-hidden="true"
      >▴</span>
    </button>
    <div
      v-if="isModelMenuOpen"
      class="composer-mode-menu composer-model-menu"
      role="dialog"
      aria-label="Model and thinking effort"
    >
      <div class="composer-mode-search">
        <input
          ref="modelSearchEl"
          type="text"
          class="composer-textarea composer-mode-search-input"
          placeholder="Search models…"
          aria-label="Search models"
          :value="modelSearch"
          @input="modelSearch = ($event.target as HTMLInputElement).value"
        >
      </div>
      <div class="composer-model-columns">
        <div
          class="composer-mode-list composer-model-list"
          role="listbox"
          aria-label="Models"
        >
          <button
            v-for="model in filteredModelOptions"
            :key="model.id"
            type="button"
            class="composer-mode-menu-item"
            role="option"
            :aria-selected="selectedMenuModel?.id === model.id"
            @click="chooseMenuModel(model)"
          >
            <span class="composer-mode-item-label">{{ model.label }}</span>
            <span
              v-if="model.supported_reasoning_efforts.length"
              aria-hidden="true"
            >›</span>
          </button>
          <div
            v-if="filteredModelOptions.length === 0"
            class="composer-mode-empty"
          >
            No matching models
          </div>
        </div>
        <div
          v-if="selectedMenuModel"
          class="composer-effort-list"
          role="listbox"
          aria-label="Thinking effort"
        >
          <div class="composer-effort-heading">
            <strong>{{ selectedMenuModel.label }}</strong>
            <span>Thinking effort</span>
          </div>
          <button
            v-if="selectedMenuModel.supported_reasoning_efforts.length === 0"
            type="button"
            class="composer-mode-menu-item"
            role="option"
            :aria-selected="isModelActive(selectedMenuModel)"
            @click="selectModelAndEffort(selectedMenuModel, '')"
          >
            <span>Use model</span><span v-if="isModelActive(selectedMenuModel)">✓</span>
          </button>
          <button
            v-else
            type="button"
            class="composer-mode-menu-item"
            role="option"
            :aria-selected="isModelEffortActive(selectedMenuModel, '')"
            @click="selectModelAndEffort(selectedMenuModel, '')"
          >
            <span>Default ({{ defaultReasoningEffortLabel(selectedMenuModel) }})</span>
            <span v-if="isModelEffortActive(selectedMenuModel, '')">✓</span>
          </button>
          <button
            v-for="effort in selectedMenuModel.supported_reasoning_efforts"
            :key="effort.id"
            type="button"
            class="composer-mode-menu-item"
            role="option"
            :aria-selected="isModelEffortActive(selectedMenuModel, effort.id)"
            :title="effort.description || effort.label || effort.id"
            @click="selectModelAndEffort(selectedMenuModel, effort.id)"
          >
            <span>{{ effort.label || effortLabel(effort.id) }}</span>
            <span v-if="isModelEffortActive(selectedMenuModel, effort.id)">✓</span>
          </button>
        </div>
      </div>
      <div class="composer-mode-custom">
        <input
          ref="modelInputEl"
          type="text"
          class="composer-textarea"
          placeholder="custom model id…"
          :value="currentModel"
          @keydown.enter="selectModel(($event.target as HTMLInputElement).value)"
        >
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatSettingsKey, chatDeliveryKey } from '../owners'
import { effortLabel } from '@/features/chat/presentation/chatModelPresentation'
const { modelPickerEl, modelTriggerEl, isModelMenuOpen, combinedModelLabel, isUpdatingModel, isUpdatingReasoningEffort, toggleModelMenu, modelSearchEl, modelSearch, filteredModelOptions, selectedMenuModel, chooseMenuModel, isModelActive, selectModelAndEffort, isModelEffortActive, defaultReasoningEffortLabel, modelInputEl, currentModel, selectModel } = requireChatOwner(chatSettingsKey)
const { modeInteractionLocked } = requireChatOwner(chatDeliveryKey)
</script>

<style scoped>
.composer-mode-picker {
  position: relative;
  flex: 0 0 auto;
}

.composer-mode-picker.composer-model-picker {
  flex-shrink: 1;
  min-width: 0;
  max-width: 210px;
}

.composer-model-picker .composer-mode-trigger {
  max-width: 100%;
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

.composer-mode-search {
  flex: 0 0 auto;
  padding: 6px 8px;
  border-bottom: 1px solid var(--ch-color-border-strong);
}

.composer-mode-search-input {
  width: 100%;
  min-height: 28px;
  padding: 4px 8px;
  font-size: 12px;
}

.composer-mode-list {
  flex: 1 1 auto;
  min-height: 0;
  padding: 4px;
  overflow-y: auto;
}

.composer-model-menu {
  width: min(560px, calc(100vw - 32px));
  max-width: min(560px, calc(100vw - 32px));
}

.composer-model-columns {
  display: grid;
  grid-template-columns: minmax(190px, 1.25fr) minmax(160px, 1fr);
  min-height: 0;
  overflow: hidden;
}

.composer-model-list {
  border-right: 1px solid var(--ch-color-border-strong);
}

.composer-effort-list {
  min-height: 0;
  padding: 4px;
  overflow-y: auto;
}

.composer-effort-heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 9px 8px;
  color: var(--ch-color-text);
  font-size: 12px;
}

.composer-effort-heading span {
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.composer-mode-empty {
  padding: 10px 8px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  text-align: center;
}

.composer-mode-custom {
  flex: 0 0 auto;
  display: flex;
  padding: 6px 8px;
  border-top: 1px solid var(--ch-color-border-strong);
}

.composer-mode-custom .composer-textarea {
  flex: 1 1 auto;
  min-height: 28px;
  padding: 4px 8px;
  font-size: 12px;
}

.composer-mode-item-label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.composer-textarea {
  flex: 1 1 100%;
  min-width: 0;
  min-height: 32px;
  max-height: 240px;
  resize: none;
  overflow-y: hidden;
  padding: 7px 10px;
  font-size: 13px;
  line-height: 1.4;
  color: var(--ch-color-text);
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  outline: none;
  font-family: inherit;
}

.composer-textarea:focus-visible {
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.composer-textarea:disabled {
  opacity: 0.6;
}

.composer-textarea {
  min-height: 34px;
  padding: 7px 6px;
  border: 0;
  background: transparent;
}

.composer-textarea:focus-visible {
  border-color: transparent;
  box-shadow: none;
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

  .composer-mode-picker.composer-model-picker {
    max-width: 170px;
  }

  .composer-mode-menu {
    max-width: min(220px, calc(100vw - 24px));
  }

  .composer-model-menu {
    width: min(320px, calc(100vw - 24px));
    max-width: min(320px, calc(100vw - 24px));
  }

  .composer-model-columns {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .composer-model-list {
    max-height: 210px;
    border-right: 0;
    border-bottom: 1px solid var(--ch-color-border-strong);
  }

  .composer-mode-menu-item {
    min-height: 44px;
  }
}
</style>
