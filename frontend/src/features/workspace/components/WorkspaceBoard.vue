<template>
  <div class="workspace-layout">
    <main class="board">
      <div
        v-if="!activeWorkspaceId"
        class="empty-board"
      >
        Create a workspace to start agents and queue tasks.
      </div>

      <template v-else>
        <transition name="board-skeleton-fade">
          <div
            v-if="boardLoading"
            class="board-skeleton"
            role="status"
            aria-live="polite"
            aria-label="Loading workspace"
          >
            <div
              v-for="column in columns"
              :key="`skeleton-${column.status}`"
              class="board-skeleton-column"
            >
              <div class="board-skeleton-header">
                <span class="board-skeleton-line board-skeleton-line--title" />
              </div>
              <div class="board-skeleton-list">
                <div
                  v-for="card in 3"
                  :key="card"
                  class="board-skeleton-card"
                >
                  <span class="board-skeleton-line board-skeleton-line--lg" />
                  <span class="board-skeleton-line board-skeleton-line--sm" />
                  <span class="board-skeleton-line board-skeleton-line--md" />
                </div>
              </div>
            </div>
          </div>
        </transition>

        <section
          v-for="column in columns"
          :key="column.status"
          :class="[
            'task-column',
            {
              'task-column--empty': tasksByStatus(column.status).length === 0,
              'task-column--collapsed': isMobileColumnCollapsed(column.status),
            },
          ]"
        >
          <div class="column-header">
            <h2>{{ column.label }}</h2>
            <div class="column-meta">
              <span>{{ taskCountByStatus(column.status) }}</span>
              <button
                type="button"
                class="column-collapse-button"
                :aria-expanded="!isMobileColumnCollapsed(column.status)"
                @click="toggleMobileColumn(column.status)"
              >
                {{ isMobileColumnCollapsed(column.status) ? 'Show' : 'Hide' }}
              </button>
            </div>
          </div>
          <div class="task-list">
            <article
              v-for="task in tasksByStatus(column.status)"
              :key="task.id"
              :class="[
                'task-card',
                `task-card--${task.status}`,
                { selected: selectedTaskId === task.id },
              ]"
              @click="selectTask($event, task.id)"
            >
              <div class="task-card-header">
                <h3>{{ task.title }}</h3>
                <span class="task-card-badges">
                  <span
                    v-if="task.origin === 'resident'"
                    class="origin-badge"
                    title="Created by the resident agent"
                  >
                    Agent
                  </span>
                  <span
                    v-if="task.agent_tag"
                    class="agent-tag-badge"
                    :title="`Agent tag: ${task.agent_tag}`"
                  >
                    {{ task.agent_tag }}
                  </span>
                  <span
                    v-if="task.task_mode === 'autonomous'"
                    class="autonomy-badge"
                    :title="task.autonomous_run?.next_action || 'Autonomous run'"
                  >
                    Auto {{ task.autonomous_run?.iteration || 1 }}/{{ task.autonomous_run?.max_iterations || task.autonomy_policy?.max_iterations || 3 }}
                  </span>
                  <span
                    v-if="task.status === 'working' && activeReviewBadge(task)"
                    :class="[
                      'review-badge',
                      `review-badge--${activeReviewBadge(task)?.kind}`,
                    ]"
                    :title="activeReviewBadge(task)?.title"
                  >
                    <span class="review-badge-dot" />
                    {{ activeReviewBadge(task)?.label }}
                  </span>
                  <span
                    v-else
                    class="agent-badge"
                  >
                    <span :class="['status-dot', `status-dot--${task.status}`]" />
                    {{ task.status }}
                  </span>
                </span>
              </div>
              <p class="task-card-description">
                {{ task.prompt }}
              </p>
              <div
                v-if="canUseManagedTaskActions(task) && task.dispatch_pending"
                class="latest-report"
              >
                Waiting for dispatcher decision
              </div>
              <div
                v-else-if="canUseManagedTaskActions(task) && task.dispatch_reason"
                class="latest-report"
              >
                {{ task.dispatch_reason }}
              </div>
              <div
                v-if="canUseManagedTaskActions(task) && task.status === 'failed' && task.failure_reason"
                class="latest-report"
              >
                <strong>failed</strong>
                <span>{{ task.failure_reason }}</span>
              </div>
              <div
                v-if="task.latest_progress"
                class="latest-report"
              >
                <strong>{{ progressStateLabel(task.latest_progress.state) }}</strong>
                <span>{{ task.latest_progress.summary }}</span>
              </div>
              <div
                v-else-if="canUseManagedTaskActions(task) && latestReportForTask(task)"
                class="latest-report"
              >
                <strong>{{ latestReportForTask(task)?.state }}</strong>
                <span>{{ reportMessageForLang(latestReportForTask(task)!) }}</span>
              </div>
              <div class="session-meta">
                <span>{{ taskSourceLabel(task.source) }}</span>
                <span>{{ taskExecutionLabel(task) }}</span>
              </div>
              <details
                v-if="canUseManagedTaskActions(task) && task.status === 'todo'"
                class="advanced-start"
                @click.stop
              >
                <summary>Dispatch options</summary>
                <label>
                  Agent
                  <select v-model="startOptionsFor(task).target_session_id">
                    <option value="">Auto</option>
                    <option
                      v-for="agent in workspaceAgents"
                      :key="agent.id"
                      :value="agent.id"
                    >
                      {{ agent.title }}
                    </option>
                  </select>
                </label>
                <label>
                  Related task
                  <span class="field-hint-inline">Session/context reuse hint — not a Task parent.</span>
                  <select v-model="startOptionsFor(task).related_task_id">
                    <option value="">Auto</option>
                    <option
                      v-for="candidate in tasks.filter(item => item.id !== task.id)"
                      :key="candidate.id"
                      :value="candidate.id"
                    >
                      {{ candidate.title }}
                    </option>
                  </select>
                </label>
                <label class="checkbox-label">
                  <input
                    v-model="startOptionsFor(task).clear_context"
                    type="checkbox"
                  >
                  Clear context
                </label>
              </details>
              <div
                v-if="canUseManagedTaskActions(task)"
                class="task-actions"
              >
                <LoadingButton
                  v-if="canUseManagedTaskActions(task) && task.status === 'todo'"
                  type="button"
                  :loading="isPending(taskActionKey('start', task.id))"
                  loading-label="Starting task"
                  @click.stop="startTask(task)"
                >
                  Start
                </LoadingButton>
                <button
                  v-if="canEditTask(task)"
                  type="button"
                  class="tool-button"
                  @click.stop="openEditTaskModal(task)"
                >
                  Edit
                </button>
                <LoadingButton
                  v-if="canMarkDoneTask(task)"
                  type="button"
                  :loading="isPending(taskActionKey('mark-done', task.id))"
                  loading-label="Marking done"
                  @click.stop="markTask(task.id, 'done')"
                >
                  Done
                </LoadingButton>
                <LoadingButton
                  v-if="canRequestReviewTask(task)"
                  type="button"
                  :loading="isPending(taskActionKey('request-review', task.id))"
                  loading-label="Requesting review"
                  @click.stop="requestReview(task)"
                >
                  Request review
                </LoadingButton>
                <LoadingButton
                  v-if="canAbortTask(task)"
                  type="button"
                  class="abort-button"
                  :loading="isPending(taskActionKey('abort', task.id))"
                  loading-label="Aborting task"
                  @click.stop="abortTask(task)"
                >
                  Abort
                </LoadingButton>
                <LoadingButton
                  v-if="sessionForTask(task)"
                  type="button"
                  :loading="isPending(sessionActionKey('open', task.session_id))"
                  loading-label="Opening tab"
                  @click.stop="openSession(sessionForTask(task)!)"
                >
                  Open tab
                </LoadingButton>
                <LoadingButton
                  type="button"
                  class="danger-button"
                  :loading="isPending(taskActionKey('delete', task.id))"
                  loading-label="Deleting task"
                  @click.stop="deleteTask(task)"
                >
                  Delete
                </LoadingButton>
              </div>
            </article>
            <div
              v-if="tasksByStatus(column.status).length === 0"
              class="column-empty"
            >
              No tasks
            </div>
          </div>
          <footer
            v-if="column.status === 'done' && boardTasksPagination"
            class="board-history-footer"
          >
            <div
              v-if="boardOlderTasksRemaining > 0"
              class="board-history-actions"
            >
              <button
                type="button"
                class="secondary-button board-history-load-more"
                :disabled="boardLoadMoreLoading"
                @click="handleLoadOlderTasks"
              >
                {{
                  boardLoadMoreLoading
                    ? 'Loading older tasks…'
                    : `Show older done (${boardOlderTasksRemaining} more)`
                }}
              </button>
              <button
                v-if="boardLoadMoreError"
                type="button"
                class="secondary-button board-history-retry"
                @click="handleLoadOlderTasks"
              >
                Retry
              </button>
              <span
                v-if="boardLoadMoreError"
                class="board-history-error"
                role="alert"
              >
                {{ boardLoadMoreError }}
              </span>
            </div>
          </footer>
        </section>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  progressStateLabel,
  taskSourceLabel,
  taskExecutionLabel,
} from '@/utils/taskExecution'
import {
  useWorkspaceRuntimeContext,
  useWorkspaceTaskActionsContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
const {
  activeWorkspaceId,
  boardLoading,
  columns,
  tasksByStatus,
  isMobileColumnCollapsed,
  taskCountByStatus,
  toggleMobileColumn,
  selectedTaskId,
  latestReportForTask,
  workspaceAgents,
  tasks,
  isPending,
  taskActionKey,
  sessionForTask,
  sessionActionKey,
  openSession,
  boardTasksPagination,
  boardOlderTasksRemaining,
  boardLoadMoreLoading,
  handleLoadOlderTasks,
  boardLoadMoreError,
} = useWorkspaceRuntimeContext()
const {
  selectTask,
  startOptionsFor,
  startTask,
  openEditTaskModal,
  markTask,
  requestReview,
  abortTask,
  deleteTask,
} = useWorkspaceTaskActionsContext()
const {
  activeReviewBadge,
  canUseManagedTaskActions,
  reportMessageForLang,
  canEditTask,
  canMarkDoneTask,
  canRequestReviewTask,
  canAbortTask,
} = useWorkspaceTaskPresentationContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped>
.workspace-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
}

