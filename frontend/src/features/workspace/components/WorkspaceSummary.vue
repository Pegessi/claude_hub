<template>
  <div
    v-if="error"
    class="workspace-error"
  >
    <span>{{ error }}</span>
    <button
      type="button"
      class="workspace-error__close"
      aria-label="关闭错误提示"
      @click="dismissWorkspaceErrors"
    >
      ×
    </button>
  </div>

  <div
    v-if="activeWorkspaceId"
    class="workspace-summary-strip"
  >
    <div class="workspace-summary-primary">
      <span>{{ workspaceAgents.length }} agents</span>
      <span>{{ reviewerAgents.length + temporaryReviewers.length }} reviewers</span>
      <strong>{{ workspaceAgents.filter(agent => agent.runtime_status === 'working').length }} working</strong>
      <span>{{ tasksByStatus('queued').length }} queued</span>
      <button
        type="button"
        class="summary-chip-button"
        @click="openLessonsModal"
      >
        {{ activeFeedbackLessons.length }} lessons
      </button>
    </div>
    <div class="workspace-column-tabs">
      <span
        v-for="column in columns"
        :key="column.status"
      >
        {{ column.label }} {{ taskCountByStatus(column.status) }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  useWorkspaceRuntimeContext,
  useWorkspaceLessonsContext,
} from '../context'
const {
  error,
  dismissWorkspaceErrors,
  activeWorkspaceId,
  workspaceAgents,
  reviewerAgents,
  temporaryReviewers,
  tasksByStatus,
  activeFeedbackLessons,
  columns,
  taskCountByStatus,
} = useWorkspaceRuntimeContext()
const {
  openLessonsModal,
} = useWorkspaceLessonsContext()
</script>

<style scoped>
.workspace-summary-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-canvas);
}

.workspace-summary-primary,
.workspace-column-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.workspace-summary-primary {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.workspace-summary-primary strong {
  color: var(--ch-color-text);
}

.summary-chip-button {
  height: 26px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  padding: 0 10px;
  white-space: nowrap;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.summary-chip-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.summary-chip-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.workspace-column-tabs {
  overflow-x: auto;
}

.workspace-column-tabs span {
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text);
  font-size: 11px;
  padding: 4px 9px;
}
</style>

<style scoped>
.workspace-error {
  padding: 8px 16px;
  background: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
  font-size: 13px;
}
</style>

<style scoped>
@media (max-width: 760px) {
  .workspace-summary-strip {
    display: none;
  }
}
</style>
