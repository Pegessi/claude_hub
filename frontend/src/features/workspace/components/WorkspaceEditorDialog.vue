<template>
  <div
    v-if="showWorkspaceModal"
    class="workspace-modal-overlay"
    @click.self="closeWorkspaceModal"
  >
    <div class="workspace-modal">
      <h3>{{ workspaceModalMode === 'edit' ? 'Edit Workspace' : 'Create Workspace' }}</h3>
      <form @submit.prevent="handleSubmitWorkspace">
        <div class="modal-field">
          <label>Name</label>
          <input
            v-model="workspaceForm.name"
            placeholder="claude_hub"
            autofocus
          >
        </div>
        <div class="modal-field">
          <label>Local workspace dir</label>
          <input
            v-model="workspaceForm.path"
            placeholder="/Users/me/workspace"
          >
          <p class="modal-hint">
            Used as the default working directory for new agents in this workspace.
          </p>
        </div>
        <div class="modal-field">
          <label>Environment</label>
          <div class="segmented-control">
            <button
              type="button"
              :class="['segment-button', { active: workspaceForm.target === 'local' }]"
              :disabled="workspaceModalMode === 'edit'"
              @click="workspaceForm.target = 'local'"
            >
              Local
            </button>
            <button
              type="button"
              :class="['segment-button', { active: workspaceForm.target === 'remote' }]"
              :disabled="workspaceModalMode === 'edit'"
              @click="workspaceForm.target = 'remote'"
            >
              Remote
            </button>
          </div>
        </div>
        <template v-if="workspaceForm.target === 'remote'">
          <div class="modal-field">
            <label>Remote profile</label>
            <select
              v-model="workspaceForm.remote_profile_id"
              :disabled="remoteProfilesLoading || workspaceModalMode === 'edit'"
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
              v-if="!remoteInteractive(selectedRemoteProfile)"
              class="modal-hint"
            >
              Browse-only alias: new remote agents need an interactive host (normal or PTY gateway).
            </p>
          </div>
          <div class="modal-field">
            <label>Remote start dir</label>
            <input
              v-model="workspaceForm.remote_cwd"
              placeholder="~"
            >
            <p class="modal-hint">
              Used as the default remote working directory for new agents.
            </p>
          </div>
          <div class="modal-field">
            <label class="checkbox-label">
              <input
                v-model="workspaceForm.remote_reconnect"
                type="checkbox"
              >
              Reconnect
            </label>
          </div>
        </template>
        <div class="form-row">
          <div class="modal-field">
            <label>Branch hint</label>
            <input
              v-model="workspaceForm.default_branch"
              placeholder="main"
            >
          </div>
          <div class="modal-field">
            <label>Prefix</label>
            <input
              v-model="workspaceForm.session_prefix"
              placeholder="chub"
              :disabled="workspaceModalMode === 'edit'"
            >
          </div>
        </div>
        <div class="modal-field resident-agent-section">
          <div class="resident-summary-row">
            <div class="resident-summary-text">
              <span class="resident-summary-title">Resident self-driven agent</span>
              <span
                :class="['resident-summary-status', workspaceForm.resident_agent_enabled ? 'is-on' : 'is-off']"
              >
                {{ residentSummaryLabel }}
              </span>
            </div>
            <button
              type="button"
              class="tool-button"
              @click="openResidentAgentModal"
            >
              Configure…
            </button>
          </div>
          <p class="modal-hint">
            An optional background agent that runs on a schedule to maintain
            lessons and propose follow-up tasks for this workspace.
          </p>
        </div>
        <div class="modal-actions">
          <button
            v-if="workspaceModalMode === 'edit'"
            type="button"
            class="danger-button workspace-delete-button"
            :disabled="isLoading || isPending('workspace:delete')"
            @click="handleDeleteWorkspace"
          >
            Delete Workspace
          </button>
          <button
            type="button"
            class="tool-button"
            @click="closeWorkspaceModal"
          >
            Cancel
          </button>
          <LoadingButton
            type="submit"
            class="primary-button"
            :disabled="isLoading || (workspaceForm.target === 'remote' && !workspaceForm.remote_profile_id)"
            :loading="isPending(workspaceModalMode === 'edit' ? 'workspace:update' : 'workspace:create')"
            :loading-label="workspaceModalMode === 'edit' ? 'Saving workspace' : 'Creating workspace'"
          >
            {{ workspaceModalMode === 'edit' ? 'Save workspace' : 'Create workspace' }}
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  remoteProfileBadge,
  remoteInteractive,
} from '@/utils/remoteProfiles'
import {
  useWorkspaceEditorContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  showWorkspaceModal,
  closeWorkspaceModal,
  workspaceModalMode,
  handleSubmitWorkspace,
  workspaceForm,
  remoteProfilesLoading,
  remoteProfiles,
  selectedRemoteProfile,
  residentSummaryLabel,
  openResidentAgentModal,
  handleDeleteWorkspace,
} = useWorkspaceEditorContext()
const {
  isLoading,
  isPending,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-env-editor.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped>
.resident-agent-section {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--ch-color-border);
}

.resident-summary-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.resident-summary-text {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.resident-summary-title {
  color: var(--ch-color-text);
  font-size: 14px;
  font-weight: 500;
}

.resident-summary-status {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--ch-color-border);
  white-space: nowrap;
}

.resident-summary-status.is-on {
  color: var(--ch-color-accent);
  border-color: var(--ch-color-accent-ring);
}

.resident-summary-status.is-off {
  color: var(--ch-color-text-soft);
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>
