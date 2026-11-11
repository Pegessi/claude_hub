<template>
  <section
    class="detail-section evidence-section"
    aria-label="Task evidence"
  >
    <div class="detail-section-title evidence-title">
      <span>Evidence</span>
      <span
        v-if="evidence.reportedAt"
        class="evidence-timestamp"
      >{{ formatTime(evidence.reportedAt) }}</span>
    </div>

    <p
      v-if="!evidence.hasContent"
      class="evidence-empty"
    >
      No validation, changed files, or artifacts reported yet. Ask the executor for evidence before deciding.
    </p>

    <template v-else>
      <div
        v-if="evidence.acceptance.length > 0"
        class="evidence-block"
      >
        <h4>Acceptance criteria</h4>
        <ol class="evidence-checklist">
          <li
            v-for="item in evidence.acceptance"
            :key="item.criterion"
            :class="['evidence-check', `evidence-check--${item.status}`]"
          >
            <span
              class="evidence-check-mark"
              :data-status="item.status"
              :aria-label="acceptanceStatusLabel(item.status)"
            >{{ acceptanceMark(item.status) }}</span>
            <span class="evidence-check-body">
              <span class="evidence-check-criterion">{{ item.criterion }}</span>
              <!-- Repeating "Not reported" under every criterion was noise;
                   the hollow mark already says the executor has not answered. -->
              <span
                v-if="item.status !== 'unreported' || item.evidence"
                class="evidence-check-meta"
              >
                {{ acceptanceStatusLabel(item.status) }}
                <template v-if="item.evidence"> — {{ item.evidence }}</template>
              </span>
            </span>
          </li>
        </ol>
      </div>

      <div
        v-if="evidence.validation"
        class="evidence-block"
      >
        <h4>Validation</h4>
        <MarkdownContent
          compact
          link-markdown-paths
          :text="evidence.validation"
          @markdown-path-click="path => openMarkdownPreviewModal(path)"
        />
      </div>

      <div
        v-if="evidence.changedFiles.length > 0"
        class="evidence-block"
      >
        <h4>Changed files</h4>
        <div class="evidence-chips">
          <!-- Only Markdown opens a preview. A disabled button is unfocusable
               and explains nothing to a keyboard user, so everything else is
               plain text with the same chip styling. -->
          <template
            v-for="file in evidence.changedFiles"
            :key="file"
          >
            <button
              v-if="isMarkdownArtifact(file)"
              type="button"
              class="evidence-chip evidence-chip--action"
              :title="`Preview ${file}`"
              @click="openMarkdownPreviewModal(file)"
            >
              {{ file }}
            </button>
            <span
              v-else
              class="evidence-chip"
              :title="file"
            >{{ file }}</span>
          </template>
        </div>
      </div>

      <div
        v-if="evidence.artifactRefs.length > 0 || imageAttachments.length > 0"
        class="evidence-block"
      >
        <h4>Artifacts</h4>
        <div
          v-if="imageAttachments.length > 0"
          class="evidence-thumbs"
        >
          <button
            v-for="attachment in imageAttachments"
            :key="attachment.id"
            type="button"
            class="evidence-thumb"
            :aria-label="`Open image ${attachment.filename}`"
            @click="openImageLightbox(attachmentUrl(attachment.id), attachment.filename)"
          >
            <img
              :src="attachmentUrl(attachment.id)"
              :alt="attachment.filename"
            >
          </button>
        </div>
        <div
          v-if="evidence.artifactRefs.length > 0"
          class="evidence-chips"
        >
          <template
            v-for="artifact in evidence.artifactRefs"
            :key="artifact"
          >
            <button
              v-if="isMarkdownArtifact(artifact)"
              type="button"
              class="evidence-chip evidence-chip--action"
              :title="`Preview ${artifact}`"
              @click="openMarkdownPreviewModal(artifact)"
            >
              {{ artifact }}
            </button>
            <span
              v-else
              class="evidence-chip"
              :title="artifact"
            >{{ artifact }}</span>
          </template>
        </div>
      </div>

      <div
        v-if="evidence.risks"
        class="evidence-block evidence-block--risk"
      >
        <h4>Risks</h4>
        <MarkdownContent
          compact
          link-markdown-paths
          :text="evidence.risks"
          @markdown-path-click="path => openMarkdownPreviewModal(path)"
        />
      </div>

      <div
        v-if="evidence.links.length > 0"
        class="evidence-block"
      >
        <h4>Review links</h4>
        <div class="evidence-chips">
          <a
            v-for="link in evidence.links"
            :key="link.url"
            class="evidence-chip evidence-link-chip"
            :href="link.url"
            target="_blank"
            rel="noopener noreferrer"
            :title="link.url"
          >
            {{ link.label }}
          </a>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import type { WorkspaceTask } from '@/types'
