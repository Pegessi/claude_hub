<template>
  <div
    v-if="showResidentAgentModal"
    class="workspace-modal-overlay resident-agent-modal-overlay"
    @click.self="closeResidentAgentModal"
  >
    <div class="workspace-modal resident-agent-modal">
      <h3>Resident Agent</h3>
      <div class="modal-field resident-agent-section--first">
        <label class="checkbox-label">
          <input
            v-model="workspaceForm.resident_agent_enabled"
            type="checkbox"
          >
          Enable resident self-driven agent
        </label>
        <p class="modal-hint">
          Wakes every interval on its own — even when idle, with no task
          running — to maintain lessons and propose follow-up tasks. It
          never picks up normal workspace tasks.
        </p>
      </div>

      <!--
        Everything below stays visible at all times; it is disabled/grayed
        while Enable is unchecked (per the requested UX) instead of hidden.
        The same elements and components as the Add-Agent form are reused so
        the two interfaces match, but the role is fixed to resident.
      -->
      <fieldset
        class="resident-config-body"
        :disabled="!workspaceForm.resident_agent_enabled"
        :class="{ 'is-disabled': !workspaceForm.resident_agent_enabled }"
      >
        <div class="modal-field">
          <label class="checkbox-label">
            <input
              v-model="workspaceForm.resident_agent_paused"
              type="checkbox"
            >
            Pause auto-scheduling (keep the session for manual chat)
          </label>
          <p class="modal-hint">
            The resident session stays available for manual chat but won't
            auto-run on its schedule.
          </p>
        </div>
        <div class="modal-field">
          <label class="checkbox-label">
            <input
              v-model="workspaceForm.resident_agent_master_mode"
              type="checkbox"
            >
            Autopilot mode
          </label>
          <p class="modal-hint">
            Changes what each cycle does, not whether it runs. On: the
            resident drives the workspace on its own — it reviews the board,
            creates and dispatches tasks to your existing worker agents, lets
            them go through review, and accepts the finished work itself. It
            never writes code and never adds or removes worker agents. Off:
            read-only maintenance, no reports.
          </p>
        </div>
        <div class="modal-field">
          <label>Run interval (minutes)</label>
          <input
            v-model.number="workspaceForm.resident_agent_interval_minutes"
            type="number"
            min="1"
            placeholder="60"
          >
        </div>
        <div class="modal-field">
          <label>Resident agent directive (optional)</label>
          <textarea
            v-model="workspaceForm.resident_agent_directive"
            rows="3"
            placeholder="A one-shot guiding instruction for the resident (e.g. 'focus on tightening test coverage this week')."
          />
          <p class="modal-hint">
            Saving applies on the next scheduled cycle. To apply a changed
            directive immediately, use "Save &amp; run now" below.
          </p>
        </div>
        <div class="modal-field">
          <label>
            Recurring tasks
            <span
              v-if="enabledPeriodicTaskCount > 0"
              class="modal-label-badge"
            >{{ enabledPeriodicTaskCount }} active</span>
          </label>
          <p class="modal-hint">
            The resident runs every enabled task on each wake-up (in addition
            to its built-in maintenance). Disable a row to keep it without
            running it; the directive above is a separate one-shot focus.
          </p>
          <ul
            v-if="workspaceForm.resident_agent_periodic_tasks.length > 0"
            class="periodic-task-list"
          >
            <li
              v-for="task in workspaceForm.resident_agent_periodic_tasks"
              :key="task.id"
              class="periodic-task-row"
            >
              <input
                v-model="task.enabled"
                type="checkbox"
                class="periodic-task-enable"
                :title="task.enabled ? 'Enabled — runs every cycle' : 'Disabled — kept but not run'"
              >
              <input
                v-model="task.text"
                class="periodic-task-text"
                :class="{ 'periodic-task-text--disabled': !task.enabled }"
                placeholder="e.g. run the linter and open a task for any new warnings"
              >
              <button
                type="button"
                class="periodic-task-remove"
                title="Remove this recurring task"
                @click="removePeriodicTask(task.id)"
              >
                ✕
              </button>
            </li>
          </ul>
          <p
            v-else
            class="modal-hint periodic-task-empty"
          >
            No recurring tasks yet.
          </p>
          <button
            type="button"
            class="tool-button periodic-task-add"
            @click="addPeriodicTask"
          >
            + Add recurring task
          </button>
        </div>
        <div class="modal-field">
          <label>Title</label>
          <input
            v-model="workspaceForm.resident_agent_title"
            placeholder="Workspace Resident"
          >
        </div>
        <AgentConfigFields
          v-model:agent-type="workspaceForm.resident_agent_type"
          v-model:solo-mode="workspaceForm.resident_agent_solo_mode"
          v-model:env-preset="workspaceForm.resident_env_preset"
          v-model:env-text="workspaceForm.resident_env_text"
          :exclude-types="['traex']"
          variant="modal"
          :disabled="!workspaceForm.resident_agent_enabled"
        />
        <div class="modal-field">
          <label>Run On</label>
          <div class="segmented-control">
            <button
              type="button"
              :class="['segment-button', { active: workspaceForm.resident_agent_target === 'local' }]"
              :disabled="!workspaceForm.resident_agent_enabled"
              @click="handleResidentTargetChange('local')"
            >
              Local
            </button>
            <button
              type="button"
              :class="['segment-button', { active: workspaceForm.resident_agent_target === 'remote' }]"
              :disabled="!workspaceForm.resident_agent_enabled"
              @click="handleResidentTargetChange('remote')"
            >
              Remote
            </button>
          </div>
        </div>
        <div
          v-if="workspaceForm.resident_agent_target === 'remote'"
          class="modal-field"
        >
          <label>Remote Server</label>
          <select
            v-model="workspaceForm.resident_agent_remote_profile_id"
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
            v-if="!remoteInteractive(remoteProfiles.find(profile => profile.id === workspaceForm.resident_agent_remote_profile_id))"
            class="modal-hint"
          >
            Browse-only alias: resident agents need an interactive host (normal or PTY gateway).
          </p>
          <p
            v-else-if="remoteProfiles.length === 0"
            class="modal-hint"
          >
            Add profiles in ~/.claude_hub/remote_profiles.json or ~/.ssh/config
          </p>
        </div>
        <div
          v-if="workspaceForm.resident_agent_target === 'remote'"
          class="modal-field"
        >
          <label class="checkbox-label">
            <input
              v-model="workspaceForm.resident_agent_remote_reconnect"
              type="checkbox"
            >
            Auto reconnect
          </label>
        </div>
      </fieldset>

      <!--
        Resident lifecycle buttons. In EDIT mode these act immediately via
        PATCH (Create=enabled:true, Pause/Resume=paused toggle,
        Delete=enabled:false resident-only teardown — never the workspace).
        In CREATE mode there is no workspace id yet, so the three lifecycle
        buttons are disabled and a hint explains the resident is created with
        the workspace via the parent "Create workspace" button. Done dismisses
        the sub-modal and returns to the parent workspace form.
      -->
      <p
        v-if="isResidentCreateMode"
        class="modal-hint"
      >
        The resident is configured here and created together with the
        workspace when you press "Create workspace".
      </p>
      <div class="modal-actions">
        <button
          type="button"
          class="tool-button"
          @click="closeResidentAgentModal"
        >
          Done
        </button>
        <LoadingButton
          type="button"
          class="danger-button workspace-delete-button"
          :disabled="isResidentCreateMode || !residentExists"
          :loading="isPending('resident:delete')"
          loading-label="Deleting"
          @click="handleDeleteResident"
        >
          Delete
        </LoadingButton>
        <LoadingButton
          type="button"
          class="tool-button"
          :disabled="isResidentCreateMode || !residentExists"
          :loading="isPending('resident:pause')"
          :loading-label="(activeWorkspace?.resident_agent_paused ?? false) ? 'Resuming resident' : 'Pausing resident'"
          @click="handleToggleResidentPause"
        >
          {{ (activeWorkspace?.resident_agent_paused ?? false) ? 'Resume' : 'Pause' }}
        </LoadingButton>
        <LoadingButton
          type="button"
          class="primary-button"
          :disabled="isResidentCreateMode || residentExists"
          :loading="isPending('resident:create')"
          loading-label="Creating"
          @click="handleCreateResident"
        >
          Create
        </LoadingButton>
        <LoadingButton
          type="button"
          class="primary-button"
          :disabled="isResidentCreateMode || !residentExists"
          :title="isResidentCreateMode || !residentExists
            ? 'Create the resident first'
            : 'Save the directive and recurring tasks, then run this cycle immediately'"
          :loading="isPending('resident:save-run')"
          loading-label="Saving & running"
          @click="handleSaveResidentAndRunNow"
        >
          Save &amp; run now
        </LoadingButton>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AgentConfigFields from '@/components/AgentConfigFields.vue'
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
  showResidentAgentModal,
  closeResidentAgentModal,
  workspaceForm,
  enabledPeriodicTaskCount,
  removePeriodicTask,
  addPeriodicTask,
  handleResidentTargetChange,
  remoteProfilesLoading,
  remoteProfiles,
  isResidentCreateMode,
  residentExists,
  handleDeleteResident,
  handleToggleResidentPause,
  handleCreateResident,
  handleSaveResidentAndRunNow,
} = useWorkspaceEditorContext()
const {
  isPending,
  activeWorkspace,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-board-layout.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped>
.modal-label-badge {
  margin-left: 8px;
  border-radius: 999px;
  padding: 1px 8px;
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
}
</style>

<style scoped>
.periodic-task-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.periodic-task-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.periodic-task-enable {
  flex: 0 0 auto;
  cursor: pointer;
  width: 16px;
  height: 16px;
  accent-color: var(--ch-color-accent);

  /* Reset default browser outline so the focus ring does not stretch to row
     height; we draw a tidy ring on :focus-visible instead. */
  outline: none;
}

.periodic-task-enable:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: 2px;
}

