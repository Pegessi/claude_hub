<template>
  <div
    class="conversation-row conversation-row--user"
  >
    <div
      class="conversation-bubble conversation-bubble--user"
      :data-message-origin="turn.origin"
    >
      <div
        v-if="turn.origin === 'feishu' || turn.origin === 'hub_message'"
        class="conversation-message-source"
        data-testid="conversation-message-source"
      >
        <FeishuIdentityMark
          v-if="turn.origin === 'feishu'"
          class="conversation-message-source__mark"
          :avatar-url="resolvedFeishuSourceAvatarUrl(turn)"
          :identity-key="feishuSourceIdentityKey(turn)"
          data-testid="conversation-message-source-avatar"
        />
        <span class="conversation-message-source__label">{{ messageSourceLabel(turn) }}</span>
      </div>
      <!-- Inline edit mode -->
      <div
        v-if="isEditingTurn(turn)"
        class="edit-resend-form"
      >
        <textarea
          v-model="editDraft"
          class="edit-resend-textarea"
          rows="3"
          placeholder="Edit your message..."
          @keydown.enter.exact.prevent="submitEdit(turn)"
          @keydown.esc.prevent="cancelEdit"
        />
        <!-- Preserved attachments: edit-resend re-sends the original
           images, so show them as read-only thumbnails so the user
           knows they are kept (not dropped) on resend. -->
        <div
          v-if="turn.attachments?.length"
          class="turn-attachments edit-resend-attachments"
        >
          <template
            v-for="(att, i) in turn.attachments"
            :key="att.id ?? `edit-null-${turn.key}-${i}`"
          >
            <img
              v-if="att.id !== null && !erroredAttachments.has(att.id)"
              :src="attachmentUrl(att.id)"
              class="turn-attachment-img"
              alt="attached image (will be re-sent)"
              @error="onAttachmentError($event, att)"
            >
            <div
              v-else
              class="turn-attachment-placeholder"
            >
              <span>{{ att.id === null ? 'Preview unavailable' : 'Preview expired' }}</span>
            </div>
          </template>
        </div>
        <div
          v-if="editError"
          class="edit-resend-error"
        >
          {{ editError }}
        </div>
        <div class="edit-resend-actions">
          <button
            type="button"
            class="edit-resend-btn edit-resend-btn--primary"
            :disabled="isEditSending"
            @click="submitEdit(turn)"
          >
            {{ isEditSending ? 'Sending...' : 'Resend' }}
          </button>
          <button
            type="button"
            class="edit-resend-btn"
            :disabled="isEditSending"
            @click="cancelEdit"
          >
            Cancel
          </button>
        </div>
      </div>
      <!-- Normal display mode -->
      <template v-else>
        <MarkdownContent
          v-if="turn.userText"
          :text="turn.userText"
          :tab-id="markdownImageScope"
          compact
          @image-click="openImageLightbox"
        />
        <div
          v-if="turn.attachments?.length"
          class="turn-attachments"
        >
          <template
            v-for="(att, i) in turn.attachments"
            :key="att.id ?? `null-${turn.key}-${i}`"
          >
            <!-- Keep conversation density high: render a bounded
               thumbnail and open the full preview in a lightbox. -->
            <button
              v-if="att.id !== null && !erroredAttachments.has(att.id)"
              type="button"
              class="turn-attachment-button"
              aria-label="Open attached image preview"
              @click="openImageLightbox(attachmentUrl(att.id), 'attached image', $event)"
            >
              <img
                :src="attachmentUrl(att.id)"
                class="turn-attachment-img"
                alt="attached image"
                loading="lazy"
                decoding="async"
                @error="onAttachmentError($event, att)"
              >
            </button>
            <!-- Placeholder for no-preview (id is null) or evicted
               preview (fetch returned 404/410). -->
            <div
              v-else
              class="turn-attachment-placeholder"
            >
              <span>{{ att.id === null ? 'Preview unavailable' : 'Preview expired' }}</span>
            </div>
          </template>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatActionsKey, chatImagesKey, chatIdentityKey } from '../owners'
