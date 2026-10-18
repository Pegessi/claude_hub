<template>
  <div class="structured-composer">
    <div class="composer-shell">
      <div
        v-if="modeChangeError"
        class="composer-mode-error"
        role="alert"
      >
        {{ modeChangeError }}
      </div>
      <div
        v-if="goalError && !goal && !isGoalSetupOpen && !isManagedChat"
        class="composer-mode-error"
        role="alert"
      >
        Goal: {{ goalError }}
        <button
          type="button"
          @click="hydrateGoal"
        >
          Retry
        </button>
      </div>

      <!-- Attachment previews -->
      <div
        v-if="attachments.length > 0"
        class="composer-attachments"
      >
        <div
          v-for="att in attachments"
          :key="att.id"
          class="attachment-chip"
        >
          <button
            type="button"
            class="attachment-preview"
            :aria-label="`View ${att.filename}`"
            @click="openImageLightbox(att.data_url, att.filename, $event)"
          >
            <img
              :src="att.preview_url"
              :alt="att.filename"
              class="attachment-thumb"
            >
          </button>
          <button
            type="button"
            class="attachment-remove"
            :disabled="isSending || isGoalMutating || isPreparingAttachments"
            :aria-label="`Remove ${att.filename}`"
            @click="removeAttachment(att)"
          >
            ×
          </button>
        </div>
      </div>

      <!-- Validation error -->
      <div
        v-if="composerError"
        class="composer-error"
        role="alert"
      >
        {{ composerError }}
      </div>

      <ChatDraftQueue
        :drafts="draftQueue"
        :busy="isSending || isQueueSendInProgress || isPreparingAttachments"
        :ready="isSubmissionReady && !goalInputBlocked"
        :goal-active="goalSteerOnly"
        :turn-in-flight="turnInFlight"
        :editing-id="queuedChatState.editingDraftId"
        :editing-message="queuedChatState.editingMessage"
        @editing="setQueuedDraftEdit"
        @update="updateQueuedDraft"
        @cancel="cancelQueuedDraft"
        @edit="editQueuedDraft"
        @send="sendQueuedDraft"
        @preview="(att, event) => openImageLightbox(att.data_url, att.filename, event)"
      />

      <div class="composer-row">
        <textarea
          ref="composerTextareaEl"
          v-model="draftMessage"
          class="composer-textarea"
          placeholder="Send a message…"
          rows="1"
          :disabled="isSending || isGoalMutating || connectionState !== 'live' || goalInputBlocked"
          :title="goalInputBlocked ? goalComposerReason || undefined : undefined"
          @compositionstart="isComposing = true"
          @compositionend="isComposing = false"
          @keydown.enter.exact="handleComposerEnter"
          @keydown.enter.meta.exact="handleComposerEnter"
          @keydown.enter.ctrl.exact="handleComposerEnter"
          @input="syncComposerTextareaHeight"
          @paste="handlePaste"
        />
        <div class="composer-tools">
          <ComposerAddMenu
            v-model:open="isAddMenuOpen"
            :show-goal="!isManagedChat && !goal && Boolean(capabilities?.supports_goals)"
            :show-schedule="!isManagedChat"
            :attachment-disabled-reason="attachmentDisabledReason"
            :goal-disabled-reason="goalSetupDisabledReason"
            @attachment="triggerFilePicker"
            @goal="isGoalEditing = false; isGoalSetupOpen = true"
            @schedule="appStore.openScheduledTasks(props.tabId)"
          />
          <input
            ref="fileInputEl"
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            class="composer-file-input"
            @change="handleFilePick"
          >
          <ChatModePicker
            v-if="!isManagedChat && modeOptions.length > 0"
          />
          <ChatModelPicker
            v-if="isModelPickerAvailable"
          />
        </div>
        <button
          v-if="stopArmed"
          type="button"
          class="composer-stop-btn"
          :disabled="isSending || isCancelling"
          title="Stop the current turn (works even while reconnecting, queued, or after an error)"
          @click="cancelActiveTurn"
        >
          {{ isCancelling ? 'Stopping…' : 'Stop' }}
        </button>
        <button
          type="button"
          class="composer-send-btn"
          :disabled="!canSend || isSending"
          :title="goalSteerOnly ? 'Send now as the next Goal turn' : goalComposerReason || undefined"
          @click="() => submit(isManagedChat ? 'normal' : (goalSteerOnly ? 'steer' : 'normal'))"
        >
          {{ isSending ? 'Sending…' : (goalImageRecovery ? 'Recover Goal' : goalSteerOnly ? 'Steer' : (composerTurnInFlight ? 'Queue' : 'Send')) }}
        </button>
      </div>
      <div
        v-if="!isMobileViewport"
        class="composer-hints"
      >
        <template v-if="goalInputBlocked">
          <span>{{ goalComposerReason }}</span>
        </template>
        <template v-else-if="goalSteerOnly">
          <span>Enter queue until the Goal stops</span>
          <span>⌘/Ctrl+Enter steer Goal</span>
          <span>Shift+Enter newline</span>
        </template>
        <template v-else-if="isManagedChat">
          <span>Enter / ⌘/Ctrl+Enter send</span>
          <span v-if="composerTurnInFlight">Busy turn follow-ups are durable</span>
          <span>Shift+Enter newline</span>
        </template>
        <template v-else>
          <span>Enter {{ composerTurnInFlight ? 'queue' : 'send' }}</span>
          <span>⌘/Ctrl+Enter steer</span>
          <span>Shift+Enter newline</span>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatSettingsKey, chatGoalKey, chatAttachmentsKey, chatImagesKey, chatDeliveryKey, chatQueueKey, chatComposerKey, chatIdentityKey } from '../owners'