.periodic-task-text {
  width: 100%;
}

.periodic-task-text--disabled {
  opacity: 0.55;

  /* Only strike through real text; empty rows showing placeholder should not
     look "deleted" — the faded opacity is already enough to convey disabled.
     We apply line-through via a wrapper-less trick by using ::placeholder
     normalization: browsers do not let text-decoration reach the placeholder
     unless the input has value, so we key the strike-through off an
     :not(:placeholder-shown) selector. Since every row shares the same
     placeholder, this reliably means "the user typed something". */
}

.periodic-task-text--disabled:not(:placeholder-shown) {
  text-decoration: line-through;
}

.periodic-task-text--disabled::placeholder {
  text-decoration: none;
  opacity: 0.6;
}

.periodic-task-remove {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
}

.periodic-task-remove:hover {
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.periodic-task-empty {
  font-style: italic;
}

.periodic-task-add {
  margin-top: 8px;
}
</style>

<style scoped src="../styles/shared-env-editor.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped>
.resident-agent-modal-overlay {
  z-index: 1050;
}

.resident-agent-modal {
  display: flex;
  flex-direction: column;

  /* Fixed target height clamped to the viewport: the modal does not
     shrink-to-fit when Enable is unchecked vs checked. Width stays the
     inherited min(520px, 100%) from .workspace-modal. */
  height: min(720px, calc(100dvh - 32px));
  overflow: hidden;
}

.resident-agent-modal > h3 {
  flex-shrink: 0;
}

.resident-agent-modal .resident-agent-section--first {
  flex-shrink: 0;
}

.resident-agent-modal .resident-config-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.resident-agent-modal .modal-actions {
  flex-shrink: 0;
  margin-top: 16px;
}

.resident-config-body {
  min-width: 0;
  margin: 0;

  /* Small right padding so the internal scrollbar doesn't overlap inputs. */
  padding: 2px 4px 2px 0;
  border: 0;
}

.resident-config-body.is-disabled {
  opacity: 0.55;
}

.resident-config-body.is-disabled :deep(*) {
  cursor: not-allowed;
}

.resident-agent-section--first {
  margin-top: 0;
  padding-top: 0;
  border-top: 0;
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>