.column-header h2,
.task-card h3 {
  margin: 0;
}

.advanced-start label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
}

.advanced-start select {
  height: 34px;
  padding: 0 10px;
}
</style>

<style scoped src="../styles/shared-board-layout.css"></style>

<style scoped>
.board-skeleton {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: repeat(5, minmax(220px, 1fr));
  gap: 12px;
  padding: 14px;
  background: var(--ch-color-surface-sunken);
  overflow: hidden;
}

.board-skeleton-column {
  min-width: 220px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  overflow: hidden;
}

.board-skeleton-header {
  padding: 11px 12px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
}

.board-skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
}

.board-skeleton-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

.board-skeleton-line {
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    var(--ch-color-border-muted) 25%,
    var(--ch-color-surface-raised) 37%,
    var(--ch-color-border-muted) 63%
  );
  background-size: 400% 100%;
  animation: board-skeleton-shimmer 1.4s ease-in-out infinite;
}

.board-skeleton-line--title {
  width: 45%;
  height: 12px;
}

.board-skeleton-line--lg {
  width: 85%;
}

.board-skeleton-line--md {
  width: 60%;
}

.board-skeleton-line--sm {
  width: 35%;
  height: 8px;
}

@keyframes board-skeleton-shimmer {
  0% {
    background-position: 100% 50%;
  }

  100% {
    background-position: 0 50%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .board-skeleton-line {
    animation: none;
  }
}

.empty-board {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ch-color-text-muted);
}

