<template>
  <div
    class="conversation-row conversation-row--assistant conversation-row--agent-image"
  >
    <div class="agent-image-card">
      <button
        v-if="!erroredAgentImages.has(part.key)"
        type="button"
        class="turn-attachment-button agent-image-button"
        :aria-label="`Open image ${agentImageBasename(part.path)}`"
        @click="openImageLightbox(agentImageUrl(part.path), agentImageBasename(part.path), $event)"
      >
        <img
          :src="agentImageUrl(part.path)"
          class="turn-attachment-img"
          :alt="agentImageBasename(part.path)"
          loading="lazy"
          decoding="async"
          @error="onAgentImageError($event, part.key)"
        >
      </button>
      <div
        v-else
        class="turn-attachment-placeholder agent-image-missing"
      >
        Image unavailable
      </div>
      <div
        class="agent-image-path"
        :title="part.path"
      >
        {{ agentImageBasename(part.path) }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatImagesKey } from '../owners'
import type { TimelinePart } from '../timeline/types'
const { erroredAgentImages, agentImageBasename, openImageLightbox, agentImageUrl, onAgentImageError } = requireChatOwner(chatImagesKey)
// Reducer parts mutate in place; revision invalidates this leaf without replacing its DOM.
defineProps<{
  renderRevision: number
  part: Extract<TimelinePart, { kind: 'agent_image' }>
}>()
</script>

<style scoped>
.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
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
.agent-image-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.agent-image-path {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-muted);
}

.agent-image-missing {
  width: clamp(72px, 8vw, 88px);
  cursor: default;
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



@media (prefers-reduced-motion: reduce) {

  .turn-attachment-img {
    transition: none;
  }
}
</style>
