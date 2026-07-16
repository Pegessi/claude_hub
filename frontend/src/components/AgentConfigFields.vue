<template>
  <!--
    Reusable agent-configuration inputs shared by every place that launches an
    agent: Add-Agent modal, the TabBar launcher, and the resident self-driven
    agent config. Renders the agent-type selector, the YOLO/solo toggle (only
    for agent types that support it, with a type-aware hint), and the
    environment-preset picker + Manage button (owns the EnvPresetManager modal).

    Public interface (discrete v-models so parents can keep a single reactive
    form object):
      v-model:agent-type   AgentType
      v-model:solo-mode     boolean
      v-model:env-preset    string  (preset id; localStorage-scoped only)
      v-model:env-text      string  (resolved KEY=VALUE text, source of truth)
    Props:
      variant      'modal' (AgentWorkspaceView styles) | 'form' (TabBar styles)
      allowTerminal  show the Terminal agent-type option (default true)
      soloLabel    label for the solo toggle (default 'YOLO mode')
  -->
  <div :class="['agent-config-fields', `agent-config-fields--${variant}`, { 'agent-config-fields--disabled': disabled }]">
    <div :class="['acf-field', fieldClass]">
      <label
        class="acf-label"
        :for="`${uid}-agent-type`"
      >Agent Type</label>
      <select
        :id="`${uid}-agent-type`"
        :class="['acf-select', selectClass]"
        :value="agentType"
        :disabled="disabled"
        @change="onAgentTypeChange(($event.target as HTMLSelectElement).value)"
      >
        <option value="claude">
          Claude
        </option>
        <option value="codex">
          Codex
        </option>
        <option value="cursor">
          Cursor
        </option>
        <option
          v-if="allowTerminal"
          value="terminal"
        >
          Terminal
        </option>
      </select>
    </div>

    <div
      v-if="supportsSoloMode"
      :class="['acf-field', fieldClass]"
    >
      <label class="acf-checkbox-label checkbox-label">
        <input
          type="checkbox"
          :class="['acf-checkbox', checkboxClass]"
          :checked="soloMode"
          :disabled="disabled"
          @change="emit('update:soloMode', ($event.target as HTMLInputElement).checked)"
        >
        <span>{{ soloLabel }}</span>
      </label>
      <p :class="['acf-hint', hintClass]">
        {{ yoloHint }}
      </p>
    </div>

    <div :class="['acf-field', fieldClass, 'env-editor']">
      <label class="acf-label">Environment Preset</label>
      <div class="env-preset-row">
        <select
          :class="['acf-select', selectClass]"
          :value="envPreset"
          :disabled="disabled"
          @change="onPresetChange(($event.target as HTMLSelectElement).value)"
        >
          <option
            v-for="preset in envPresets"
            :key="preset.id"
            :value="preset.id"
          >
            {{ preset.name }}
          </option>
        </select>
        <button
          type="button"
          :class="manageButtonClass"
          :disabled="disabled"
          @click="openEnvPresetManager"
        >
          Manage
        </button>
      </div>
      <p :class="['acf-hint', hintClass]">
        Pick a preset for this launch. Click "Manage" to create, edit, or delete
        presets. Values are not printed in logs.
      </p>
    </div>

    <EnvPresetManager
      :model-value="envPreset"
      :visible="showEnvManager"
      @update:model-value="onPresetChange"
      @close="closeEnvPresetManager"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import {
  defaultLaunchEnvPresetForAgent,
  useLaunchEnvPresets,
} from '@/composables/useLaunchEnvPresets'
import type { AgentType } from '@/types'

interface Props {
  agentType: AgentType
  soloMode: boolean
  envPreset: string
  envText: string
  variant?: 'modal' | 'form'
  allowTerminal?: boolean
  soloLabel?: string
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'modal',
  allowTerminal: true,
  soloLabel: 'YOLO mode',
  disabled: false,
})

const emit = defineEmits<{
  (e: 'update:agentType', value: AgentType): void
  (e: 'update:soloMode', value: boolean): void
  (e: 'update:envPreset', value: string): void
  (e: 'update:envText', value: string): void
}>()

const { envPresets, getPresetText, defaultPresetTextForAgent } =
  useLaunchEnvPresets()

const uid = `acf-${Math.random().toString(36).slice(2, 9)}`
const showEnvManager = ref(false)

const supportsSoloMode = computed(
  () => props.agentType !== 'cursor' && props.agentType !== 'terminal'
)

