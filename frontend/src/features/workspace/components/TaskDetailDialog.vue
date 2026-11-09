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
          <div class="detail-header-main">
            <span :class="['detail-eyebrow', `detail-eyebrow--${selectedTask.status}`]">
              <span class="detail-eyebrow-dot" />
              {{ taskStatusLabel(selectedTask.status) }}
            </span>
            <h2>{{ selectedTask.title }}</h2>
            <p
              v-if="evidence.outcome"
              :class="['detail-outcome', { 'detail-outcome--failure': evidence.outcomeIsFailure }]"
            >
              {{ evidence.outcome }}
            </p>
            <span class="detail-updated">Updated {{ formatTime(selectedTask.updated_at) }}</span>
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

        <div
          ref="bodyElement"
          class="detail-body"
        >
          <TaskEvidence :selected-task="selectedTask" />

          <section
            v-if="canUseManagedTaskActions(selectedTask)"
            ref="decisionSection"
            tabindex="-1"
            aria-label="Decision"
            :class="[
              'detail-section',
              'decision-section',
              { 'decision-section--attention': selectedTask.status === 'review' },
            ]"
          >
            <div class="detail-section-title">
              Decision
            </div>
            <p
              v-if="decisionHint"
              class="decision-hint"
            >
              {{ decisionHint }}
            </p>
            <TaskDetailActions :selected-task="selectedTask" />
          </section>

          <TaskOverview :selected-task="selectedTask" />

          <TaskReports :selected-task="selectedTask" />

          <TaskMarkdownDocuments :selected-task="selectedTask" />

          <!-- Execution control stays actionable (progress reporting, handoff,
               reporter credential); its internal bookkeeping is folded inside. -->
          <section
            v-if="activeWorkspaceId"
            class="execution-section"
            aria-label="Task execution"
          >
            <TaskExecutionPanel
              :key="`${activeWorkspaceId}:${selectedTask.id}`"
              :workspace-id="activeWorkspaceId"
              :task="selectedTask"
              :capabilities="taskCapabilities"
              :debug-open="false"
              :show-progress="false"
            />
          </section>
        </div>

        <!-- The old sticky action footer guaranteed an always-reachable
             decision. Keeping that promise only where it matters: while a task
             waits for review, a compact bar stays at the panel edge and jumps
             to the decision (it disappears once the decision is on screen, so
             the actions are not duplicated). -->
        <div
          v-if="showReviewBar"
          class="detail-review-bar"
        >
          <span class="detail-review-text">{{ decisionHint || 'Waiting for your decision' }}</span>
          <button
            type="button"
            class="detail-review-jump"
            @click="revealDecision"
          >
            Review decision
          </button>
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import {
  computed,
  onUnmounted,
  ref,
  watch,
} from 'vue'
import TaskExecutionPanel from '@/components/TaskExecutionPanel.vue'
import TaskEvidence from './TaskEvidence.vue'
import TaskOverview from './TaskOverview.vue'
import TaskReports from './TaskReports.vue'
import TaskMarkdownDocuments from './TaskMarkdownDocuments.vue'
import TaskDetailActions from './TaskDetailActions.vue'
import { taskStatusLabel } from '../presentation/board'
import { taskEvidence } from '../presentation/evidence'
import {
  useWorkspaceRuntimeContext,
  useWorkspaceTaskActionsContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
const {
  selectedTask,
  activeWorkspaceId,
  taskCapabilities,
  formatTime,
} = useWorkspaceRuntimeContext()
const {
  closeTaskDetail,
  isDetailActionsExpanded,
} = useWorkspaceTaskActionsContext()
const {
  canUseManagedTaskActions,
  reviewStatusLabel,
  selectedReports,
} = useWorkspaceTaskPresentationContext()

const evidence = computed(() =>
  selectedTask.value
    ? taskEvidence(selectedTask.value, selectedReports.value)
    : { outcome: '', outcomeIsFailure: false },
)

const bodyElement = ref<HTMLElement | null>(null)
const decisionSection = ref<HTMLElement | null>(null)
const decisionInView = ref(false)
let decisionObserver: IntersectionObserver | null = null

function stopDecisionObserver(): void {
  decisionObserver?.disconnect()
  decisionObserver = null
}

// Watch the decision inside the dialog's own scroll container rather than the
// viewport: the body, not the window, is what scrolls here.
watch(decisionSection, (element) => {
  stopDecisionObserver()
  if (!element || typeof IntersectionObserver === 'undefined') {
    decisionInView.value = false
    return
  }
  decisionObserver = new IntersectionObserver(
    (entries) => {
      decisionInView.value = entries[0]?.isIntersecting ?? false
    },
    { root: bodyElement.value, threshold: 0.2 },
  )
  decisionObserver.observe(element)
})

onUnmounted(stopDecisionObserver)

const showReviewBar = computed(() => {
  const task = selectedTask.value
  if (!task || task.status !== 'review') return false
  return canUseManagedTaskActions(task) && !decisionInView.value
})

function revealDecision(): void {
  const target = decisionSection.value
  if (!target) return
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches
  target.scrollIntoView({ block: 'start', behavior: reduceMotion ? 'auto' : 'smooth' })
  // Focus the region so keyboard and screen-reader users land on the decision.
  target.focus({ preventScroll: true })
}

// The Decision section answers "what am I being asked to decide?" using the
// same review wording the board badge shows, so both surfaces agree.
const decisionHint = computed(() => {
  if (!selectedTask.value) return ''
  const label = reviewStatusLabel(selectedTask.value)
  if (label) return label
  return selectedTask.value.status === 'review' ? 'This task is waiting for your decision.' : ''
})
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

.detail-header-main {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.detail-header h2 {
  margin: 2px 0 0;
  color: var(--ch-color-text-strong);
  font-size: 18px;
  line-height: 1.3;
}

/* The eyebrow is a state pill: sentence case, tone carried by the dot only,
 * so the accent stays reserved for the primary decision control. */
.detail-eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: fit-content;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.01em;
  line-height: 1;
  padding: 4px 9px;
  text-transform: none;
}

.detail-eyebrow-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--ch-color-text-subtle);
}

