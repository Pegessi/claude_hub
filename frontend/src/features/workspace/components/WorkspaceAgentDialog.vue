<template>
  <div
    v-if="showAgentOptionsModal"
    class="workspace-modal-overlay"
    @click.self="closeAgentOptionsModal"
  >
    <div class="workspace-modal agent-manager-modal">
      <h3>Manage Agents</h3>
      <section class="modal-section modal-section--first">
        <div class="modal-section-header">
          <h4>Workspace Agents</h4>
          <div
            class="agent-manager-view-switch"
            aria-label="Workspace agents view"
          >
            <button
              type="button"
              :data-active="agentManagerView === 'agents'"
              @click="agentManagerView = 'agents'"
            >
              <span>Agents</span>
              <strong>{{ workspaceAgents.length + (dispatcherAgent ? 1 : 0) + (residentAgent ? 1 : 0) }}</strong>
            </button>
            <button
              type="button"
              :data-active="agentManagerView === 'reviewers'"
              @click="agentManagerView = 'reviewers'"
            >
              <span>Reviewers</span>
              <strong>{{ reviewerSessions.length }}</strong>
            </button>
          </div>
        </div>
        <div class="agent-list">
          <article
            v-for="agent in agentManagerSessions"
            :key="agent.id"
            :class="['agent-row', { 'dispatcher-row': agent.role === 'dispatcher' }]"
          >
            <div>
              <strong>{{ agent.title }}</strong>
              <span>{{ agentRoleLabel(agent) }} · {{ agent.agent_type }} · {{ agent.session_kind }} · {{ agent.id }}</span>
              <span>{{ agent.target }} · {{ agent.workspace_path }}</span>
            </div>
            <div class="agent-row-meta">
              <span :class="['runtime-pill', `runtime-pill--${agent.runtime_status}`]">
                {{ agent.runtime_status }}
              </span>
              <span
                v-if="isResidentAgent(agent) && isResidentPaused"
                class="runtime-pill runtime-pill--paused"
              >paused</span>
              <span
                v-if="isResidentAgent(agent) && isResidentMaster"
                class="runtime-pill runtime-pill--paused"
              >autopilot</span>
              <span v-if="agent.ephemeral">temporary</span>
              <span>current {{ taskTitle(agent.current_task_id) }}</span>
              <span>queued {{ agent.queued_count }}</span>
            </div>
            <div class="agent-row-actions">
              <LoadingButton
                type="button"
                :loading="isPending(sessionActionKey('open', agent.id))"
                loading-label="Opening agent"
                @click="openSession(agent)"
              >
                Open
              </LoadingButton>
              <LoadingButton
                v-if="canSwitchAgentEnv(agent)"
                type="button"
                title="Switch Env / Model"
                :loading="isPending(sessionActionKey('switch-env', agent.id))"
                loading-label="Switching env"
                @click="openSwitchEnvModal(agent)"
              >
                ⚙ Env
              </LoadingButton>
              <LoadingButton
                v-if="isResidentAgent(agent)"
                type="button"
                :loading="isPending('workspace:resident-pause')"
                :loading-label="isResidentPaused ? 'Resuming agent' : 'Pausing agent'"
                @click="toggleResidentPaused"
              >
                {{ isResidentPaused ? 'Resume' : 'Pause' }}
              </LoadingButton>
              <LoadingButton
                v-if="isResidentAgent(agent)"
                type="button"
                :disabled="residentRunPending"
                :title="residentRunPending
                  ? 'A run is already queued for the next monitor tick'
                  : 'Run the resident now using its saved directive and periodic tasks'"
                :loading="isPending('resident:run')"
                loading-label="Queuing run"
                @click="handleRunResidentNow"
              >
                {{ residentRunPending ? 'Run queued' : 'Run now' }}
              </LoadingButton>
              <LoadingButton
                v-if="agent.role !== 'dispatcher'"
                type="button"
                class="danger-button"
                :disabled="!canDeleteAgent(agent)"
                :title="agentDeleteTitle(agent)"
                :loading="isPending(agentActionKey('delete', agent.id))"
                loading-label="Deleting agent"
                @click="deleteAgent(agent)"
              >
                Delete
              </LoadingButton>
            </div>
          </article>
          <div
            v-if="agentManagerSessions.length === 0"
            class="empty-inline"
          >
            {{ agentManagerEmptyText }}
          </div>
        </div>
      </section>

      <form
        class="modal-section agent-create-form"
        @submit.prevent="handleCreateAdvancedAgent"
      >
        <div class="modal-section-header">
          <h4>Add Agent</h4>
        </div>
        <div class="modal-field">
          <label>Title</label>
          <input
            v-model="agentOptionsForm.title"
            placeholder="Workspace Agent"
          >
        </div>

        <div class="modal-field-row">
          <div class="modal-field">
            <label>Role</label>
            <select v-model="agentOptionsForm.role">
              <option value="orchestrator">
                Agent
              </option>
              <option value="reviewer">
                Reviewer
              </option>
            </select>
          </div>
        </div>

        <div class="modal-field">
          <label>Execution surface</label>
          <div class="segmented-control">
            <button
              type="button"
              :class="['segment-button', { active: agentOptionsForm.session_kind === 'terminal' }]"
              @click="handleAgentSurfaceChange('terminal')"
            >
              Terminal
            </button>
            <button
              type="button"
              :disabled="agentOptionsForm.target === 'remote'"
              :class="['segment-button', { active: agentOptionsForm.session_kind === 'chat' }]"
              title="Managed Chat is available for local agents"
              @click="handleAgentSurfaceChange('chat')"
            >
              Chat
            </button>
          </div>
          <p class="modal-hint">
            {{ agentOptionsForm.target === 'remote'
              ? 'Remote agents use the Terminal surface.'
              : agentOptionsForm.session_kind === 'chat'
                ? 'Chat opens structured managed history and sends through the Workspace mailbox.'
                : 'Terminal opens the provider’s native CLI.' }}
          </p>
        </div>

        <AgentConfigFields
          v-model:agent-type="agentOptionsForm.agent_type"
          v-model:solo-mode="agentOptionsForm.solo_mode"
          v-model:env-preset="agentOptionsForm.env_preset"
          v-model:env-text="agentOptionsForm.env_text"
          :allow-maso="agentOptionsForm.session_kind === 'chat'"
          :allow-antigravity="agentOptionsForm.session_kind === 'chat' && agentOptionsForm.target === 'local'"
          :allow-terminal="agentOptionsForm.session_kind === 'terminal'"
          :exclude-types="['traex']"
          variant="modal"
        />

        <div class="modal-field">
          <label>Run On</label>
          <div class="segmented-control">
            <button
              type="button"
              :class="['segment-button', { active: agentOptionsForm.target === 'local' }]"
              @click="handleAgentTargetChange('local')"
            >
              Local
            </button>
            <button
              type="button"
              :class="['segment-button', { active: agentOptionsForm.target === 'remote' }]"
              @click="handleAgentTargetChange('remote')"
            >
              Remote
            </button>
          </div>
          <p
            v-if="activeWorkspace?.target === 'remote' && agentOptionsForm.target === 'local'"
            class="modal-hint"
          >
            This workspace is remote. New agents stay local unless you switch Run On to Remote.
          </p>
          <p
            v-else-if="activeWorkspace?.target === 'local' && agentOptionsForm.target === 'remote'"
            class="modal-hint"
          >
            This agent will SSH to the selected server. Auto-review follows this agent, not the workspace default.
          </p>
        </div>

        <div
          v-if="agentOptionsForm.target === 'remote'"
          class="modal-field"
        >
          <label>Remote Server</label>
          <select
            v-model="agentOptionsForm.remote_profile_id"
            :disabled="remoteProfilesLoading"
          >
            <option value="">
              Select server
            </option>
            <option
              v-for="profile in remoteProfiles"
              :key="profile.id"
              :value="profile.id"
            >
              {{ profile.name }}{{ remoteProfileBadge(profile) }}
            </option>
          </select>
          <p
            v-if="!remoteInteractive(selectedAgentRemoteProfile)"
            class="modal-hint"
          >
            Browse-only alias. Pick an interactive host (normal or PTY gateway such as merlin_dev) for remote agents.
          </p>
          <p
            v-else-if="remoteProfiles.length === 0"
            class="modal-hint"
          >
            Add profiles in ~/.claude_hub/remote_profiles.json or ~/.ssh/config
          </p>
        </div>

        <div class="modal-field">
          <label>Working Directory</label>
          <div class="path-input-row">
            <input
              v-model="agentOptionsForm.cwd"
              :placeholder="agentOptionsForm.target === 'remote' ? '~/workspace/project' : '/Users/me/workspace'"
            >
            <LoadingButton
              type="button"
              class="tool-button"
              :disabled="agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id"
              :loading="isPending('agent-browser:open')"
              loading-label="Opening browser"
              @click="openAgentDirectoryBrowser"
            >
              Browse
            </LoadingButton>
          </div>
        </div>

        <div
          v-if="agentOptionsForm.target === 'remote'"
          class="modal-field"
        >
          <label class="checkbox-label">
            <input
              v-model="agentOptionsForm.remote_reconnect"
              type="checkbox"
            >
            Auto reconnect
          </label>
        </div>

        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="closeAgentOptionsModal"
          >
            Cancel
          </button>
          <LoadingButton
            type="submit"
            class="primary-button"
            :disabled="isAgentOptionsCreateDisabled"
            :loading="isPending('agent:create')"
            loading-label="Creating agent"
          >
            Create agent
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import AgentConfigFields from '@/components/AgentConfigFields.vue'
import {
  remoteProfileBadge,
  remoteInteractive,
} from '@/utils/remoteProfiles'
import {
  useWorkspaceAgentManagerContext,
  useWorkspaceRuntimeContext,
  useWorkspaceEditorContext,
  useWorkspaceDirectoryBrowserContext,
} from '../context'
const {
  showAgentOptionsModal,
  closeAgentOptionsModal,
  agentManagerView,
  agentManagerSessions,
  openSwitchEnvModal,
  agentManagerEmptyText,
  handleCreateAdvancedAgent,
  agentOptionsForm,
  handleAgentSurfaceChange,
  handleAgentTargetChange,
  selectedAgentRemoteProfile,
  isAgentOptionsCreateDisabled,
} = useWorkspaceAgentManagerContext()
const {
  workspaceAgents,
  dispatcherAgent,
  residentAgent,
  reviewerSessions,
  agentRoleLabel,
  taskTitle,
  isPending,
  sessionActionKey,
  openSession,
  canSwitchAgentEnv,
  canDeleteAgent,
  agentDeleteTitle,
  agentActionKey,
  deleteAgent,
  activeWorkspace,
} = useWorkspaceRuntimeContext()
const {
  isResidentAgent,
  isResidentPaused,
  isResidentMaster,
  toggleResidentPaused,
  residentRunPending,
  handleRunResidentNow,
  remoteProfilesLoading,
  remoteProfiles,
} = useWorkspaceEditorContext()
const {
  openAgentDirectoryBrowser,
} = useWorkspaceDirectoryBrowserContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped>
.agent-submit-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.compact-button {
  flex: 0 0 auto;
}
</style>