const yoloHint = computed(() => {
  if (props.agentType === 'codex') {
    return 'Runs Codex with --ask-for-approval never and --sandbox danger-full-access'
  }
  return 'Runs Claude with IS_SANDBOX=1 and --dangerously-skip-permissions'
})

// Variant-specific class wiring so the component renders consistently inside
// both the AgentWorkspaceView modal and the TabBar form.
const fieldClass = computed(() =>
  props.variant === 'form' ? 'form-group' : 'modal-field'
)
const selectClass = computed(() =>
  props.variant === 'form' ? 'select-input' : ''
)
const checkboxClass = computed(() =>
  props.variant === 'form' ? 'checkbox-input' : ''
)
const hintClass = computed(() =>
  props.variant === 'form' ? 'form-hint' : 'modal-hint'
)
const manageButtonClass = computed(() =>
  props.variant === 'form'
    ? 'btn btn-secondary env-manage-button'
    : 'tool-button env-manage-button'
)

function applyEnvPreset(presetId: string) {
  const text = getPresetText(presetId)
  if (text === null) return
  emit('update:envText', text)
}

function onPresetChange(presetId: string) {
  if (presetId !== props.envPreset) {
    emit('update:envPreset', presetId)
  }
  applyEnvPreset(presetId)
}

function openEnvPresetManager() {
  if (props.disabled) return
  showEnvManager.value = true
}

function closeEnvPresetManager() {
  showEnvManager.value = false
  // Re-sync env_text in case the active preset was edited in the manager.
  applyEnvPreset(props.envPreset)
}

function onAgentTypeChange(value: string) {
  const agentType = value as AgentType
  emit('update:agentType', agentType)
  // User-driven type change: reset solo for unsupported types and re-apply the
  // type's default env preset/text. Programmatic prop updates (e.g. populating
  // an existing config) do NOT pass through here, so they are not clobbered.
  if (agentType === 'cursor' || agentType === 'terminal') {
    emit('update:soloMode', false)
  }
  emit('update:envPreset', defaultLaunchEnvPresetForAgent(agentType))
  emit('update:envText', defaultPresetTextForAgent(agentType))
}
</script>

<style scoped>
/*
  Self-contained styling so the component renders correctly REGARDLESS of the
  parent. Vue scoped styles in the parent (AgentWorkspaceView / TabBar) do not
  pierce into this child's inner elements, so the label/select/checkbox/hint are
  styled here using stable component-owned classes (acf-*). The variant classes
  remain on the elements for outer spacing parity but the inner visuals no
  longer depend on parent rules.
*/
.agent-config-fields .acf-field {
  margin-bottom: 14px;
}

/* Grayed-out state when the parent disables the whole block (e.g. the resident
   agent config before "Enable" is checked). Individual controls also receive
   :disabled so they are non-interactive; this just dims the labels/hints. */
.agent-config-fields--disabled {
  opacity: 0.5;
}

.agent-config-fields--disabled .acf-label,
.agent-config-fields--disabled .acf-checkbox-label {
  cursor: not-allowed;
}

.agent-config-fields--form .acf-field {
  margin-bottom: 16px;
}

.agent-config-fields .acf-label {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
}

.agent-config-fields--form .acf-label {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
}

.agent-config-fields .acf-select {
  width: 100%;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  font-family: var(--ch-font-sans);
  box-sizing: border-box;
  cursor: pointer;
  transition: border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
}

.agent-config-fields--form .acf-select {
  height: auto;
  padding: 10px 12px;
  border-radius: var(--ch-radius-md);
}

.agent-config-fields .acf-select:hover {
  border-color: var(--ch-color-border-hover);
}

.agent-config-fields .acf-select:focus,
.agent-config-fields .acf-select:focus-visible {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.agent-config-fields .acf-checkbox-label {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 10px;
  margin-bottom: 0;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  cursor: pointer;
}

.agent-config-fields .acf-checkbox {
  width: 16px;
  height: 16px;
  margin: 0;
  cursor: pointer;
  flex-shrink: 0;
  accent-color: var(--ch-color-accent);
}

.agent-config-fields .acf-hint {
  margin: 6px 0 0;
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-xs);
  line-height: 1.4;
}

.env-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.env-preset-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.env-preset-row select {
  flex: 1;
  min-width: 0;
}

.env-manage-button {
  white-space: nowrap;
  flex-shrink: 0;
}
</style>