.board-history-footer {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  padding: 8px 10px;
  border-top: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
}

.board-history-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.board-history-load-more,
.board-history-retry {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  white-space: nowrap;
}

.board-history-error {
  font-size: 11px;
  color: var(--ch-color-danger, #c0392b);
}

.task-column {
  min-width: 220px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-card);
  overflow: hidden;
}

.column-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 12px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
}

.column-header h2 {
  color: var(--ch-color-text-strong);
  font-size: 13px;
}

.column-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.column-header span {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.column-collapse-button {
  display: none;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  padding: 10px;
}
</style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/workspace-board-task-cards.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .workspace-layout {
    display: block;
    min-height: auto;
  }

  .board-skeleton {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 8px;
  }

  .board-skeleton-column:nth-child(n + 3) {
    display: none;
  }

  .task-column {
    min-width: 0;
    border-radius: var(--ch-radius-md);
  }

  .task-column--collapsed .task-list {
    display: none;
  }

  .task-column--collapsed .column-header {
    border-bottom: 0;
  }

  .task-column--empty .task-list {
    display: none;
  }

  .task-column--empty .column-header {
    border-bottom: 0;
  }

  .column-header {
    padding: 8px 10px;
  }

  .column-meta {
    gap: 6px;
  }

  .column-collapse-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 28px;
    min-width: 54px;
    border: 1px solid var(--ch-color-border-strong);
    border-radius: var(--ch-radius-md);
    background: var(--ch-color-surface-control-active);
    color: var(--ch-color-text);
    font-size: var(--ch-font-size-sm);
    font-weight: 500;
    padding: 0 8px;
    cursor: pointer;
    -webkit-tap-highlight-color: var(--ch-color-accent-halo);
    transition: all var(--ch-motion-fast);
  }

  .column-collapse-button:focus-visible {
    outline: none;
    box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
    border-color: var(--ch-color-accent);
  }

  .column-collapse-button:active {
    background: var(--ch-color-surface-pressed);
    transform: translateY(1px);
  }

  .task-list {
    overflow: visible;
    padding: 8px;
  }

  .task-card {
    padding: 10px 10px 10px 12px;
  }

  .task-card.selected {
    border-color: var(--ch-color-accent);
    background: var(--ch-color-surface-selected);
  }

  .task-card-header {
    align-items: center;
  }

  .task-card-description {
    display: -webkit-box;
    overflow: hidden;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
  }

  .agent-badge,
  .session-meta span {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .task-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  .advanced-start select {
    width: 100%;
    font-size: 16px;
  }
}

@media (max-width: 480px) {
  .task-actions {
    grid-template-columns: 1fr;
  }
}
</style>

<style scoped src="../styles/shared-switch-env.css"></style>