<style scoped src="../styles/shared-agent-manager.css"></style>

<style scoped>
.agent-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-manager-view-switch {
  display: flex;
  gap: 6px;
}

.agent-manager-view-switch button {
  height: 32px;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  padding: 0 12px;
  text-align: left;
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
  transition: all var(--ch-motion-fast);
}

.agent-manager-view-switch button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.agent-manager-view-switch button[data-active='true'] {
  border-color: var(--ch-color-border-strong);
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.agent-manager-view-switch span {
  font-size: 12px;
  font-weight: 700;
}

.agent-manager-view-switch strong {
  min-width: 20px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: currentColor;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}

.agent-row {
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 9px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-row strong {
  display: block;
  color: var(--ch-color-text-strong);
  font-size: 12px;
}

.agent-row span {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.dispatcher-row {
  border-color: var(--ch-color-discovery-border);
  background: var(--ch-color-discovery-bg);
}

.runtime-pill {
  border-radius: 999px;
  padding: 3px 7px;
  background: var(--ch-color-border-strong);
  color: var(--ch-color-text);
}

.runtime-pill--idle {
  background: var(--ch-color-success-bg);
  color: var(--ch-color-success);
}

.runtime-pill--working {
  background: var(--ch-color-warning-bg);
  color: var(--ch-color-warning);
}

.runtime-pill--attention {
  background: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.runtime-pill--offline {
  background: var(--ch-color-surface-muted);
  color: var(--ch-color-text-muted);
}

.runtime-pill--paused {
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
}
</style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped>
.agent-manager-modal {
  width: min(720px, 100%);
  display: flex;
  flex-direction: column;

  /* Keep the whole modal within a single viewport height. */
  max-height: calc(100dvh - 32px);
  overflow: hidden;
}

.agent-manager-modal > h3 {
  flex-shrink: 0;
  margin-bottom: 12px;
}

.agent-manager-modal .modal-section--first {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.agent-manager-modal .modal-section--first .modal-section-header {
  flex-shrink: 0;
}

.agent-manager-modal .modal-section--first .agent-list {
  flex: 1;
  min-height: 80px;
  overflow-y: auto;
}

.agent-manager-modal .agent-create-form {
  flex: 0 1 auto;
  min-height: 0;
  overflow-y: auto;
  margin-top: 14px;
  padding-top: 12px;
}

.agent-manager-modal .modal-field-row {
  display: flex;
  gap: 12px;
}

.agent-manager-modal .modal-field-row .modal-field {
  flex: 1 1 0;
  min-width: 0;
}

.agent-manager-modal .agent-create-form .modal-field {
  margin-bottom: 10px;
}
</style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-env-editor.css"></style>

<style scoped>
.env-template-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-muted);
  padding: 10px;
}

.env-preset-name {
  width: 100%;
}

.modal-field .env-textarea-preview {
  margin-top: 8px;
  width: 100%;
  resize: none;
  min-height: 60px;
  opacity: 0.75;
  cursor: default;
  white-space: pre-wrap;
  font-family: var(--ch-font-mono) !important;
}

.env-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.path-input-row {
  display: flex;
  gap: 8px;
}

.path-input-row input {
  min-width: 0;
  flex: 1;
}

.path-input-row .tool-button {
  height: 34px;
}
</style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .agent-manager-modal .modal-field-row {
    flex-direction: column;
    gap: 0;
  }

  .agent-row-meta,
  .agent-row-actions {
    flex-wrap: wrap;
  }
}
</style>
