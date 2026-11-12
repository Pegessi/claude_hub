<template>
  <GoalSetupDialog
    :open="isGoalSetupOpen"
    :busy="isGoalMutating || isPreparingAttachments"
    :submit-disabled="isGoalHydrating"
    :error="goalError || (!isGoalEditing ? composerError : null)"
    :editing="isGoalEditing"
    :initial-objective="goal?.objective"
    :initial-plan-first="planFirstPreferred"
    :plan-first-disabled-reason="planFirstDisabledReason"
    @close="isGoalSetupOpen = false"
    @submit="startGoal"
    @paste="handleGoalPaste"
  >
    <template #recovery>
      <div
        v-if="goalError"
        class="goal-status-recovery"
      >
        <p>
          Check Goal status before submitting again. If this continues, save your
          objective and attachments, then refresh the page. Refreshing reloads
          this page and may retry provider startup.
        </p>
        <button
          type="button"
          :disabled="isGoalMutating || isGoalHydrating"
          @click="hydrateGoal"
        >
          Check Goal status
        </button>
      </div>
    </template>
    <template #images>
      <p v-if="isGoalEditing">
        Send new images using Steer in the Chat composer.
      </p>
      <div
        v-else
        class="goal-image-picker"
      >
        <p>Images are sent with the first Goal turn.</p>
        <button
          type="button"
          :disabled="Boolean(attachmentDisabledReason)"
          @click="triggerFilePicker"
        >
          Add images
        </button>
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
            :disabled="isGoalMutating || isPreparingAttachments"
            :aria-label="`Remove ${att.filename}`"
            @click="removeAttachment(att)"
          >
            ×
          </button>
        </div>
      </div>
    </template>
  </GoalSetupDialog>
</template>

<script setup lang="ts">
import { requireChatOwner, chatGoalKey, chatAttachmentsKey, chatDeliveryKey, chatImagesKey } from '../owners'
import GoalSetupDialog from '@/components/GoalSetupDialog.vue'
const { isGoalSetupOpen, isGoalMutating, isGoalHydrating, hydrateGoal, goalError, isGoalEditing, goal, startGoal, planFirstPreferred, planFirstDisabledReason } = requireChatOwner(chatGoalKey)
const { isPreparingAttachments, attachmentDisabledReason, triggerFilePicker, attachments, removeAttachment, handlePaste } = requireChatOwner(chatAttachmentsKey)
const { composerError } = requireChatOwner(chatDeliveryKey)
const { openImageLightbox } = requireChatOwner(chatImagesKey)

/** A pasted image joins the same draft the "Add images" button fills, so
 *  ``startGoal`` sends it with the first Goal turn. Editing an existing Goal
 *  has no image field: the dialog tells the reader to steer the Chat instead,
 *  and a draft staged here would be invisible (the picker is hidden while
 *  editing) yet still attached to the next composer send. */
function handleGoalPaste(event: ClipboardEvent) {
  if (isGoalEditing.value) return
  // While the Goal is being created the request already carries a snapshot of
  // these images and clears the array when it resolves, so a paste here would
  // stage a chip that silently disappears.
  if (isGoalMutating.value) return
  handlePaste(event)
}
</script>

<style scoped>
.goal-status-recovery { display: grid; gap: 8px; font-size: 12px; line-height: 1.5; }
.goal-status-recovery p { margin: 0; }
.goal-status-recovery button {
  justify-self: start;
  min-height: 44px;
  padding: 8px 12px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
}
.goal-status-recovery button:disabled { opacity: 0.55; cursor: default; }
/* The picker owns this row inside the Goal dialog: one line for the caption,
   then the trigger and the chips side by side instead of stacked at zero gap. */
.goal-image-picker {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.goal-image-picker p {
  flex: 1 1 100%;
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

.attachment-preview { width: 100%; height: 100%; padding: 0; display: flex; border: 0; background: transparent; cursor: zoom-in; }
@media (max-width: 640px) {

  .attachment-chip {
    width: 40px;
    height: 40px;
  }
}
</style>
