<template>
  <div
    v-if="visible"
    class="workspace-modal-overlay"
    @click.self="close"
  >
    <div class="workspace-modal">
      <h3>Add Task</h3>
      <form
        @submit.prevent="submit"
        @input.capture="onFormMutation"
        @change.capture="onFormMutation"
        @paste="pasteAttachments($event)"
      >
        <p
          v-if="capabilitiesStatus==='loading'||capabilitiesStatus==='idle'"
          class="modal-hint"
          role="status"
        >
          Loading Task execution options…
        </p>
        <div
          v-else-if="capabilitiesStatus==='error'"
          class="modal-hint"
          role="alert"
        >
          <span>{{ capabilitiesError || 'Task execution options could not be loaded.' }}</span>
          <button
            type="button"
            @click="retryCapabilities"
          >
            Retry
          </button>
        </div>
        <p
          v-else-if="capabilitiesStatus==='unsupported'"
          class="modal-hint"
        >
          This Hub supports the legacy Workspace-managed Task form only.
        </p>
        <p
          v-if="pendingAttempt&&capabilitiesStatus==='unsupported'"
          class="modal-hint"
          role="alert"
        >
          This saved request uses the newer Task contract and cannot be sent to this Hub. Discard it to use the legacy form.
        </p>
        <section
          v-if="reporterRecovery"
          class="modal-hint"
          role="status"
        >
          <strong>Task created.</strong>
          <p>The reporter credential could not be saved in session storage. Copy it now or retry local storage. This does not submit the Task again.</p>
          <code v-if="revealReporterKey">{{ reporterRecovery.key }}</code>
          <button
            type="button"
            @click="toggleReporterKey"
          >
            {{ revealReporterKey ? 'Hide' : 'Show' }}
          </button>
          <button
            type="button"
            @click="copyReporterCredential"
          >
            Copy credential
          </button>
          <button
            type="button"
            @click="persistReporterCredential"
          >
            Retry local storage
          </button>
        </section>
        <p
          v-else-if="pendingAttempt && submitting"
          class="modal-hint"
          role="status"
        >
          Creating “{{ pendingAttempt.payload.title }}”…
        </p>
        <section
          v-else-if="pendingAttempt"
          class="task-create-recovery"
          role="status"
        >
          <p class="task-create-recovery-title">
            “{{ pendingAttempt.payload.title }}” was not confirmed
          </p>
          <p class="modal-hint">
            Retry sends the identical saved request, so the Task is never created twice. Discard it to edit a new Task instead.
          </p>
        </section>
        <template v-if="!pendingAttempt && !reporterRecovery">
          <div class="modal-field">
            <label>Execution responsibility</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button',{active:form.execution_control==='workspace'}]"
                @click="discardPendingAttempt(); form.execution_control='workspace'"
              >
                Workspace dispatches agent
              </button>
              <button
                type="button"
                :disabled="capabilitiesStatus !== 'supported' || !capabilities?.supported_execution_controls.includes('initiator')"
                :class="['segment-button',{active:form.execution_control==='initiator'}]"
                @click="discardPendingAttempt(); form.execution_control='initiator'"
              >
                Initiator reports progress
              </button>
            </div>
            <p class="modal-hint">
              Source is recorded separately and never grants permission.
            </p>
          </div>
          <div class="modal-field">
            <label for="task-create-title">Title</label>
            <input
              id="task-create-title"
              v-model="form.title"
              placeholder="Implement a focused change"
              :disabled="!targetAvailable"
              autofocus
            >
          </div>
          <div class="modal-field">
            <label for="task-create-description">Task description</label>
            <textarea
              id="task-create-description"
              v-model="form.prompt"
              placeholder="Describe what the workspace agent should implement..."
              :disabled="!targetAvailable"
            />
            <div
              v-if="form.attachments.length > 0"
              class="attachment-list"
            >
              <div
                v-for="attachment in form.attachments"
                :key="attachment.id"
                class="attachment-row"
              >
                <div class="attachment-thumb">
                  <img
                    :src="attachment.preview_url"
                    :alt="attachment.filename"
                  >
                </div>
                <div class="attachment-meta">
                  <strong>{{ attachment.filename }}</strong>
                  <span>{{ attachment.mime_type }} · {{ formatAttachmentSize(attachment.size_bytes) }}</span>
                </div>
                <button
                  type="button"
                  class="icon-button"
                  aria-label="Remove attachment"
                  @click="removeAttachment(attachment)"
                >
                  x
                </button>
              </div>
            </div>
          </div>
          <p
            v-if="form.execution_control === 'initiator'"
            class="modal-hint"
          >
            These review and autonomy policies are retained for a future Workspace handoff, but do not run while the initiator controls execution.
          </p>
          <div
            v-if="form.execution_control === 'workspace'"
            class="modal-field"
          >
            <label>Execution surface</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: form.session_kind === 'terminal' }]"
                @click="form.session_kind = 'terminal'; form.session_id = ''"
              >
                Terminal
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.session_kind === 'chat' }]"
                @click="form.session_kind = 'chat'; form.session_id = ''"
              >
                Chat
              </button>
            </div>
            <p class="modal-hint">
              Chat uses the managed conversation stream. Terminal preserves the native CLI surface.
            </p>
          </div>
          <div class="modal-field">
            <label>Mode</label>
            <div class="segmented-control segmented-control--three">
              <button
                type="button"
                :class="['segment-button', { active: form.task_mode === 'direct' }]"
                @click="form.task_mode = 'direct'"
              >
                Direct
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.task_mode === 'reviewed' }]"
                @click="form.task_mode = 'reviewed'"
              >
                Reviewed
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.task_mode === 'autonomous' }]"
                @click="form.task_mode = 'autonomous'"
              >
                Autonomous
              </button>
            </div>
          </div>
          <div class="modal-field">
            <label>Execution</label>
            <div class="segmented-control segmented-control--three">
              <button
                type="button"
                :class="['segment-button', { active: form.execution_complexity === 'auto' }]"
                title="Let the agent decide whether independent parts of this task benefit from delegation."
                @click="form.execution_complexity = 'auto'"
              >
                Auto
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.execution_complexity === 'simple' }]"
                title="Use a single execution agent without requesting subagent delegation."
                @click="form.execution_complexity = 'simple'"
              >
                Simple
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.execution_complexity === 'complex' }]"
                title="Use orchestration for work with independent subtasks. Additional agents can increase token usage."
                @click="form.execution_complexity = 'complex'"
              >
                Complex
              </button>
            </div>
          </div>
          <div
            v-if="form.task_mode === 'autonomous'"
            class="autonomy-form"
          >
            <div class="form-row">
              <div class="modal-field">
                <label>Max iterations</label>
                <input
                  v-model.number="form.max_iterations"
                  type="number"
                  min="1"
                  max="10"
                >
              </div>
              <div class="modal-field">
                <label>Strictness</label>
                <select v-model="form.evaluation_strictness">
                  <option value="lenient">
                    Lenient
                  </option>
                  <option value="balanced">
                    Balanced
                  </option>
                  <option value="strict">
                    Strict
                  </option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <div class="modal-field">
                <label class="checkbox-label">
                  <input
                    v-model="form.allow_web_research"
                    type="checkbox"
                  >
                  Web research
                </label>
              </div>
              <div class="modal-field">
                <label class="checkbox-label">
                  <input
                    v-model="form.require_artifact_review"
                    type="checkbox"
                  >
                  Artifact review
                </label>
              </div>
            </div>
          </div>
          <div
            v-if="form.execution_control === 'workspace'"
            class="modal-field"
          >
            <label>Dispatch agent</label>
            <select
              v-model="form.session_id"
              :disabled="!targetAvailable"
            >
              <option value="">
                Auto
              </option>
              <option
                v-for="agent in workspaceAgents.filter(agent => agent.session_kind === form.session_kind)"
                :key="agent.id"
                :value="agent.id"
              >
                {{ agent.title }}
              </option>
            </select>
          </div>
          <div
            v-if="form.execution_control === 'workspace'"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="form.clear_context"
                type="checkbox"
              >
              Clear context
            </label>
          </div>
          <div
            v-if="form.execution_control === 'workspace'"
            class="modal-field"
          >
            <label>Related task</label>
            <p class="modal-hint">
              Session/context reuse hint — not a Task parent (<code>parent_task_id</code>).
              Task Graph tree UI is follow-up <code>487c630c</code>; not implemented here.
            </p>
            <select
              v-model="form.related_task_id"
              :disabled="!targetAvailable"
            >
              <option value="">
                None
              </option>
              <option
                v-for="task in tasks"
                :key="task.id"
                :value="task.id"
              >
                {{ task.title }}
              </option>
            </select>
          </div>
        </template>
        <div class="modal-actions">
          <button
            v-if="pendingAttempt && !submitting && !reporterRecovery"
            type="button"
            class="tool-button modal-actions-leading"
            @click="discardPendingAttempt"
          >
            Discard and start over
          </button>
          <button
            type="button"
            class="tool-button"
            @click="close"
          >
            Cancel
          </button>
          <LoadingButton
            v-if="!reporterRecovery"
            type="submit"
            class="primary-button"
            :disabled="submitDisabled"
            :loading="submitting"
            loading-label="Adding task"
          >
            {{ submitting ? 'Creating Task' : (pendingAttempt ? 'Retry saved request' : 'Create Task') }}
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import { formatAttachmentSize } from '@/utils/draftAttachments'
import type { TaskCreateFlow } from '@/composables/useTaskCreateFlow'

