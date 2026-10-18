<template>
  <details
    class="detail-section detail-section--collapsible"
    open
  >
    <summary class="detail-section-title">
      Progress
    </summary>
    <div
      v-if="selectedReports.length > 0 && hasBilingualReport"
      class="detail-section-controls"
    >
      <div
        class="lang-toggle"
        role="group"
        aria-label="Report language"
      >
        <button
          type="button"
          class="lang-toggle-btn"
          :class="{ active: reportLang === 'en' }"
          @click="setReportLang('en')"
        >
          EN
        </button>
        <button
          type="button"
          class="lang-toggle-btn"
          :class="{ active: reportLang === 'zh' }"
          @click="setReportLang('zh')"
        >
          中
        </button>
      </div>
    </div>
    <div
      v-if="selectedProgressTimeline.length > 0"
      class="progress-overview"
    >
      <ol class="progress-overview-timeline">
        <li
          v-for="item in selectedProgressTimeline"
          :key="item.id"
          :class="['progress-overview-item', `progress-overview-item--${item.tone}`]"
        >
          <span class="progress-overview-dot" />
          <span class="progress-overview-main">{{ item.label }}</span>
          <span class="progress-overview-time">{{ item.elapsedLabel }}</span>
          <span
            v-if="item.deltaLabel"
            class="progress-overview-delta"
          >
            +{{ item.deltaLabel }}
          </span>
        </li>
      </ol>
    </div>
    <div
      v-if="selectedReports.length === 0"
      class="empty-timeline"
    >
      No agent reports yet.
    </div>
    <ol
      v-else
      class="timeline"
    >
      <li
        v-for="(report, reportIndex) in selectedReports"
        :key="report.id"
      >
        <details
          class="report-card"
          :open="isLatestSelectedReport(report)"
        >
          <summary>
            <span class="report-state">{{ report.state }}</span>
            <span
              v-if="reportSummaryLabel(report)"
              class="report-summary-label"
            >
              {{ reportSummaryLabel(report) }}
            </span>
            <span class="report-summary-message">
              {{ reportMessagePreview(report) }}
            </span>
            <span class="report-summary-meta">
              <span class="report-time">{{ formatTime(report.created_at) }}</span>
              <span
                class="report-delta"
                :title="reportElapsedTitle(report, reportIndex)"
              >
                {{ reportElapsedLabel(report, reportIndex) }}
              </span>
            </span>
          </summary>
          <MarkdownContent
            class="report-message"
            link-markdown-paths
            :text="reportMessageForLang(report)"
            @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
          />
          <div
            v-if="report.changed_files.length > 0"
            class="report-files"
          >
            <button
              v-for="file in report.changed_files"
              :key="file"
              type="button"
              :class="['report-file-chip', { 'report-file-chip--clickable': isMarkdownArtifact(file) }]"
              :disabled="!isMarkdownArtifact(file)"
              @click="openMarkdownPreviewModal(file, report)"
            >
              {{ file }}
            </button>
          </div>
          <div
            v-if="report.validation"
            class="report-note"
          >
            <strong>Validation</strong>
            <MarkdownContent
              compact
              link-markdown-paths
              :text="report.validation"
              @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
            />
          </div>
          <div
            v-if="acceptanceChecksFor(report).length > 0"
            class="report-note"
          >
            <strong>Acceptance Check</strong>
            <ol class="acceptance-check-list">
              <li
                v-for="check in acceptanceChecksFor(report)"
                :key="`${check.criterion}-${check.status}`"
              >
                <span>{{ check.status }}</span>
                {{ check.criterion }} - {{ check.evidence }}
              </li>
            </ol>
          </div>
          <div
            v-if="profileResultsFor(report).length > 0"
            class="report-note"
          >
            <strong>Review Profiles</strong>
            <ol class="profile-result-list">
              <li
                v-for="result in profileResultsFor(report)"
                :key="`${result.profile}-${result.status}`"
              >
                <span>{{ result.status }}</span>
                {{ reviewProfileLabel(result.profile) }} - {{ result.evidence || 'No evidence recorded.' }}
                <template v-if="result.blocking_findings?.length">
                  Blocking: {{ result.blocking_findings.join('; ') }}
                </template>
              </li>
            </ol>
          </div>
          <div
            v-if="report.artifact_refs?.length"
            class="report-note"
          >
            <strong>Artifacts</strong>
            <div class="report-artifacts">
              <div
                v-for="artifact in report.artifact_refs"
                :key="artifact"
                class="report-artifact"
              >
                <span>{{ artifact }}</span>
                <button
                  v-if="isMarkdownArtifact(artifact)"
                  type="button"
                  class="artifact-preview-button"
                  :disabled="isArtifactPreviewLoading(report, artifact)"
                  @click="toggleArtifactPreview(report, artifact)"
                >
                  {{ artifactPreviewButtonLabel(report, artifact) }}
                </button>
              </div>
            </div>
            <div
              v-for="artifact in markdownArtifactRefs(report)"
              :key="`${artifact}-preview`"
              class="artifact-preview"
            >
              <template v-if="expandedArtifactKey === artifactPreviewKey(report, artifact)">
                <div
                  v-if="artifactPreviewErrors[artifactPreviewKey(report, artifact)]"
                  class="artifact-preview-status artifact-preview-error"
                >
                  {{ artifactPreviewErrors[artifactPreviewKey(report, artifact)] }}
                </div>
                <div
                  v-else-if="!artifactPreviews[artifactPreviewKey(report, artifact)]"
                  class="artifact-preview-status"
                >
                  Loading Markdown preview...
                </div>
                <template v-else>
                  <div class="artifact-preview-header">
                    <span>{{ artifactPreviews[artifactPreviewKey(report, artifact)].filename }}</span>
                    <span>{{ formatAttachmentSize(artifactPreviews[artifactPreviewKey(report, artifact)].size_bytes) }}</span>
                  </div>
                  <MarkdownContent
                    class="artifact-preview-content"
                    :text="artifactPreviews[artifactPreviewKey(report, artifact)].content"
                  />
                  <div
                    v-if="artifactPreviews[artifactPreviewKey(report, artifact)].truncated"
                    class="artifact-preview-status"
                  >
                    Preview truncated to the first 512 KB.
                  </div>
                </template>
              </template>
            </div>
          </div>
          <div
            v-if="report.confidence !== null && report.confidence !== undefined"
            class="report-note report-note--inline"
          >
            <strong>Confidence</strong>
            <span>{{ formatAutonomousScore(report.confidence) }}</span>
            <span v-if="report.requires_human_judgment">Human judgment required</span>
          </div>
          <div
            v-if="report.risks"
            class="report-note"
          >
            <strong>Risks</strong>
            <MarkdownContent
              compact
              link-markdown-paths
              :text="report.risks"
              @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
            />
          </div>
        </details>
      </li>
    </ol>
  </details>
