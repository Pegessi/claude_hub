<template>
  <Teleport to="body">
    <div
      v-if="selectedTask"
      class="task-detail-overlay"
      @click.self="closeTaskDetail"
    >
      <aside
        :class="['task-detail-panel', { 'mobile-actions-expanded': isDetailActionsExpanded }]"
        role="dialog"
        aria-modal="true"
        :aria-label="selectedTask.title"
      >
        <div class="detail-header">
          <div>
            <span class="detail-eyebrow">{{ selectedTask.status }}</span>
            <h2>{{ selectedTask.title }}</h2>
          </div>
          <button
            type="button"
            class="icon-button"
            aria-label="Close task detail"
            @click="closeTaskDetail"
          >
            ×
          </button>
        </div>

        <div class="detail-body">
          <TaskOverview :selected-task="selectedTask" />

          <TaskReports :selected-task="selectedTask" />

          <TaskMarkdownDocuments :selected-task="selectedTask" />
        </div>

        <TaskDetailActions :selected-task="selectedTask" />
      </aside>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import TaskOverview from './TaskOverview.vue'
import TaskReports from './TaskReports.vue'
import TaskMarkdownDocuments from './TaskMarkdownDocuments.vue'
import TaskDetailActions from './TaskDetailActions.vue'
import {
  useWorkspaceRuntimeContext,
  useWorkspaceTaskActionsContext,
} from '../context'
const {
  selectedTask,
} = useWorkspaceRuntimeContext()
const {
  closeTaskDetail,
  isDetailActionsExpanded,
} = useWorkspaceTaskActionsContext()
</script>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.task-detail-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  overscroll-behavior: contain;
  background: var(--ch-color-overlay);
  padding: 24px;
}

.task-detail-panel {
  width: min(1040px, calc(100vw - 48px));
  max-height: min(860px, calc(100dvh - 48px));
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-dialog);
}

.detail-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
}

.detail-header h2 {
  margin: 4px 0 0;
  color: var(--ch-color-text-strong);
  font-size: 18px;
}

.detail-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  padding: 16px 18px;
}
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .task-detail-overlay {
    align-items: stretch;
    overflow: hidden;
    padding: 0;
  }

  .task-detail-panel {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0;
    border-left: 0;
    border-right: 0;
    -webkit-overflow-scrolling: touch;
  }

  .detail-header {
    position: sticky;
    top: 0;
    z-index: 2;
    align-items: center;
    background: var(--ch-color-surface-raised);
    padding: 12px;
  }

  .detail-header h2 {
    font-size: 17px;
  }

  .detail-body {
    flex: 1;
    min-height: 0;
    max-height: none;
    padding: 12px;
  }

  .task-detail-panel.mobile-actions-expanded .detail-body {
    max-height: none;
  }
}
</style>
