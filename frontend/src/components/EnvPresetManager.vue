<template>
  <div
    v-if="visible"
    class="modal-overlay"
    @click.self="handleClose"
  >
    <div class="modal env-manage-modal">
      <div class="env-manage-header">
        <h3>Manage Environment Presets</h3>
        <button
          type="button"
          class="btn btn-secondary btn-small"
          @click="handleClose"
        >
          Close
        </button>
      </div>
      <div class="env-manage-body">
        <div class="env-manage-sidebar">
          <div class="env-manage-sidebar-header">
            <span>Presets</span>
            <button
              type="button"
              class="btn btn-secondary btn-small env-new-btn"
              title="Create new preset"
              @click="handleNew"
            >
              + New
            </button>
          </div>
          <div class="env-preset-list">
            <div
              v-for="preset in envPresets"
              :key="preset.id"
              :class="['env-preset-item', { active: selectedId === preset.id }]"
              @click="handleSelect(preset.id)"
            >
              <span class="env-preset-item-name">{{ preset.name }}</span>
              <span
                v-if="isBuiltIn(preset.id)"
                class="env-preset-item-badge"
                title="Built-in preset"
              >
                built-in
              </span>
            </div>
          </div>
        </div>
        <div class="env-manage-editor">
          <div v-if="selectedPreset || isNewPreset">
            <div class="form-group">
              <label for="env-preset-name-input">
                Name
                <span
                  v-if="isNewPreset"
                  class="form-hint-inline"
                >
                  (new preset)
                </span>
              </label>
              <input
                id="env-preset-name-input"
                v-model="draftName"
                type="text"
                :disabled="!canEditSelected"
                placeholder="Preset name"
              >
            </div>
            <div class="form-group env-textarea-group">
              <label for="env-preset-text-input">
                Environment Variables
                <span class="form-hint-inline">
                  One per line, KEY=value format. Values are not printed in logs.
                </span>
              </label>
              <textarea
                id="env-preset-text-input"
                v-model="draftText"
                class="env-textarea env-textarea-large"
                :disabled="!canEditSelected"
                spellcheck="false"
                placeholder="HTTP_PROXY=http://127.0.0.1:7890&#10;HTTPS_PROXY=http://127.0.0.1:7890&#10;NO_PROXY=localhost,127.0.0.1,::1"
              />
              <p
                v-if="selectedPreset && !canEditSelected"
                class="form-hint"
              >
                {{ selectedPreset.id === 'none'
                  ? 'The "No custom env" preset cannot be edited.'
                  : 'Built-in presets cannot be edited. Create a new preset or duplicate this one to customize.' }}
              </p>
            </div>
          </div>
          <div
            v-else
            class="env-empty-state"
          >
            <p>Select a preset or create a new one.</p>
          </div>
        </div>
      </div>
      <div class="env-manage-footer">
        <button
          type="button"
          class="btn btn-danger"
          :disabled="!canDeleteSelected"
          @click="handleDelete"
        >
          Delete
        </button>
        <div class="env-manage-footer-right">
          <button
            type="button"
            class="btn btn-secondary"
            @click="handleClose"
          >
            Cancel
          </button>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="!canSaveSelected"
            @click="handleSave"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { BUILT_IN_PRESET_IDS, useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import type { LaunchEnvPreset } from '@/composables/useLaunchEnvPresets'

interface Props {
  visible: boolean
  modelValue: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'update:modelValue', id: string): void
  (e: 'close'): void
}>()

const { envPresets, savePreset, deletePreset } = useLaunchEnvPresets()

const selectedId = ref(props.modelValue)
const draftName = ref('')
const draftText = ref('')
const isNewPreset = ref(false)

const selectedPreset = computed<LaunchEnvPreset | null>(
  () => envPresets.value.find(p => p.id === selectedId.value) ?? null
)

const canEditSelected = computed(() => {
  if (isNewPreset.value) return true
  if (!selectedPreset.value) return false
  return !isBuiltIn(selectedPreset.value.id) && selectedPreset.value.id !== 'none'
})

const canSaveSelected = computed(() => {
  if (!canEditSelected.value) return false
  if (!draftName.value.trim()) return false
  if (!draftText.value.trim()) return false
  if (isNewPreset.value) return true
  return draftName.value !== selectedPreset.value?.name || draftText.value !== selectedPreset.value?.text
})

const canDeleteSelected = computed(() => {
  if (!selectedPreset.value) return false
  const id = selectedPreset.value.id
  return id !== 'none'
})

function isBuiltIn(id: string): boolean {
  return BUILT_IN_PRESET_IDS.includes(id)
}

function loadPresetIntoDraft(preset: LaunchEnvPreset) {
  draftName.value = preset.name
  draftText.value = preset.text
  isNewPreset.value = false
}

function handleSelect(id: string) {
  selectedId.value = id
  const preset = envPresets.value.find(p => p.id === id)
  if (preset) {
    loadPresetIntoDraft(preset)
  }
}

function handleNew() {
  selectedId.value = `new-${Date.now().toString(36)}`
  draftName.value = ''
  draftText.value = ''
  isNewPreset.value = true
}

function handleSave() {
  if (!canSaveSelected.value) return
  const name = draftName.value.trim()
  const text = draftText.value
  const id = isNewPreset.value ? undefined : selectedId.value
  const result = savePreset(name, text, id)
  if (result) {
    selectedId.value = result.id
    emit('update:modelValue', result.id)
    isNewPreset.value = false
  }
}

