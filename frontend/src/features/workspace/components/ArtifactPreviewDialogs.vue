<template>
  <Teleport to="body">
    <div
      v-if="markdownPreviewModalPath"
      class="workspace-modal-overlay markdown-preview-modal-overlay"
      @click.self="closeMarkdownPreviewModal"
    >
      <div
        class="workspace-modal markdown-preview-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="`Markdown preview: ${markdownPreviewModalPath}`"
      >
        <div class="markdown-preview-modal-header">
          <div>
            <span>Markdown Preview</span>
            <strong>{{ markdownPreviewModalPath }}</strong>
          </div>
          <button
            type="button"
            class="icon-button"
            aria-label="Close Markdown preview"
            @click="closeMarkdownPreviewModal"
          >
            x
          </button>
        </div>
        <div
          v-if="markdownPreviewModalError"
          class="artifact-preview-status artifact-preview-error"
        >
          {{ markdownPreviewModalError }}
        </div>
        <div
          v-else-if="markdownPreviewModalLoading || !markdownPreviewModalContent"
          class="artifact-preview-status"
        >
          Loading Markdown preview...
        </div>
        <template v-else>
          <div class="artifact-preview-header">
            <span>{{ markdownPreviewModalContent.filename }}</span>
            <span>{{ formatAttachmentSize(markdownPreviewModalContent.size_bytes) }}</span>
          </div>
          <MarkdownContent
            class="artifact-preview-content markdown-preview-modal-content"
            :text="markdownPreviewModalContent.content"
          />
          <div
            v-if="markdownPreviewModalContent.truncated"
            class="artifact-preview-status"
          >
            Preview truncated to the first 512 KB.
          </div>
        </template>
      </div>
    </div>
  </Teleport>

  <Teleport to="body">
    <div
      v-if="imageLightboxUrl"
      class="workspace-modal-overlay image-lightbox-overlay"
      @click.self="closeImageLightbox"
    >
      <button
        type="button"
        class="icon-button image-lightbox-close"
        aria-label="Close image preview"
        @click="closeImageLightbox"
      >
        x
      </button>
      <img
        class="image-lightbox-img"
        :src="imageLightboxUrl"
        :alt="imageLightboxAlt"
        @click.stop
      >
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import MarkdownContent from '@/components/MarkdownContent.vue'
import {
  formatAttachmentSize,
} from '@/utils/draftAttachments'
import {
  useWorkspaceArtifactsContext,
} from '../context'
const {
  markdownPreviewModalPath,
  closeMarkdownPreviewModal,
  markdownPreviewModalError,
  markdownPreviewModalLoading,
  markdownPreviewModalContent,
  imageLightboxUrl,
  closeImageLightbox,
  imageLightboxAlt,
} = useWorkspaceArtifactsContext()
</script>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped src="../styles/shared-artifact-previews.css"></style>

<style scoped>
.markdown-preview-modal-overlay {
  z-index: 1400;
}

.markdown-preview-modal {
  width: min(960px, 100%);
}

.markdown-preview-modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.markdown-preview-modal-header > div {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.markdown-preview-modal-header span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
  text-transform: uppercase;
}

.markdown-preview-modal-header strong {
  color: var(--ch-color-text);
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-base);
  overflow-wrap: anywhere;
}

.markdown-preview-modal-content {
  max-height: min(70dvh, 720px);
}

.image-lightbox-overlay {
  z-index: 1400;
}

.image-lightbox-img {
  /* Fit within the viewport (minus the overlay padding) while preserving the
     intrinsic aspect ratio; scale down large images, never up small ones. */
  max-width: min(1200px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.45);
}

.image-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
}
</style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>