import ChatDraftQueue from '@/components/ChatDraftQueue.vue'
import ComposerAddMenu from '@/components/ComposerAddMenu.vue'
import ChatModelPicker from './ChatModelPicker.vue'
import ChatModePicker from './ChatModePicker.vue'
const { modeChangeError, isAddMenuOpen, modeOptions, isModelPickerAvailable } = requireChatOwner(chatSettingsKey)
const { goalError, goal, isGoalSetupOpen, hydrateGoal, isGoalMutating, goalInputBlocked, goalSteerOnly, goalComposerReason, goalSetupDisabledReason, isGoalEditing, goalImageRecovery } = requireChatOwner(chatGoalKey)
const { attachments, isPreparingAttachments, removeAttachment, handlePaste, attachmentDisabledReason, triggerFilePicker, fileInputEl, handleFilePick } = requireChatOwner(chatAttachmentsKey)
const { openImageLightbox } = requireChatOwner(chatImagesKey)
const { isSending, composerError, isSubmissionReady, turnInFlight, composerTurnInFlight, draftMessage, stopArmed, isCancelling, cancelActiveTurn, canSend, submit } = requireChatOwner(chatDeliveryKey)
const { draftQueue, isQueueSendInProgress, queuedChatState, setQueuedDraftEdit, updateQueuedDraft, cancelQueuedDraft, editQueuedDraft, sendQueuedDraft } = requireChatOwner(chatQueueKey)
const { composerTextareaEl, isComposing, handleComposerEnter, syncComposerTextareaHeight, isMobileViewport } = requireChatOwner(chatComposerKey)
const { isManagedChat, connectionState, capabilities, appStore, props } = requireChatOwner(chatIdentityKey)
</script>

<style scoped>
.structured-composer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  flex-shrink: 0;
}
.composer-tools {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex: 1 1 0;
  min-width: 0;
}

.composer-mode-error {
  margin: 0 0 7px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 11px;
  line-height: 1.35;
}

.composer-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.attachment-chip {
  position: relative;
  width: 48px;
  height: 48px;
  border-radius: var(--ch-radius-sm);
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
}

.attachment-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.attachment-remove {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background-color: var(--ch-color-danger-strong, #f85149);
  color: #fff;
  border: none;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.composer-error {
  font-size: 12px;
  color: var(--ch-color-danger-strong, #f85149);
  margin-bottom: 6px;
}

.composer-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.composer-file-input {
  display: none;
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

.composer-send-btn {
  height: 32px;
  padding: 0 14px;
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  background-color: var(--ch-color-accent-strong);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.composer-send-btn:hover:not(:disabled) {
  background-color: var(--ch-color-accent-fill-hover);
}

.composer-send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-send-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-stop-btn {
  height: 32px;
  padding: 0 12px;
  font-size: 13px;
  font-weight: 500;
  color: var(--ch-color-text);
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.composer-stop-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.attachment-preview { width: 100%; height: 100%; padding: 0; display: flex; border: 0; background: transparent; cursor: zoom-in; }

.composer-hints {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 6px 10px 0;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
}

.structured-composer {
  padding: 12px 28px;
  background: color-mix(in srgb, var(--ch-color-surface) 94%, transparent);
}

.composer-shell {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
}

.composer-row {
  padding: 5px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: calc(var(--ch-radius-md) + 2px);
  background: var(--ch-color-app-bg);
  box-shadow: 0 8px 24px var(--ch-shadow-color-soft);
}

.composer-row:focus-within {
  border-color: var(--ch-color-accent);
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

.composer-send-btn,

.composer-stop-btn {
  height: 34px;
  border-radius: var(--ch-radius-sm);
}
@media (max-width: 640px) {

  .structured-composer {
    padding: 8px 12px;
  }

  .composer-row {
    gap: 4px;
  }

  .composer-tools {
    gap: 2px;
  }

  .composer-send-btn {
    padding: 0 10px;
  }

  .attachment-chip {
    width: 40px;
    height: 40px;
  }
}
</style>