function handleDelete() {
  if (!canDeleteSelected.value) return
  if (!selectedPreset.value) return
  const id = selectedPreset.value.id
  const name = selectedPreset.value.name
  if (!confirm(`Delete preset "${name}"?`)) return
  if (deletePreset(id)) {
    // Select 'none' after deletion
    selectedId.value = 'none'
    emit('update:modelValue', 'none')
    const nonePreset = envPresets.value.find(p => p.id === 'none')
    if (nonePreset) {
      loadPresetIntoDraft(nonePreset)
    }
  }
}

function handleClose() {
  emit('close')
}

// Sync with external model value
watch(
  () => props.modelValue,
  (newVal) => {
    if (newVal !== selectedId.value && !isNewPreset.value) {
      selectedId.value = newVal
    }
  }
)

// When the modal opens, load the current selection into draft
watch(
  () => props.visible,
  (newVisible) => {
    if (newVisible) {
      selectedId.value = props.modelValue
      const preset = envPresets.value.find(p => p.id === props.modelValue)
      if (preset) {
        loadPresetIntoDraft(preset)
      } else {
        // Fallback to 'none'
        const nonePreset = envPresets.value.find(p => p.id === 'none')
        if (nonePreset) {
          selectedId.value = 'none'
          loadPresetIntoDraft(nonePreset)
        }
      }
    }
  }
)
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--ch-color-overlay);
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 16px;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  z-index: 1100;
}

.modal {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  box-shadow: var(--ch-shadow-dialog);
  padding: 20px;
  min-width: 640px;
  width: min(800px, 100%);
  max-width: 100%;
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.env-manage-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-shrink: 0;
}

.env-manage-header h3 {
  margin: 0;
  color: var(--ch-color-text);
  font-size: 18px;
}

.env-manage-body {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.env-manage-sidebar {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border);
  border-radius: 6px;
  background: var(--ch-color-surface-soft);
  overflow: hidden;
}

.env-manage-sidebar-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ch-color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-sunken);
}

.env-new-btn {
  font-size: 11px;
  padding: 4px 8px;
  text-transform: none;
  letter-spacing: normal;
}

.env-preset-list {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.env-preset-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  color: var(--ch-color-text);
  font-size: 13px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  transition: background var(--ch-motion-fast);
}

.env-preset-item:last-child {
  border-bottom: none;
}

.env-preset-item:hover {
  background: var(--ch-color-surface-control-hover);
}

.env-preset-item.active {
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-text);
}

.env-preset-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.env-preset-item-badge {
  font-size: 10px;
  color: var(--ch-color-text-soft);
  background: var(--ch-color-surface-control);
  padding: 2px 6px;
  border-radius: 4px;
  flex-shrink: 0;
  text-transform: lowercase;
}

.env-manage-editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  color: var(--ch-color-text);
  margin-bottom: 6px;
  font-size: 14px;
  font-weight: 500;
}

.form-group input {
  width: 100%;
  padding: 8px 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  color: var(--ch-color-text);
  font-size: 14px;
  box-sizing: border-box;
}

.form-group input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
}

.form-group input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.form-hint-inline {
  font-weight: normal;
  font-size: 12px;
  color: var(--ch-color-text-soft);
  margin-left: 6px;
}

.form-hint {
  color: var(--ch-color-text-soft);
  font-size: 12px;
  margin: 6px 0 0 0;
}

.env-textarea-group {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.env-textarea-group .form-hint {
  flex-shrink: 0;
}

.env-textarea {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  color: var(--ch-color-text);
  font-family: monospace !important;
  font-size: 13px;
  line-height: 1.45;
  resize: vertical;
  spellcheck: false;
}

.env-textarea:focus {
  outline: none;
  border-color: var(--ch-color-accent);
}

.env-textarea:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.env-textarea-large {
  min-height: 280px;
  flex: 1;
  resize: vertical;
}

.env-empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--ch-color-text-soft);
  font-size: 14px;
  border: 1px dashed var(--ch-color-border);
  border-radius: 6px;
  background: var(--ch-color-surface-soft);
}

.env-manage-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--ch-color-border);
  flex-shrink: 0;
}

.env-manage-footer-right {
  display: flex;
  gap: 10px;
}

.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  transition: background-color 0.2s;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-small {
  padding: 5px 10px;
  font-size: 12px;
}

.btn-secondary {
  background-color: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.btn-secondary:hover:not(:disabled) {
  background-color: var(--ch-color-surface-pressed);
}

.btn-primary {
  background-color: var(--ch-color-accent);
  color: var(--ch-color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background-color: var(--ch-color-accent-hover);
}

.btn-danger {
  background-color: var(--ch-color-danger-strong);
  color: var(--ch-color-text-inverse);
}

.btn-danger:hover:not(:disabled) {
  background-color: var(--ch-color-danger-hover);
}

@media (max-width: 720px) {
  .modal {
    min-width: 0;
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 16px;
  }

  .env-manage-body {
    flex-direction: column;
  }

  .env-manage-sidebar {
    width: 100%;
    max-height: 160px;
  }

  .env-textarea-large {
    min-height: 160px;
  }
}
</style>