</template>

<script setup lang="ts">
import MarkdownContent from '@/components/MarkdownContent.vue'
import {
  reportSummaryLabel,
  isMarkdownArtifact,
  acceptanceChecksFor,
  profileResultsFor,
  reviewProfileLabel,
  markdownArtifactRefs,
  artifactPreviewKey,
} from '@/features/workspace/presentation/reports'
import {
  formatAttachmentSize,
} from '@/utils/draftAttachments'
import type { WorkspaceTask } from '@/types'
defineProps<{ selectedTask: WorkspaceTask }>()
import {
  useWorkspaceTaskPresentationContext,
  useWorkspaceRuntimeContext,
  useWorkspaceArtifactsContext,
} from '../context'
const {
  selectedReports,
  hasBilingualReport,
  reportLang,
  setReportLang,
  selectedProgressTimeline,
  isLatestSelectedReport,
  reportMessagePreview,
  reportElapsedTitle,
  reportElapsedLabel,
  reportMessageForLang,
  formatAutonomousScore,
} = useWorkspaceTaskPresentationContext()
const {
  formatTime,
} = useWorkspaceRuntimeContext()
const {
  openMarkdownPreviewModal,
  isArtifactPreviewLoading,
  toggleArtifactPreview,
  artifactPreviewButtonLabel,
  expandedArtifactKey,
  artifactPreviewErrors,
  artifactPreviews,
} = useWorkspaceArtifactsContext()
</script>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.lang-toggle {
  display: inline-flex;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  overflow: hidden;
  text-transform: none;
}

.lang-toggle-btn {
  padding: 2px 8px;
  border: 0;
  background: transparent;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: 0.02em;
}

.lang-toggle-btn:hover {
  color: var(--ch-color-text);
}

