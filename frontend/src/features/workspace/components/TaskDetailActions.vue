<template>
  <div
    v-if="canUseManagedTaskActions(selectedTask)"
    class="detail-footer"
  >
    <button
      type="button"
      class="detail-footer-toggle"
      :aria-expanded="isDetailActionsExpanded"
      @click="toggleDetailActions"
    >
      <span>{{ isDetailActionsExpanded ? 'Hide actions' : 'Actions' }}</span>
      <span class="detail-footer-chevron">
        {{ isDetailActionsExpanded ? 'v' : '^' }}
      </span>
    </button>
    <div class="detail-action-drawer">
      <div class="detail-actions">
        <LoadingButton
          v-if="selectedTask.status === 'todo'"
          type="button"
          class="primary-button"
          :loading="isPending(taskActionKey('start', selectedTask.id))"
          loading-label="Starting task"
          @click="startTask(selectedTask)"
        >
          Start
        </LoadingButton>
        <button
          v-if="canEditTask(selectedTask)"
          type="button"
          class="tool-button"
          @click="openEditTaskModal(selectedTask)"
        >
          Edit
        </button>
        <LoadingButton
          v-if="canMarkDoneTask(selectedTask)"
          type="button"
          class="tool-button"
          :loading="isPending(taskActionKey('mark-done', selectedTask.id))"
          loading-label="Marking done"
          @click="markTask(selectedTask.id, 'done')"
        >
          Done
        </LoadingButton>
        <LoadingButton
          v-if="canRequestReviewTask(selectedTask)"
          type="button"
          class="tool-button"
          :loading="isPending(taskActionKey('request-review', selectedTask.id))"
          loading-label="Requesting review"
          @click="requestReview(selectedTask)"
        >
          Request review
        </LoadingButton>
        <LoadingButton
          v-if="canAbortTask(selectedTask)"
          type="button"
          class="abort-button"
          :loading="isPending(taskActionKey('abort', selectedTask.id))"
          loading-label="Aborting task"
          @click="abortTask(selectedTask)"
        >
          Abort
        </LoadingButton>
        <LoadingButton
          v-if="selectedSession"
          type="button"
          class="tool-button"
          :loading="isPending(sessionActionKey('open', selectedSession.id))"
          loading-label="Opening terminal"
          @click="openSession(selectedSession)"
        >
          Open terminal
        </LoadingButton>
        <LoadingButton
          type="button"
          class="danger-button"
          :loading="isPending(taskActionKey('delete', selectedTask.id))"
          loading-label="Deleting task"
          @click="deleteTask(selectedTask)"
        >
          Delete
        </LoadingButton>
      </div>
      <form
        v-if="selectedSession"
        class="send-form"
        @submit.prevent="sendDetailMessage"
        @paste="handleAttachmentPaste($event, detailAttachments)"
      >
        <textarea
          v-model="detailMessage"
          placeholder="Follow-up instructions..."
        />
        <div
          v-if="detailAttachments.length > 0"
          class="attachment-list send-attachments"
        >
          <div
            v-for="attachment in detailAttachments"
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
              @click="removeDraftAttachment(detailAttachments, attachment)"
            >
              x
            </button>
          </div>
        </div>
        <LoadingButton
          type="submit"
          class="primary-button"
          :disabled="!detailMessage.trim() && detailAttachments.length === 0"
          :loading="isPending(selectedTaskSendKey)"
          loading-label="Sending message"
        >
          Send
        </LoadingButton>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  handleAttachmentPaste,
  formatAttachmentSize,
  removeDraftAttachment,
} from '@/utils/draftAttachments'
import type { WorkspaceTask } from '@/types'
defineProps<{ selectedTask: WorkspaceTask }>()
import {
  useWorkspaceTaskPresentationContext,
  useWorkspaceTaskActionsContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  canUseManagedTaskActions,
  canEditTask,
  canMarkDoneTask,
  canRequestReviewTask,
  canAbortTask,
  selectedSession,
  selectedTaskSendKey,
} = useWorkspaceTaskPresentationContext()
const {
  isDetailActionsExpanded,
  toggleDetailActions,
  startTask,
  openEditTaskModal,
  markTask,
  requestReview,
  abortTask,
  deleteTask,
  sendDetailMessage,
  detailAttachments,
  detailMessage,
} = useWorkspaceTaskActionsContext()
const {
  isPending,
  taskActionKey,
  sessionActionKey,
  openSession,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.detail-actions button {
  min-width: 86px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.detail-footer-toggle {
  display: none;
}

.detail-action-drawer {
  display: block;
}

.send-form {
  margin-top: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 8px;
}

.send-form textarea {
  min-height: 100px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-family: var(--ch-font-sans);
  line-height: 1.45;
  padding: 10px 12px;
  resize: vertical;
  transition: all var(--ch-motion-fast);
}

.send-form textarea:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.send-form .primary-button {
  height: 34px;
}

.send-attachments {
  grid-column: 1 / -1;
}
</style>

<style scoped src="../styles/shared-attachments.css"></style>

<style scoped>
.detail-footer {
  position: sticky;
  bottom: 0;
  z-index: 2;
  border-top: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
  padding: 12px 16px;
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .detail-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .detail-actions .primary-button {
    grid-column: 1 / -1;
  }

  .detail-footer {
    position: sticky;
    bottom: 0;
    padding: 8px 12px max(10px, env(safe-area-inset-bottom));
    box-shadow: 0 -12px 24px var(--ch-shadow-color-soft);
  }

  .detail-footer-toggle {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid var(--ch-color-border-strong);
    border-radius: var(--ch-radius-md);
    background: var(--ch-color-surface-control);
    color: var(--ch-color-text);
    font-size: var(--ch-font-size-md);
    font-weight: 600;
    transition: all var(--ch-motion-fast);
  }

  .detail-footer-toggle:focus-visible {
    outline: none;
    box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
    border-color: var(--ch-color-accent);
  }

  .detail-footer-chevron {
    color: var(--ch-color-accent);
    font-family: var(--ch-font-mono);
  }

  .detail-action-drawer {
    display: none;
    margin-top: 8px;
  }

  .task-detail-panel.mobile-actions-expanded .detail-action-drawer {
    display: block;
  }

  .send-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 88px;
    align-items: end;
  }

  .detail-actions button {
    width: auto;
    min-width: 0;
    height: 34px;
  }

  .send-form button {
    width: auto;
    min-width: 0;
    height: 36px;
  }

  .send-form textarea {
    min-height: 44px;
    max-height: 92px;
    font-size: 13px;
    line-height: 1.45;
  }
}
</style>
