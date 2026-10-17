<template>
  <GoalSetupDialog
    :open="isGoalSetupOpen"
    :busy="isGoalMutating || isPreparingAttachments"
    :error="goalError || (!isGoalEditing ? composerError : null)"
    :editing="isGoalEditing"
    :initial-objective="goal?.objective"
    @close="isGoalSetupOpen = false"
    @submit="startGoal"
  >
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
const { isGoalSetupOpen, isGoalMutating, goalError, isGoalEditing, goal, startGoal } = requireChatOwner(chatGoalKey)
const { isPreparingAttachments, attachmentDisabledReason, triggerFilePicker, attachments, removeAttachment } = requireChatOwner(chatAttachmentsKey)
const { composerError } = requireChatOwner(chatDeliveryKey)
const { openImageLightbox } = requireChatOwner(chatImagesKey)
</script>

<style scoped>
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
