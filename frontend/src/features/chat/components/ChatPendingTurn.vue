<template>
  <div
    class="structured-turn structured-turn--pending"
  >
    <div class="conversation-row conversation-row--user">
      <div class="conversation-bubble conversation-bubble--user">
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
          <button
            v-for="(att, i) in turn.attachments"
            :key="i"
            type="button"
            class="turn-attachment-button"
            aria-label="Open attached image preview"
            @click="openImageLightbox(att.preview_url, 'attached image', $event)"
          >
            <img
              :src="att.preview_url"
              class="turn-attachment-img"
              alt="attached image"
            >
          </button>
        </div>
      </div>
    </div>
    <div class="event-status event-status--pending">
      <span>Waiting for model activity…</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatImagesKey, chatIdentityKey } from '../owners'
import MarkdownContent from '@/components/MarkdownContent.vue'
import type { PendingTurn } from '../controllers/types'
const { openImageLightbox } = requireChatOwner(chatImagesKey)
const { markdownImageScope } = requireChatOwner(chatIdentityKey)
defineProps<{
  turn: PendingTurn
}>()
</script>

<style scoped>
.structured-turn {
  margin-bottom: 16px;
  position: relative;
}

.structured-turn--pending {
  opacity: 0.82;
}

.event-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
}

.event-status--pending {
  color: var(--ch-color-text-muted);
}

.structured-turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 22px;
}

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

.event-status {
  justify-content: center;
  font-style: italic;
  text-align: center;
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
