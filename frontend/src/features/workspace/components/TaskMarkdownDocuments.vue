<template>
  <details class="detail-section detail-section--collapsible markdown-output-section">
    <summary class="detail-section-title detail-section-title--with-count">
      <span>Markdown Outputs</span>
      <span>{{ selectedMarkdownDocuments.length }}</span>
    </summary>
    <div
      v-if="selectedMarkdownDocuments.length === 0"
      class="empty-timeline"
    >
      No Markdown outputs discovered yet. Agents can report artifact_refs, or Markdown changed_files will appear here automatically.
    </div>
    <div
      v-else
      class="markdown-output-list"
    >
      <article
        v-for="document in selectedMarkdownDocuments"
        :key="document.id"
        class="markdown-output-card"
      >
        <div class="markdown-output-row">
          <div>
            <strong>{{ document.label }}</strong>
            <span>{{ markdownDocumentSourceLabel(document.source) }}</span>
            <code>{{ document.path }}</code>
          </div>
          <div class="markdown-output-actions">
            <span v-if="document.size_bytes">{{ formatAttachmentSize(document.size_bytes) }}</span>
            <button
              type="button"
              class="artifact-preview-button"
              :disabled="isMarkdownDocumentPreviewLoading(document)"
              @click="toggleMarkdownDocumentPreview(document)"
            >
              {{ markdownDocumentPreviewButtonLabel(document) }}
            </button>
          </div>
        </div>
        <div
          v-if="expandedArtifactKey === markdownDocumentPreviewKey(document)"
          class="artifact-preview markdown-output-preview"
        >
          <div
            v-if="artifactPreviewErrors[markdownDocumentPreviewKey(document)]"
            class="artifact-preview-status artifact-preview-error"
          >
            {{ artifactPreviewErrors[markdownDocumentPreviewKey(document)] }}
          </div>
          <div
            v-else-if="!artifactPreviews[markdownDocumentPreviewKey(document)]"
            class="artifact-preview-status"
          >
            Loading Markdown preview...
          </div>
          <template v-else>
            <div class="artifact-preview-header">
              <span>{{ artifactPreviews[markdownDocumentPreviewKey(document)].filename }}</span>
              <span>{{ formatAttachmentSize(artifactPreviews[markdownDocumentPreviewKey(document)].size_bytes) }}</span>
            </div>
            <MarkdownContent
              class="artifact-preview-content"
              :text="artifactPreviews[markdownDocumentPreviewKey(document)].content"
            />
            <div
              v-if="artifactPreviews[markdownDocumentPreviewKey(document)].truncated"
              class="artifact-preview-status"
            >
              Preview truncated to the first 512 KB.
            </div>
          </template>
        </div>
      </article>
    </div>
  </details>
</template>

<script setup lang="ts">
import MarkdownContent from '@/components/MarkdownContent.vue'
import {
  markdownDocumentSourceLabel,
  markdownDocumentPreviewKey,
} from '@/features/workspace/presentation/reports'
import {
  formatAttachmentSize,
} from '@/utils/draftAttachments'
import type { WorkspaceTask } from '@/types'
defineProps<{ selectedTask: WorkspaceTask }>()
import {
  useWorkspaceTaskPresentationContext,
  useWorkspaceArtifactsContext,
} from '../context'
const {
  selectedMarkdownDocuments,
} = useWorkspaceTaskPresentationContext()
const {
  isMarkdownDocumentPreviewLoading,
  toggleMarkdownDocumentPreview,
  markdownDocumentPreviewButtonLabel,
  expandedArtifactKey,
  artifactPreviewErrors,
  artifactPreviews,
} = useWorkspaceArtifactsContext()
</script>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.detail-section-title--with-controls,
.detail-section-title--with-count {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.detail-section-title--with-count > span:last-child {
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 2px 7px;
}
</style>

<style scoped src="../styles/shared-attachments.css"></style>

<style scoped>
.markdown-output-section {
  border-color: color-mix(in srgb, var(--ch-color-accent) 45%, var(--ch-color-border-muted));
}

.markdown-output-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.markdown-output-card {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 9px;
}

.markdown-output-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.markdown-output-row > div:first-child {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.markdown-output-row strong {
  color: var(--ch-color-text);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.markdown-output-row span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.markdown-output-row code {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  white-space: normal;
  overflow-wrap: anywhere;
}

.markdown-output-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.markdown-output-preview {
  margin-top: 10px;
}
</style>

<style scoped src="../styles/shared-artifact-previews.css"></style>