.detail-eyebrow--queued .detail-eyebrow-dot {
  background: var(--ch-color-info);
}

.detail-eyebrow--working .detail-eyebrow-dot {
  background: var(--ch-color-warning-strong);
}

.detail-eyebrow--review .detail-eyebrow-dot {
  background: var(--ch-color-attention-strong);
}

.detail-eyebrow--done .detail-eyebrow-dot {
  background: var(--ch-color-success-strong);
}

.detail-eyebrow--failed {
  border-color: color-mix(in srgb, var(--ch-color-danger) 55%, transparent);
  color: var(--ch-color-danger);
}

.detail-eyebrow--failed .detail-eyebrow-dot {
  background: var(--ch-color-danger);
}

.detail-outcome {
  margin: 2px 0 0;
  display: -webkit-box;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.4;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
}

.detail-outcome--failure {
  color: var(--ch-color-danger);
}

.detail-updated {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
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

.decision-section {
  display: grid;
  gap: 10px;
  border-color: var(--ch-color-border-strong);
}

.decision-section--attention {
  border-color: var(--ch-color-attention-strong);
  background: color-mix(in srgb, var(--ch-color-attention-strong) 6%, var(--ch-color-surface));
}

.decision-hint {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

/* The panel owns the card and its "Execution control" heading; wrapping it in
 * another bordered section produced two nested boxes and two headings. */
.execution-section {
  display: grid;
}

.detail-review-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-top: 1px solid color-mix(in srgb, var(--ch-color-attention-strong) 55%, transparent);
  background: color-mix(in srgb, var(--ch-color-attention-strong) 8%, var(--ch-color-surface-raised));
  padding: 8px 18px;
}

.detail-review-text {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-review-jump {
  flex: 0 0 auto;
  height: 30px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  padding: 0 12px;
  white-space: nowrap;
}

.detail-review-jump:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.detail-review-jump:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  border-color: var(--ch-color-accent);
}

.decision-section:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
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

  .detail-review-bar {
    padding: 8px 12px max(8px, env(safe-area-inset-bottom));
  }

  .detail-review-jump {
    height: 44px;
    min-height: 44px;
  }
}
</style>
