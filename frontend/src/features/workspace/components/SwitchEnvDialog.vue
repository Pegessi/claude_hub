<template>
  <!-- Switch Env Preset Manager (for the Switch Env modal) -->
  <EnvPresetManager
    v-model:model-value="switchEnvForm.env_preset"
    :visible="showSwitchEnvManager"
    @close="closeSwitchEnvPresetManager"
  />

  <!-- Switch Env Modal (for hot-swapping env/solo on running Claude agents) -->
  <div
    v-if="showSwitchEnvModal"
    class="workspace-modal-overlay"
    @click.self="closeSwitchEnvModal"
  >
    <div class="workspace-modal switch-env-modal">
      <div class="switch-env-header">
        <div
          class="switch-env-icon"
          aria-hidden="true"
        >
          ⚙
        </div>
        <div class="switch-env-title-block">
          <h3>Switch Environment</h3>
          <p class="switch-env-subtitle">
            {{ switchEnvAgent?.title }}
          </p>
        </div>
      </div>
      <p class="switch-env-callout">
        <span
          class="switch-env-callout-icon"
          aria-hidden="true"
        >↻</span>
        <span>
          The agent will restart and automatically resume its conversation.
          In-flight generation will be interrupted.
        </span>
      </p>
      <form @submit.prevent="handleSwitchEnv">
        <div class="modal-field env-editor">
          <label>Environment Preset</label>
          <div class="env-preset-row">
            <select
              v-model="switchEnvForm.env_preset"
              @change="applySwitchEnvPreset(switchEnvForm.env_preset)"
            >
              <option
                v-for="preset in envPresets"
                :key="preset.id"
                :value="preset.id"
              >
                {{ preset.name }}
              </option>
              <option value="custom">
                Custom (current values)
              </option>
            </select>
            <button
              type="button"
              class="tool-button env-manage-button"
              @click="openSwitchEnvPresetManager"
            >
              Manage
            </button>
          </div>
        </div>
        <div class="modal-field">
          <label for="wsSwitchEnvText">Environment Variables <span class="field-hint-inline">(KEY=VALUE, one per line)</span></label>
          <textarea
            id="wsSwitchEnvText"
            v-model="switchEnvForm.env_text"
            class="env-textarea"
            rows="6"
            placeholder="ANTHROPIC_MODEL=claude-sonnet-4-5&#10;ANTHROPIC_BASE_URL=https://..."
          />
          <p class="modal-hint">
            These fully replace the agent's current environment. Include
            <code>ANTHROPIC_MODEL</code> to switch models.
          </p>
        </div>
        <div class="modal-field">
          <label class="checkbox-label">
            <input
              v-model="switchEnvForm.solo_mode"
              type="checkbox"
            >
            <span>Solo Mode</span>
          </label>
          <p
            v-if="switchEnvAgent?.agent_type === 'codex'"
            class="modal-hint"
          >
            Relaunch with <code>--ask-for-approval never</code> and
            <code>--sandbox danger-full-access</code>.
          </p>
          <p
            v-else
            class="modal-hint"
          >
            Relaunch with <code>IS_SANDBOX=1</code> and
            <code>--dangerously-skip-permissions</code>.
          </p>
        </div>
        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="closeSwitchEnvModal"
          >
            Cancel
          </button>
          <LoadingButton
            type="submit"
            class="primary-button switch-env-submit"
            :loading="switchEnvAgent ? isPending(sessionActionKey('switch-env', switchEnvAgent.id)) : false"
            loading-label="Restarting…"
          >
            Restart Agent
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import {
  useWorkspaceAgentManagerContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  switchEnvForm,
  showSwitchEnvManager,
  closeSwitchEnvPresetManager,
  showSwitchEnvModal,
  closeSwitchEnvModal,
  switchEnvAgent,
  handleSwitchEnv,
  applySwitchEnvPreset,
  openSwitchEnvPresetManager,
} = useWorkspaceAgentManagerContext()
const {
  envPresets,
  isPending,
  sessionActionKey,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped>
.env-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.env-preset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.env-manage-button {
  white-space: nowrap;
}

.env-textarea {
  width: 100%;
  min-height: 92px;
  resize: vertical;
  font-family: var(--ch-font-mono) !important;
  line-height: 1.45;
}
</style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped src="../styles/shared-switch-env.css"></style>

<style scoped>
.switch-env-modal {
  width: min(480px, 100%);
}

.switch-env-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.switch-env-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 18px;
}

.switch-env-title-block {
  min-width: 0;
}

.switch-env-title-block h3 {
  margin: 0;
  font-size: 16px;
}

.switch-env-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.switch-env-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  border: 1px solid var(--ch-color-border-muted);
  border-left: 3px solid var(--ch-color-accent);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ch-color-text-muted);
}

.switch-env-callout-icon {
  flex: 0 0 auto;
  color: var(--ch-color-accent);
  font-weight: 600;
  line-height: 1.5;
}

.switch-env-submit {
  min-width: 124px;
}

.switch-env-modal .modal-field label code,
.switch-env-modal .modal-hint code {
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-sm);
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}
</style>
