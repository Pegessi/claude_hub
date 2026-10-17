<template>
  <section class="detail-section">
    <div class="detail-section-title">
      Task description
    </div>
    <MarkdownContent
      class="detail-copy"
      link-markdown-paths
      :text="selectedTask.prompt"
      @markdown-path-click="path => openMarkdownPreviewModal(path)"
    />
    <div
      v-if="selectedTask.attachments.length > 0"
      class="attachment-list attachment-list--readonly"
    >
      <div
        v-for="attachment in selectedTask.attachments"
        :key="attachment.id"
        class="attachment-row"
      >
        <button
          type="button"
          class="attachment-thumb attachment-thumb--clickable"
          :aria-label="`Preview ${attachment.filename}`"
          @click="openImageLightbox(`/api/workspaces/attachments/${attachment.id}`, attachment.filename)"
        >
          <img
            :src="`/api/workspaces/attachments/${attachment.id}`"
            :alt="attachment.filename"
          >
        </button>
        <div class="attachment-meta">
          <strong>{{ attachment.filename }}</strong>
          <span>{{ persistedAttachmentMeta(attachment) }}</span>
          <code>{{ attachment.path }}</code>
        </div>
      </div>
    </div>
  </section>

  <details class="detail-section detail-section--collapsible">
    <summary class="detail-section-title">
      Goal Packet
    </summary>
    <div
      v-if="!selectedTask.goal_packet"
      class="empty-timeline"
    >
      No goal packet recorded yet.
    </div>
    <div
      v-else
      class="goal-packet"
    >
      <div class="goal-packet-objective">
        <span>Objective</span>
        <MarkdownContent
          compact
          :text="selectedTask.goal_packet.objective"
        />
      </div>
      <div class="goal-packet-meta">
        <span>{{ goalPacketStatusLabel(selectedTask.goal_packet.status) }}</span>
        <span>{{ selectedTask.goal_packet.source || 'agent_generated' }}</span>
      </div>
      <div
        v-for="section in goalPacketSections(selectedTask.goal_packet)"
        :key="section.key"
        class="goal-packet-section"
      >
        <strong>{{ section.label }}</strong>
        <ol v-if="section.items.length > 0">
          <li
            v-for="item in section.items"
            :key="item"
          >
            {{ item }}
          </li>
        </ol>
        <span
          v-else
          class="goal-packet-empty"
        >
          none
        </span>
      </div>
    </div>
  </details>

  <details class="detail-section detail-section--collapsible">
    <summary class="detail-section-title">
      Assignment
    </summary>
    <div class="fact-grid">
      <div>
        <span>Mode</span>
        <strong>{{ taskModeLabel(selectedTask.task_mode) }}</strong>
      </div>
      <div>
        <span>Execution</span>
        <strong>{{ executionComplexityLabel(selectedTask.execution_complexity) }}</strong>
      </div>
      <div>
        <span>Task stage</span>
        <strong>{{ selectedTask.status }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Agent runtime</span>
        <strong>{{ selectedSession?.runtime_status || 'none' }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Agent</span>
        <strong>{{ selectedSession?.title || 'auto' }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Reviewer</span>
        <strong>{{ selectedTask.review_session_id ? reviewerTitle(selectedTask.review_session_id) : 'none' }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Review state</span>
        <strong>{{ reviewStatusLabel(selectedTask) || 'not requested' }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Goal Packet gate</span>
        <strong>{{ goalPacketGateLabel(selectedTask) }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Review profiles</span>
        <strong>{{ taskReviewProfiles(selectedTask) }}</strong>
      </div>
      <div v-if="selectedTask.human_acceptance_requested_at">
        <span>Human acceptance</span>
        <strong>{{ selectedTask.human_accepted_at ? 'accepted' : 'awaiting' }}</strong>
      </div>
      <div v-if="selectedTask.review_skip_reason">
        <span>Review reason</span>
        <strong>{{ selectedTask.review_skip_reason }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Queued behind</span>
        <strong>{{ selectedSession?.queued_count || 0 }}</strong>
      </div>
      <div v-if="canUseManagedTaskActions(selectedTask)">
        <span>Clear context</span>
        <strong>{{ selectedTask.clear_context ? 'yes' : 'no' }}</strong>
      </div>
      <div>
        <span>Snapshot</span>
        <strong>{{ board?.snapshot_path || 'none' }}</strong>
      </div>
    </div>
  </details>

  <TaskExecutionPanel
    v-if="activeWorkspaceId"
    :key="`${activeWorkspaceId}:${selectedTask.id}`"
    :workspace-id="activeWorkspaceId"
    :task="selectedTask"
    :capabilities="taskCapabilities"
  />

  <details
    v-if="selectedTask.task_mode === 'autonomous'"
    class="detail-section detail-section--collapsible autonomous-run-panel"
  >
    <summary class="detail-section-title">
      Autonomous Run
    </summary>
    <div
      v-if="!selectedTask.autonomous_run"
      class="empty-timeline"
    >
      No autonomous run recorded yet.
    </div>
    <template v-else>
      <div class="fact-grid">
        <div>
          <span>Phase</span>
          <strong>{{ autonomousRunPhaseLabel(selectedTask.autonomous_run.phase) }}</strong>
        </div>
        <div>
          <span>Iteration</span>
          <strong>{{ selectedTask.autonomous_run.iteration }} / {{ selectedTask.autonomous_run.max_iterations }}</strong>
        </div>
        <div>
          <span>Score</span>
          <strong>{{ formatAutonomousScore(selectedTask.autonomous_run.current_score) }}</strong>
        </div>
        <div>
          <span>Threshold</span>
          <strong>{{ formatAutonomousScore(selectedTask.autonomous_run.pass_threshold) }}</strong>
        </div>
        <div>
          <span>Strictness</span>
          <strong>{{ selectedTask.autonomy_policy?.evaluation_strictness || 'balanced' }}</strong>
        </div>
        <div>
          <span>Artifacts</span>
          <strong>{{ selectedTask.autonomy_policy?.require_artifact_review ? 'required' : 'optional' }}</strong>
        </div>
        <div>
          <span>Total elapsed</span>
          <strong>{{ taskTotalElapsedLabel(selectedTask) }}</strong>
        </div>
        <div>
          <span>Working elapsed</span>
          <strong>{{ taskWorkingElapsedLabel(selectedTask) }}</strong>
        </div>
        <div>
          <span>Latest report age</span>
          <strong>{{ latestSelectedReportAgeLabel }}</strong>
        </div>
      </div>
      <div class="autonomous-next-action">
        <span>Next action</span>
        <strong>{{ selectedTask.autonomous_run.next_action }}</strong>
      </div>
      <div
        v-if="(selectedTask.autonomous_run.evaluation_reports || []).length > 0"
        class="autonomous-evaluations"
      >
        <strong>Evaluations</strong>
        <ol>
          <li
            v-for="evaluation in selectedTask.autonomous_run.evaluation_reports"
            :key="evaluation.id"
          >
            <span>{{ evaluation.decision }}</span>
            <span>round {{ evaluation.iteration }}</span>
            <span>{{ formatAutonomousScore(evaluation.overall_score) }}</span>
            <span
              v-if="evaluation.profile_results?.length"
              class="profile-summary"
            >
              {{ profileResultSummary(evaluation.profile_results) }}
            </span>
          </li>
        </ol>
      </div>
    </template>
  </details>
</template>

<script setup lang="ts">
import MarkdownContent from '@/components/MarkdownContent.vue'
import TaskExecutionPanel from '@/components/TaskExecutionPanel.vue'
import {
  goalPacketStatusLabel,
  goalPacketSections,
  goalPacketGateLabel,
  taskReviewProfiles,
  profileResultSummary,
} from '@/features/workspace/presentation/reports'
import type { WorkspaceTask } from '@/types'
defineProps<{ selectedTask: WorkspaceTask }>()
import {
  useWorkspaceArtifactsContext,
  useWorkspaceRuntimeContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
const {
  openMarkdownPreviewModal,
  openImageLightbox,
} = useWorkspaceArtifactsContext()
const {
  persistedAttachmentMeta,
  reviewerTitle,
  board,
  activeWorkspaceId,
  taskCapabilities,
} = useWorkspaceRuntimeContext()
const {
  taskModeLabel,
  executionComplexityLabel,
  canUseManagedTaskActions,
  selectedSession,
  reviewStatusLabel,
  autonomousRunPhaseLabel,
  formatAutonomousScore,
  taskTotalElapsedLabel,
  taskWorkingElapsedLabel,
  latestSelectedReportAgeLabel,
} = useWorkspaceTaskPresentationContext()
</script>

<style scoped src="../styles/shared-board-layout.css"></style>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.detail-copy {
  margin: 8px 0 0;
}

.fact-grid {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.fact-grid div {
  min-width: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.fact-grid span {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.fact-grid strong {
  display: block;
  margin-top: 4px;
  color: var(--ch-color-text);
  font-size: 12px;
  overflow-wrap: anywhere;
}
</style>

<style scoped src="../styles/shared-attachments.css"></style>

<style scoped>
.attachment-list--readonly .attachment-row {
  grid-template-columns: 54px minmax(0, 1fr);
}

.attachment-thumb--clickable {
  /* Rendered as a <button> for keyboard/click affordance; strip the native
     button chrome so it matches the surrounding thumbnail styling. */
  padding: 0;
  flex: none;
  cursor: pointer;
  transition: border-color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.attachment-thumb--clickable:hover {
  border-color: var(--ch-color-accent);
  transform: scale(1.04);
}

.attachment-thumb--clickable:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}
</style>

<style scoped>
.goal-packet {
  margin-top: 10px;
  display: grid;
  gap: 10px;
  min-width: 0;
}

.goal-packet-objective,
.goal-packet-section {
  min-width: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 10px;
}

.goal-packet-objective span,
.goal-packet-section strong {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.goal-packet-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.goal-packet-meta span {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  padding: 3px 7px;
}

.goal-packet-section ol {
  margin: 0;
  padding-left: 18px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.goal-packet-section li {
  overflow-wrap: anywhere;
}

.goal-packet-empty {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.autonomous-run-panel .fact-grid {
  margin-bottom: 10px;
}

.autonomous-next-action {
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  padding: 10px 12px;
}

.autonomous-next-action span,
.autonomous-evaluations > strong {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 5px;
  text-transform: uppercase;
}

.autonomous-next-action strong {
  color: var(--ch-color-text);
  font-size: 13px;
}

.autonomous-evaluations {
  margin-top: 10px;
}

.autonomous-evaluations ol {
  display: grid;
  gap: 6px;
  list-style: none;
  margin: 0;
  padding: 0;
}

.autonomous-evaluations li {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  padding: 8px 10px;
}

.autonomous-evaluations span {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.autonomous-evaluations .profile-summary {
  flex-basis: 100%;
  color: var(--ch-color-text);
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .fact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