const props = defineProps<{ flow: TaskCreateFlow }>()
const {
  visible, form, pendingAttempt, reporterRecovery, revealReporterKey,
  capabilities, capabilitiesStatus, capabilitiesError, submitDisabled,
  submitting, targetAvailable, workspaceAgents, tasks, close, retryCapabilities,
  discardPendingAttempt, onFormMutation, pasteAttachments, removeAttachment,
  toggleReporterKey, copyReporterCredential, persistReporterCredential, submit,
} = props.flow
</script>

<style scoped>


.form-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tool-button,
.primary-button {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}

.tool-button,
.primary-button {
  height: 32px;
  padding: 0 14px;
  font-size: var(--ch-font-size-base);
  font-weight: 500;
  font-family: var(--ch-font-sans);
  transition: all var(--ch-motion-fast);
}

.tool-button,
.primary-button {
  gap: 6px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.tool-button:hover,
.primary-button:hover {
  border-color: var(--ch-color-border-hover);
}

.tool-button:focus-visible,
.primary-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.tool-button,
.primary-button {
  cursor: pointer;
}

.tool-button:disabled,
.primary-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.primary-button {
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
  font-weight: 600;
  box-shadow: var(--ch-shadow-button);
}

.primary-button:hover {
  background: var(--ch-color-accent-fill-hover);
  border-color: var(--ch-color-accent-fill-hover);
  box-shadow: var(--ch-shadow-button-primary);
}

.form-row > .modal-field {
  flex: 1;
}

.tool-button:active,
.primary-button:active {
  transform: translateY(1px);
  background: var(--ch-color-surface-pressed);
}

.icon-button {
  width: 32px;
  height: 32px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all var(--ch-motion-fast);
}

.icon-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.icon-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.attachment-list {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.attachment-row {
  min-width: 0;
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.attachment-thumb {
  width: 54px;
  height: 42px;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
}

.attachment-thumb img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.attachment-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.attachment-meta strong,
.attachment-meta span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-meta strong {
  color: var(--ch-color-text);
  font-size: 12px;
}

.attachment-meta span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.workspace-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  background: var(--ch-color-overlay);
  padding: 16px;
}

.workspace-modal {
  width: min(520px, 100%);
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  box-shadow: var(--ch-shadow-dialog);
  padding: 20px;
}

.workspace-modal h3 {
  margin: 0 0 16px;
  color: var(--ch-color-text-strong);
  font-size: 18px;
}

.modal-field {
  margin-bottom: 14px;
}

.modal-field label {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
}

.modal-field input,
.modal-field textarea,
.modal-field select {
  width: 100%;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  font-family: var(--ch-font-sans);
  box-sizing: border-box;
  transition: all var(--ch-motion-fast);
}

.modal-field input,
.modal-field select {
  height: 36px;
  padding: 0 12px;
}

.modal-field textarea {
  min-height: 120px;
  resize: vertical;
  padding: 10px 12px;
}

.modal-field input:focus,
.modal-field textarea:focus,
.modal-field select:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.modal-field .checkbox-label {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin-bottom: 0;
  color: var(--ch-color-text);
}

.modal-field .checkbox-label input {
  width: 16px;
  height: 16px;
  accent-color: var(--ch-color-accent);
}

.modal-hint {
  margin: 6px 0 0;
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-xs);
  line-height: 1.35;
}

.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-sunken);
  padding: 3px;
}

.segmented-control--three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.segment-button {
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  padding: 8px 10px;
  font-family: var(--ch-font-sans);
  transition: all var(--ch-motion-fast);
}

.segment-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.segment-button.active {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-strong);
}

.segment-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.autonomy-form {
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  padding: 12px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}

.modal-actions .modal-actions-leading {
  margin-right: auto;
}

.task-create-recovery {
  margin-top: 4px;
  padding: 12px 14px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
}

.task-create-recovery-title {
  margin: 0;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.modal-actions button {
  white-space: nowrap;
}

@media (max-width: 760px) {

  .workspace-modal-overlay {
    align-items: flex-start;
    padding: 10px;
  }

  .workspace-modal {
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 14px;
    border-radius: var(--ch-radius-md);
  }

  .modal-actions {
    position: sticky;
    bottom: -1px;
    background: var(--ch-color-surface);
    padding-top: 10px;
  }

  .modal-actions .tool-button,
  .modal-actions .primary-button {
    flex: 1;
  }

  .icon-button {
    width: 34px;
    height: 34px;
  }
}

@media (max-width: 480px) {

  .form-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