import {
  taskEvidence,
  type EvidenceCriterionStatus,
} from '../presentation/evidence'
import { isMarkdownArtifact } from '../presentation/reports'
import {
  useWorkspaceArtifactsContext,
  useWorkspaceRuntimeContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
const props = defineProps<{ selectedTask: WorkspaceTask }>()
const { selectedReports } = useWorkspaceTaskPresentationContext()
const { formatTime } = useWorkspaceRuntimeContext()
const {
  openMarkdownPreviewModal,
  openImageLightbox,
} = useWorkspaceArtifactsContext()

const evidence = computed(() => taskEvidence(props.selectedTask, selectedReports.value))

// Task attachments are the only workspace images the Hub can serve as bytes,
// so they are the thumbnails here; other artifact references stay text chips.
const imageAttachments = computed(() =>
  (props.selectedTask.attachments || []).filter(
    attachment => (attachment.mime_type || '').startsWith('image/'),
  ),
)

function attachmentUrl(id: string): string {
  return `/api/workspaces/attachments/${id}`
}

const ACCEPTANCE_LABELS: Record<EvidenceCriterionStatus, string> = {
  passed: 'Passed',
  failed: 'Failed',
  partial: 'Partial',
  not_checked: 'Not checked',
  unreported: 'Not reported',
}

const ACCEPTANCE_MARKS: Record<EvidenceCriterionStatus, string> = {
  passed: '✓',
  failed: '✗',
  partial: '~',
  not_checked: '?',
  unreported: '·',
}

function acceptanceStatusLabel(status: EvidenceCriterionStatus): string {
  return ACCEPTANCE_LABELS[status]
}

function acceptanceMark(status: EvidenceCriterionStatus): string {
  return ACCEPTANCE_MARKS[status]
}
</script>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.evidence-section {
  gap: 10px;
}

.evidence-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.evidence-timestamp {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-weight: 400;
  text-transform: none;
}

.evidence-section {
  display: grid;
  gap: 12px;
}

.evidence-empty {
  margin: 0;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.evidence-block {
  min-width: 0;
  display: grid;
  gap: 6px;
  border-left: 2px solid var(--ch-color-border-hover);
  padding-left: 10px;
}

.evidence-block--risk {
  border-left-color: var(--ch-color-warning-strong);
}

.evidence-block h4 {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.evidence-checklist {
  margin: 0;
  padding: 0;
  display: grid;
  gap: 6px;
  list-style: none;
}

.evidence-check {
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 8px;
}

.evidence-check-mark {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  line-height: 1;
}

.evidence-check-mark[data-status='passed'] {
  border-color: var(--ch-color-success);
  color: var(--ch-color-success);
}

.evidence-check-mark[data-status='failed'] {
  border-color: var(--ch-color-danger);
  color: var(--ch-color-danger);
}

.evidence-check-mark[data-status='partial'] {
  border-color: var(--ch-color-warning-strong);
  color: var(--ch-color-warning-strong);
}

.evidence-check-body {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.evidence-check-criterion {
  color: var(--ch-color-text);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.evidence-check-meta {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.evidence-check--unreported .evidence-check-criterion {
  color: var(--ch-color-text-muted);
}

.evidence-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-width: 0;
}

.evidence-chip {
  max-width: 100%;
  display: inline-block;
  border: 0;
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-family: var(--ch-font-mono);
  font-size: 10px;
  line-height: 1.3;
  padding: 3px 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.evidence-chip--action {
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.evidence-link-chip {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-accent);
  font-family: var(--ch-font-sans);
  font-size: 11px;
  font-weight: 600;
  text-decoration: none;
}

.evidence-link-chip:hover {
  text-decoration: underline;
  text-underline-offset: 2px;
}

.evidence-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.evidence-thumb {
  width: 64px;
  height: 64px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  cursor: pointer;
  transition: border-color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.evidence-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.evidence-thumb:hover {
  border-color: var(--ch-color-accent);
  transform: scale(1.03);
}

.evidence-thumb:focus-visible,
.evidence-chip--action:focus-visible,
.evidence-link-chip:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>