import FeishuIdentityMark from '@/components/FeishuIdentityMark.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { messageSourceLabel } from '../timeline/eventValues'
import type { TimelineTurn } from '../timeline/types'
const { resolvedFeishuSourceAvatarUrl, feishuSourceIdentityKey, isEditingTurn, editDraft, submitEdit, cancelEdit, editError, isEditSending } = requireChatOwner(chatActionsKey)
const { erroredAttachments, attachmentUrl, onAttachmentError, openImageLightbox } = requireChatOwner(chatImagesKey)
const { markdownImageScope } = requireChatOwner(chatIdentityKey)
defineProps<{
  turn: TimelineTurn
}>()
</script>

<style scoped>
.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}

.conversation-row--user {
  justify-content: flex-end;
}

.conversation-bubble {
  max-width: min(85%, 680px);
  min-width: 0;
  padding: 9px 12px;
  border-radius: var(--ch-radius-md);
  overflow-wrap: anywhere;
}

.conversation-bubble--user {
  position: relative;
  background: var(--ch-color-message-user-bg);
  color: var(--ch-color-text-inverse);
  border-bottom-right-radius: var(--ch-radius-sm);
}

.conversation-message-source {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  margin-bottom: 6px;
  color: var(--ch-color-message-user-muted);
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
}

.conversation-message-source__mark {
  position: relative;
  display: inline-grid;
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.18);
  font-size: 9px;
  overflow: hidden;
}

.conversation-message-source__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.turn-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.turn-attachment-button {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: block;
  padding: 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  box-shadow: 0 2px 8px rgb(0 0 0 / 18%);
  cursor: zoom-in;
}

.turn-attachment-button:hover {
  border-color: color-mix(in srgb, #fff 70%, transparent);
}

.turn-attachment-button:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}

.turn-attachment-img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  transition: transform 140ms ease;
}

.turn-attachment-button:hover .turn-attachment-img {
  transform: scale(1.025);
}

.turn-attachment-placeholder {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: grid;
  place-items: center;
  padding: 10px;
  border: 1px dashed var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  text-align: center;
}

.conversation-bubble--user .turn-attachment-placeholder {
  border-color: color-mix(in srgb, #fff 36%, transparent);
  color: var(--ch-color-message-user-muted);
}
.edit-resend-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: min(240px, 55vw);
}

.edit-resend-textarea {
  width: 100%;
  padding: 0;
  border: none;
  border-radius: 0;
  background: none;
  color: inherit;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  resize: vertical;
  outline: none;
}

.edit-resend-textarea:focus-visible {
  outline: 2px solid var(--ch-color-message-user-focus);
  outline-offset: 3px;
  border-radius: 2px;
}

.edit-resend-textarea::placeholder {
  color: var(--ch-color-message-user-muted);
}

.edit-resend-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.edit-resend-attachments .turn-attachment-img {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
}

.edit-resend-btn {
  padding: 5px 14px;
  border: 1px solid color-mix(in srgb, #fff 30%, transparent);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: #fff;
  font-size: 12px;
  cursor: pointer;
  transition: background 120ms ease;
}

.edit-resend-btn:hover:not(:disabled) {
  background: rgb(255 255 255 / 12%);
}

.edit-resend-btn--primary {
  background: rgb(255 255 255 / 18%);
  border-color: color-mix(in srgb, #fff 45%, transparent);
}

.edit-resend-btn--primary:hover:not(:disabled) {
  background: rgb(255 255 255 / 28%);
}

.edit-resend-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.edit-resend-error {
  color: var(--ch-color-message-user-error);
  font-size: 12px;
}
@media (max-width: 640px) {

  .conversation-bubble {
    width: min(92%, 680px);
    max-width: 92%;
  }
}

@media (prefers-reduced-motion: reduce) {

  .turn-attachment-img {
    transition: none;
  }
}

/* Compact Chat prose stays larger than dense Workspace Markdown. */
.conversation-bubble :deep(.markdown-content.compact) {
  font-size: 14px;
  line-height: 1.65;
}

.conversation-bubble--user :deep(.markdown-content),
.conversation-bubble--user :deep(.markdown-content :where(h1, h2, h3, h4, a, blockquote)) {
  color: inherit;
}
</style>