.lang-toggle-btn.active {
  background: var(--ch-color-accent-strong);
  color: var(--ch-color-on-accent, #fff);
}

.detail-section-controls {
  margin-bottom: 10px;
}
</style>

<style scoped src="../styles/shared-attachments.css"></style>

<style scoped>
.timeline {
  list-style: none;
  margin: 12px 0 0;
  padding: 0 0 0 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-left: 1px solid var(--ch-color-border-muted);
}

.timeline > li {
  position: relative;
  min-width: 0;
}

.timeline > li::before {
  content: '';
  position: absolute;
  top: 16px;
  left: -16px;
  width: 7px;
  height: 7px;
  border: 2px solid var(--ch-color-surface);
  border-radius: 999px;
  background: var(--ch-color-accent);
}

.progress-overview {
  margin-top: 10px;
  overflow-x: auto;
  padding: 4px 0 6px;
}

.progress-overview-timeline {
  list-style: none;
  margin: 0;
  min-width: max-content;
  padding: 0;
  display: flex;
  align-items: stretch;
}

.progress-overview-item {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-areas: "dot dot"
    "main main"
    "time delta";
  column-gap: 8px;
  row-gap: 2px;
  min-width: 132px;
  max-width: 184px;
  padding: 0 18px 0 0;
  color: var(--ch-color-text-muted);
}

.progress-overview-item::after {
  content: '';
  position: absolute;
  top: 4px;
  right: 4px;
  left: 14px;
  height: 1px;
  background: var(--ch-color-border-muted);
}

.progress-overview-item:last-child::after {
  display: none;
}

.progress-overview-dot {
  grid-area: dot;
  position: relative;
  z-index: 1;
  width: 9px;
  height: 9px;
  border: 2px solid var(--ch-color-surface);
  border-radius: 999px;
  background: var(--ch-color-text-muted);
}

.progress-overview-main {
  grid-area: main;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-overflow: ellipsis;
  text-transform: capitalize;
  white-space: nowrap;
}

.progress-overview-time,
.progress-overview-delta {
  font-size: 10px;
  line-height: 1.2;
  white-space: nowrap;
}

.progress-overview-time {
  grid-area: time;
}

.progress-overview-delta {
  grid-area: delta;
  min-width: 0;
  color: var(--ch-color-text-subtle);
}

.progress-overview-item--task .progress-overview-dot {
  background: var(--ch-color-accent);
}

.progress-overview-item--report .progress-overview-dot {
  background: var(--ch-color-warning);
}

.progress-overview-item--terminal .progress-overview-dot {
  background: var(--ch-color-success);
}

.progress-overview-item--live .progress-overview-dot {
  background: var(--ch-color-danger);
}

.report-card {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 0;
}

.report-card summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  padding: 10px;
  color: var(--ch-color-text);
  font-size: 12px;
}

.report-card summary::-webkit-details-marker {
  display: none;
}

.report-card[open] summary {
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.report-state {
  font-weight: 700;
  color: var(--ch-color-text);
}

.report-time {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
}

.report-summary-meta {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  margin-left: auto;
}

.report-summary-label {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
}

.report-summary-message {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.report-delta {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  line-height: 1;
  padding: 3px 6px;
  white-space: nowrap;
}

.report-message {
  padding: 10px;
}

.report-files {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 0 10px 10px;
}

.report-file-chip {
  border: 0;
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 3px 7px;
}

.report-file-chip--clickable {
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.report-file-chip:disabled:not(.report-file-chip--clickable) {
  opacity: 1;
}
</style>

<style scoped src="../styles/shared-artifact-previews.css"></style>

<style scoped>
.report-artifacts {
  display: grid;
  gap: 6px;
}

.report-artifact {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.report-artifact > span {
  overflow-wrap: anywhere;
  color: var(--ch-color-text);
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-xs);
}
</style>

<style scoped>
.report-note {
  margin: 0 10px 10px;
  border-left: 2px solid var(--ch-color-border-hover);
  padding-left: 8px;
  color: var(--ch-color-text-muted);
}

.report-note strong {
  display: block;
  margin-bottom: 5px;
  color: var(--ch-color-text);
  font-size: 11px;
  text-transform: uppercase;
}

.acceptance-check-list,
.profile-result-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.45;
}

.acceptance-check-list li,
.profile-result-list li {
  overflow-wrap: anywhere;
}

.acceptance-check-list span,
.profile-result-list span {
  margin-right: 5px;
  color: var(--ch-color-text);
  font-weight: 700;
}

.report-note--inline {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}

.report-note--inline strong {
  display: inline-flex;
  margin: 0;
}

.report-note--inline > span {
  line-height: 1;
}
</style>
