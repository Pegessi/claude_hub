<template>
  <div class="tp-shell">
    <div
      v-if="!activeWorkspaceId"
      class="tp-empty-workspace"
    >
      <div class="tp-empty-card">
        <h2>No Workspace Selected</h2>
        <p>Create or select a Git repository workspace above to triage tasks and review agent runs.</p>
      </div>
    </div>

    <div
      v-else
      class="tp-linear"
    >
      <!-- ==================== LEFT TRIAGE RAIL (360px) ==================== -->
      <aside
        class="tp-rail"
        aria-label="Workspace tasks"
      >
        <div
          v-if="boardLoading && triageItems.length === 0"
          class="tp-rail-loading"
          role="status"
          aria-live="polite"
        >
          Loading tasks…
        </div>

        <div
          v-else-if="triageItems.length === 0"
          class="tp-rail-empty"
        >
          <p>No tasks in {{ activeWorkspace?.name || 'this workspace' }} yet.</p>
          <button
            type="button"
            class="tp-primary-btn"
            @click="openTaskModal"
          >
            Add Task
          </button>
        </div>

        <div
          v-else
          class="tp-groups"
        >
          <section
            v-for="group in groupedTasks"
            :key="group.key"
            class="tp-group"
          >
            <header class="tp-group-header">
              <span>{{ group.label }}</span>
              <span class="tp-num">{{ group.items.length }}</span>
            </header>

            <div
              v-if="group.items.length === 0"
              class="tp-group-empty"
            >
              No {{ group.label.toLowerCase() }} tasks
            </div>

            <button
              v-for="item in group.items"
              :key="item.id"
              type="button"
              :class="['tp-row', { active: activeItem?.id === item.id }]"
              :aria-current="activeItem?.id === item.id ? 'true' : undefined"
              :data-task-id="item.id"
              @click="selectTriageTask(item.id)"
            >
              <div class="tp-row-top">
                <span :class="['tp-dot', `tp-dot--${item.statusTone}`]" />
                <span class="tp-row-code tp-mono">{{ item.code }}</span>
                <span class="tp-row-ws tp-mono">{{ item.workspaceName }}</span>
                <span :class="['tp-row-state', `tp-row-state--${item.statusTone}`]">
                  {{ item.statusLabel }}
                </span>
                <span class="tp-row-elapsed tp-num">{{ item.elapsedLabel }}</span>
              </div>
              <div class="tp-row-title">
                {{ item.title }}
              </div>
            </button>
          </section>

          <div
            v-if="boardTasksPagination && boardOlderTasksRemaining > 0"
            class="tp-load-more-wrap"
          >
            <button
              type="button"
              class="tp-ghost-btn tp-load-more-btn"
              :disabled="boardLoadMoreLoading"
              @click="handleLoadOlderTasks"
            >
              {{
                boardLoadMoreLoading
                  ? 'Loading older…'
                  : `Load older done (${boardOlderTasksRemaining} more)`
              }}
            </button>
            <span
              v-if="boardLoadMoreError"
              class="tp-error-inline"
              role="alert"
            >
              {{ boardLoadMoreError }}
            </span>
          </div>
        </div>

        <footer class="tp-rail-footer">
          <span class="tp-kbd-group">
            <kbd>J</kbd><kbd>K</kbd>
            <span>navigate</span>
          </span>
          <span class="tp-kbd-group">
            <kbd>S</kbd>
            <span>steer</span>
          </span>
          <span class="tp-kbd-group">
            <kbd>⌘↵</kbd>
            <span>accept</span>
          </span>
        </footer>
      </aside>

      <!-- ==================== RIGHT EDITORIAL CANVAS ==================== -->
      <main
        class="tp-detail"
        aria-label="Selected task detail"
      >
        <div
          v-if="!activeItem"
          class="tp-detail-empty"
        >
          <p>Select a task on the left or create a new task to inspect its execution spine.</p>
        </div>

        <div
          v-else
          class="tp-editorial"
        >
          <!-- Meta Breadcrumb -->
          <div class="tp-meta-line">
            <span :class="['tp-status-pill', `tp-status-pill--${activeItem.statusTone}`]">
              <i :class="['tp-dot', `tp-dot--${activeItem.statusTone}`]" />
              {{ activeItem.statusLabel }}
            </span>
            <span class="tp-mono tp-meta-code">{{ activeItem.code }}</span>
            <span class="tp-sep">·</span>
            <span class="tp-mono">{{ activeItem.workspaceName }}</span>
            <span class="tp-sep">·</span>
            <span class="tp-branch-chip tp-mono">
              <svg
                class="tp-icon"
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
              >
                <circle
                  cx="5"
                  cy="4"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <circle
                  cx="5"
                  cy="12"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <circle
                  cx="11"
                  cy="6.5"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <path
                  d="M5 5.75v4.5M11 8.25c0 2-2 2.25-6 2.25"
                  stroke="currentColor"
                  stroke-width="1.5"
                  stroke-linecap="round"
                />
              </svg>
              {{ activeItem.branchLabel }}
            </span>
            <template v-if="activeItem.changedFilesCount > 0">
              <span class="tp-sep">·</span>
              <span class="tp-mono tp-num">{{ activeItem.changedFilesCount }} files</span>
            </template>
            <span class="tp-meta-updated tp-num">
              <template v-if="activeItem.updatedAgoLabel">{{ activeItem.updatedAgoLabel }} · </template>
              {{ activeItem.elapsedLabel }}
            </span>
          </div>

          <h1 class="tp-title">
            {{ activeItem.title }}
          </h1>

          <p class="tp-summary">
            {{ activeItem.outcomeSummary }}
          </p>

          <!-- Architectural Deliverable & Consensus Spec Frame -->
          <section
            class="tp-spec-frame"
            aria-label="Deliverable and consensus specification"
          >
            <!-- Row 1: Actionable Gate -->
            <div class="tp-spec-gate">
              <div class="tp-spec-gate-left">
                <span :class="['tp-verdict-badge', `tp-verdict-badge--${activeItem.consensus.verdictTone}`, 'tp-mono']">
                  <svg
                    v-if="activeItem.consensus.verdictTone === 'pass'"
                    class="tp-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M3.5 8.25L6.5 11.25L12.5 4.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                  <span
                    v-else-if="activeItem.consensus.verdictTone === 'live'"
                    class="tp-pulse-dot"
                  />
                  {{ activeItem.consensus.verdictLabel }}
                </span>
                <span class="tp-decision-copy">
                  {{ activeItem.decisionLine || activeItem.liveStep || activeItem.consensus.roundsLabel }}
                </span>
              </div>

              <div
                v-if="canUseManagedTaskActions(activeItem.task)"
                class="tp-decision-btns"
              >
                <LoadingButton
                  v-if="activeItem.task.status === 'todo'"
                  type="button"
                  class="tp-primary-btn"
                  :loading="isPending(taskActionKey('start', activeItem.id))"
                  loading-label="Starting…"
                  @click="startTask(activeItem.task)"
                >
                  Start Task
                </LoadingButton>

                <button
                  v-if="selectedSession"
                  type="button"
                  class="tp-ghost-btn"
                  @click="toggleSteerComposer"
                >
                  {{ isSteerOpen ? 'Close steer' : 'Steer…' }}
                </button>

                <LoadingButton
                  v-if="canRequestReviewTask(activeItem.task)"
                  type="button"
                  class="tp-ghost-btn"
                  :loading="isPending(taskActionKey('request-review', activeItem.id))"
                  loading-label="Requesting…"
                  @click="requestReview(activeItem.task)"
                >
                  Request review
                </LoadingButton>

                <LoadingButton
                  v-if="canMarkDoneTask(activeItem.task)"
                  type="button"
                  class="tp-primary-btn"
                  :loading="isPending(taskActionKey('mark-done', activeItem.id))"
                  loading-label="Accepting…"
                  @click="acceptAndCleanupTask(activeItem.task)"
                >
                  Accept
                </LoadingButton>

                <LoadingButton
                  v-if="activeItem.bucket === 'done' && selectedSession?.ephemeral"
                  type="button"
                  class="tp-ghost-btn"
                  :loading="isPending(taskActionKey('cleanup-session', activeItem.id))"
                  loading-label="Releasing…"
                  @click="cleanupCompletedTaskSession(activeItem.task)"
                >
                  Release session
                </LoadingButton>

                <button
                  v-if="selectedSession"
                  type="button"
                  class="tp-ghost-btn"
                  :disabled="isPending(sessionActionKey('open', selectedSession.id))"
                  @click="openSession(selectedSession)"
                >
                  Open session ↗
                </button>
              </div>
            </div>

            <!-- Inline Steer / Follow-up Composer (expands cleanly inside the Spec Frame) -->
            <form
              v-if="isSteerOpen && selectedSession && canUseManagedTaskActions(activeItem.task)"
              class="tp-steer-form"
              @submit.prevent="submitSteer"
              @paste="handleSteerPaste"
            >
              <textarea
                ref="steerInputRef"
                v-model="detailMessage"
                rows="2"
                placeholder="Send steering instructions or revision feedback to the agent…"
              />
              <div
                v-if="detailAttachments.length > 0"
                class="tp-steer-attachments"
              >
                <span
                  v-for="att in detailAttachments"
                  :key="att.id"
                  class="tp-mono tp-att-chip"
                >
                  {{ att.filename }} ({{ formatAttachmentSize(att.size_bytes) }})
                  <button
                    type="button"
                    aria-label="Remove attachment"
                    @click="removeDraftAttachment(detailAttachments, att)"
                  >×</button>
                </span>
              </div>
              <div class="tp-steer-actions">
                <span class="tp-steer-hint tp-mono">
                  {{ activeItem.task.status === 'review' || activeItem.task.status === 'failed' ? 'Continues task from review/failed' : 'Sends live follow-up to active session' }}
                </span>
                <LoadingButton
                  type="submit"
                  class="tp-primary-btn"
                  :disabled="!detailMessage.trim() && detailAttachments.length === 0"
                  :loading="isPending(selectedTaskSendKey)"
                  loading-label="Sending…"
                >
                  Send instructions
                </LoadingButton>
              </div>
            </form>

            <!-- Row 2: Heterogeneous Consensus Matrix -->
            <div class="tp-spec-consensus">
              <div class="tp-consensus-cell">
                <span class="tp-cell-label">CONVERGENCE PAIR</span>
                <div class="tp-cell-val">
                  <span>{{ activeItem.consensus.implementer }}</span>
                  <span class="tp-arrow">→</span>
                  <span>{{ activeItem.consensus.reviewer }}</span>
                </div>
              </div>
              <div class="tp-consensus-cell">
                <span class="tp-cell-label">REVIEW TRAJECTORY</span>
                <div class="tp-cell-val">
                  {{ activeItem.consensus.roundsLabel }}
                </div>
              </div>
              <div class="tp-consensus-cell">
                <span class="tp-cell-label">EVIDENCE &amp; CHECKS</span>
                <div class="tp-cell-val tp-mono tp-num">
                  {{ activeItem.consensus.metricHighlight }}
                </div>
              </div>
            </div>

            <!-- Row 3: Deliverable Artifacts & Changed Markdown Docs -->
            <div
              v-if="activeItem.links.length > 0 || selectedMarkdownDocuments.length > 0 || imageAttachments.length > 0"
              class="tp-spec-artifacts"
            >
              <div class="tp-artifacts-list">
                <a
                  v-for="link in activeItem.links"
                  :key="link.url"
                  :href="link.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="tp-artifact-row"
                >
                  <svg
                    v-if="link.kind === 'mr'"
                    class="tp-icon tp-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <circle
                      cx="4.5"
                      cy="4"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <circle
                      cx="4.5"
                      cy="12"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <circle
                      cx="11.5"
                      cy="12"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <path
                      d="M4.5 5.75v4.5M11.5 10.25V6.5a2.5 2.5 0 0 0-2.5-2.5H7.5"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                    />
                  </svg>
                  <svg
                    v-else
                    class="tp-icon tp-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <rect
                      x="3"
                      y="2.25"
                      width="10"
                      height="11.5"
                      rx="1.5"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <path
                      d="M5.5 5.75h5M5.5 8.25h5M5.5 10.75h3"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                    />
                  </svg>
                  <span class="tp-artifact-label">{{ link.label }}</span>
                  <span class="tp-artifact-meta tp-mono">{{ link.meta }}</span>
                  <span class="tp-artifact-ext">↗</span>
                </a>

                <button
                  v-for="doc in selectedMarkdownDocuments.slice(0, 4)"
                  :key="doc.id"
                  type="button"
                  class="tp-doc-btn tp-mono"
                  @click="openMarkdownPreviewModal(doc.path)"
                >
                  {{ doc.label }} ↗
                </button>

                <button
                  v-for="img in imageAttachments"
                  :key="img.id"
                  type="button"
                  class="tp-doc-btn tp-mono"
                  @click="openImageLightbox(`/api/workspaces/attachments/${img.id}`, img.filename)"
                >
                  {{ img.filename }} ↗
                </button>
              </div>
              <span class="tp-checks-note tp-mono">✓ {{ activeItem.checksLabel }}</span>
            </div>
          </section>

          <!-- Sculptural Epoch Run Spine -->
          <section class="tp-spine-section">
            <div class="tp-spine-header">
              <h2 class="tp-micro-heading">
                EXECUTION &amp; REVIEW SPINE
              </h2>
              <span class="tp-spine-overview tp-mono tp-num">
                {{ activeItem.spine.length }} stages · epoch {{ activeItem.internalMeta.epoch }} · {{ activeItem.elapsedLabel }}
              </span>
            </div>

            <ol class="tp-spine">
              <template
                v-for="node in activeItem.spine"
                :key="node.id"
              >
                <li
                  v-if="node.epochLabel"
                  class="tp-epoch-row"
                >
                  <span class="tp-epoch-label tp-mono">{{ node.epochLabel }}</span>
                  <span class="tp-epoch-line" />
                </li>

                <li :class="['tp-spine-item', `tp-spine-item--${node.tone}`, `tp-spine-role--${node.actorRole}`]">
                  <div class="tp-spine-time-col tp-num">
                    <span class="tp-spine-time">{{ node.timeLabel }}</span>
                    <span
                      v-if="node.deltaLabel"
                      class="tp-spine-delta tp-mono"
                    >{{ node.deltaLabel }}</span>
                  </div>

                  <span class="tp-spine-track">
                    <i class="tp-spine-node" />
                  </span>

                  <div class="tp-spine-body">
                    <div class="tp-spine-head">
                      <strong>{{ node.stage }}</strong>
                      <span class="tp-spine-actor">
                        {{ node.actor }}
                        <em class="tp-role-tag">{{ node.actorRole }}</em>
                      </span>
                      <span
                        v-if="node.badge"
                        :class="['tp-node-badge', 'tp-mono', `tp-node-badge--${node.tone}`]"
                      >
                        {{ node.badge }}
                      </span>
                      <button
                        v-if="node.sessionId || node.tabId"
                        type="button"
                        class="tp-inline-link tp-mono"
                        @click="toggleTranscript(node)"
                      >
                        {{ openTranscriptNodeId === node.id ? 'Hide transcript' : 'Transcript ↗' }}
                      </button>
                    </div>

                    <p>{{ node.summary }}</p>

                    <div
                      v-if="node.filesTouched || node.receipt"
                      class="tp-spine-evidence tp-mono"
                    >
                      <span
                        v-if="node.filesTouched"
                        class="tp-evidence-files"
                      >{{ node.filesTouched }}</span>
                      <span
                        v-if="node.receipt"
                        class="tp-evidence-receipt"
                      >✓ {{ node.receipt }}</span>
                    </div>

                    <!-- Real Read-only Session Stream Excerpt (fetched on demand, works even when archived!) -->
                    <div
                      v-if="openTranscriptNodeId === node.id"
                      class="tp-log-excerpt"
                    >
                      <div
                        v-if="transcriptLoading"
                        class="tp-log-status tp-mono"
                      >
                        Loading session stream…
                      </div>
                      <div
                        v-else-if="transcriptError"
                        class="tp-log-status tp-mono"
                      >
                        {{ transcriptError }}
                      </div>
                      <div
                        v-else-if="transcriptLines.length === 0"
                        class="tp-log-status tp-mono"
                      >
                        No persisted structured stream turns found for this session.
                      </div>
                      <template v-else>
                        <div
                          v-for="(line, idx) in transcriptLines"
                          :key="idx"
                          class="tp-log-row"
                        >
                          <span class="tp-mono tp-num">{{ line.time }}</span>
                          <span :class="['tp-mono', 'tp-log-speaker', `tp-log-speaker--${line.role}`]">
                            {{ line.speaker }}
                          </span>
                          <span>{{ line.text }}</span>
                        </div>
                      </template>

                      <div
                        v-if="node.tabId"
                        class="tp-log-footer"
                      >
                        <button
                          type="button"
                          class="tp-inline-link tp-mono"
                          @click="openOrRestoreTab(node.tabId)"
                        >
                          Open full conversation in Chat pane ↗
                        </button>
                      </div>
                    </div>
                  </div>
                </li>
              </template>
            </ol>
          </section>

          <!-- Collapsible Raw Prompt, Execution Control & Secondary Actions (Zero Default Clutter) -->
          <details class="tp-meta-disclosure">
            <summary>
              <span>Prompt, Execution Control &amp; Secondary Actions</span>
              <span class="tp-mono">
                control={{ activeItem.internalMeta.control }} · mode={{ activeItem.internalMeta.mode }} · seat={{ activeItem.internalMeta.seat }}
              </span>
            </summary>

            <div class="tp-disclosure-body">
              <div
                v-if="canUseManagedTaskActions(activeItem.task)"
                class="tp-secondary-actions"
              >
                <button
                  v-if="canEditTask(activeItem.task)"
                  type="button"
                  class="tp-ghost-btn"
                  @click="openEditTaskModal(activeItem.task)"
                >
                  Edit Task
                </button>
                <LoadingButton
                  v-if="canAbortTask(activeItem.task)"
                  type="button"
                  class="tp-ghost-btn tp-danger-text"
                  :loading="isPending(taskActionKey('abort', activeItem.id))"
                  loading-label="Aborting…"
                  @click="abortTask(activeItem.task)"
                >
                  Abort Task
                </LoadingButton>
                <LoadingButton
                  type="button"
                  class="tp-ghost-btn tp-danger-text"
                  :loading="isPending(taskActionKey('delete', activeItem.id))"
                  loading-label="Deleting…"
                  @click="deleteTask(activeItem.task)"
                >
                  Delete Task
                </LoadingButton>
              </div>

              <div class="tp-raw-prompt-block">
                <div class="tp-micro-heading">
                  TASK DESCRIPTION / BRIEF
                </div>
                <MarkdownContent
                  class="tp-prompt-md"
                  link-markdown-paths
                  :text="activeItem.task.prompt || '(empty description)'"
                  @markdown-path-click="path => openMarkdownPreviewModal(path)"
                />
              </div>

              <TaskExecutionPanel
                v-if="activeWorkspaceId"
                :key="`${activeWorkspaceId}:${activeItem.id}`"
                :workspace-id="activeWorkspaceId"
                :task="activeItem.task"
                :capabilities="taskCapabilities"
                :debug-open="false"
                :show-progress="false"
              />
            </div>
          </details>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import LoadingButton from '@/components/LoadingButton.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import TaskExecutionPanel from '@/components/TaskExecutionPanel.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type { AgentStreamEvent, ManagedSession } from '@/types'
import {
  formatAttachmentSize,
  handleAttachmentPaste,
  removeDraftAttachment,
  resetDraftAttachments,
  type DraftAttachment,
} from '@/utils/draftAttachments'
import { parseTimestampMs } from '@/utils/duration'
import {
  useWorkspaceAgentManagerContext,
  useWorkspaceArtifactsContext,
  useWorkspaceEditorContext,
  useWorkspaceLessonsContext,
  useWorkspaceRuntimeContext,
  useWorkspaceTaskActionsContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
import {
  buildTriageTaskViewModel,
  groupTriageTasks,
  type TriageSpineNode,
  type TriageTaskViewModel,
} from '../presentation/triageProModel'

const workspaceStore = useWorkspaceStore()
const terminalStore = useTerminalStore()
const appStore = useAppStore()

const {
  activeWorkspace,
  activeWorkspaceId,
  board,
  boardLoading,
  tasks,
  selectedTaskId,
  isPending,
  taskActionKey,
  sessionActionKey,
  openSession,
  openTaskModal,
  boardTasksPagination,
  boardOlderTasksRemaining,
  boardLoadMoreLoading,
  handleLoadOlderTasks,
  boardLoadMoreError,
  taskCapabilities,
  taskCreateFlow,
} = useWorkspaceRuntimeContext()

const {
  showWorkspaceModal,
  showResidentAgentModal,
} = useWorkspaceEditorContext()

const {
  showAgentOptionsModal,
  showAgentFileBrowser,
  showSwitchEnvModal,
  showSwitchEnvManager,
} = useWorkspaceAgentManagerContext()

const {
  showLessonsModal,
} = useWorkspaceLessonsContext()

const {
  canUseManagedTaskActions,
  canEditTask,
  canMarkDoneTask,
  canRequestReviewTask,
  canAbortTask,
  selectedSession,
  selectedReports,
  selectedMarkdownDocuments,
  selectedTaskSendKey,
} = useWorkspaceTaskPresentationContext()

const {
  startTask,
  openEditTaskModal,
  acceptAndCleanupTask,
  cleanupCompletedTaskSession,
  requestReview,
  abortTask,
  deleteTask,
  sendDetailMessage,
  detailAttachments,
  detailMessage,
  showEditTaskModal,
} = useWorkspaceTaskActionsContext()

const {
  openMarkdownPreviewModal,
  openImageLightbox,
  markdownPreviewModalPath,
  imageLightboxUrl,
} = useWorkspaceArtifactsContext()

const nowMs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | null = null

const isSteerOpen = ref(false)
const steerInputRef = ref<HTMLTextAreaElement | null>(null)

interface TranscriptExcerptRow {
  time: string
  speaker: string
  role: 'worker' | 'reviewer' | 'gate' | 'system'
  text: string
}

const openTranscriptNodeId = ref<string | null>(null)
const transcriptLoading = ref(false)
const transcriptError = ref<string | null>(null)
const transcriptLines = ref<TranscriptExcerptRow[]>([])
let transcriptEpoch = 0

const sessionsById = computed(() => {
  const map = new Map<string, ManagedSession>()
  for (const session of board.value?.sessions ?? []) {
    map.set(session.id, session)
  }
  return map
})

const triageItems = computed<TriageTaskViewModel[]>(() => {
  const wsName = activeWorkspace.value?.name || 'workspace'
  const defBranch = activeWorkspace.value?.default_branch || 'main'
  const lifecycles = board.value?.task_lifecycles ?? {}

  return tasks.value.map(task => {
    const isSelected = selectedTaskId.value === task.id
    const fullReports = isSelected ? selectedReports.value : []
    const reports = fullReports.length > 0 ? fullReports : workspaceStore.reportsForTask(task)
    return buildTriageTaskViewModel({
      task,
      workspaceName: wsName,
      defaultBranch: defBranch,
      reports,
      sessionsById: sessionsById.value,
      lifecycle: lifecycles[task.id],
      nowMs: nowMs.value,
    })
  })
})

const groupedTasks = computed(() => groupTriageTasks(triageItems.value))

const orderedVisibleItems = computed(() => groupedTasks.value.flatMap(g => g.items))

// Auto-select first prioritized task when entering or switching workspaces without opening a modal dialog
watch(
  [orderedVisibleItems, activeWorkspaceId],
  ([items]) => {
    if (items.length === 0) return
    if (!selectedTaskId.value || !items.some(i => i.id === selectedTaskId.value)) {
      selectedTaskId.value = items[0].id
    }
  },
  { immediate: true },
)

const activeItem = computed<TriageTaskViewModel | null>(() => {
  if (!orderedVisibleItems.value.length) return null
  return (
    orderedVisibleItems.value.find(item => item.id === selectedTaskId.value) ??
    orderedVisibleItems.value[0]
  )
})

const imageAttachments = computed(() =>
  (activeItem.value?.task.attachments || []).filter(att =>
    (att.mime_type || '').startsWith('image/'),
  ),
)

let steerSelectionEpoch = 0

function resetTaskTransientState() {
  steerSelectionEpoch += 1
  detailMessage.value = ''
  resetDraftAttachments(detailAttachments.value)
  detailAttachments.value = []
  isSteerOpen.value = false
  openTranscriptNodeId.value = null
  transcriptLines.value = []
  transcriptError.value = null
}

async function handleSteerPaste(event: ClipboardEvent) {
  const epoch = steerSelectionEpoch
  const staged: DraftAttachment[] = []
  await handleAttachmentPaste(event, staged)
  if (epoch !== steerSelectionEpoch) {
    resetDraftAttachments(staged)
    return
  }
  detailAttachments.value.push(...staged)
}

watch(selectedTaskId, (nextId, prevId) => {
  if (prevId !== undefined && nextId !== prevId) {
    resetTaskTransientState()
  }
})

function selectTriageTask(id: string) {
  if (selectedTaskId.value === id) return
  selectedTaskId.value = id
}

function toggleSteerComposer() {
  isSteerOpen.value = !isSteerOpen.value
  if (isSteerOpen.value) {
    void nextTick(() => steerInputRef.value?.focus())
  }
}

async function submitSteer() {
  await sendDetailMessage()
  if (!detailMessage.value.trim() && detailAttachments.value.length === 0) {
    isSteerOpen.value = false
  }
}

function formatClock(iso: string | undefined): string {
  const ms = parseTimestampMs(iso)
  if (ms === null) return '--:--'
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function toggleTranscript(node: TriageSpineNode) {
  if (openTranscriptNodeId.value === node.id) {
    openTranscriptNodeId.value = null
    return
  }
  openTranscriptNodeId.value = node.id
  transcriptLoading.value = true
  transcriptError.value = null
  transcriptLines.value = []
  const reqEpoch = ++transcriptEpoch

  try {
    const endpoint = node.sessionId
      ? `/api/workspaces/sessions/${encodeURIComponent(node.sessionId)}/stream/events?since_sequence=-1&limit=120&compact=true`
      : node.tabId
        ? `/api/workspaces/tabs/${encodeURIComponent(node.tabId)}/stream/events?since_sequence=-1&limit=120&compact=true`
        : null
    if (!endpoint) {
      transcriptError.value = 'No session reference recorded on this stage.'
      return
    }
    const response = await fetch(endpoint, { credentials: 'same-origin' })
    if (reqEpoch !== transcriptEpoch) return
    if (!response.ok) {
      transcriptError.value = node.tabId
        ? 'Live stream closed; click below to restore the archived conversation.'
        : `Stream unavailable (HTTP ${response.status}).`
      return
    }
    const data = (await response.json()) as { events?: AgentStreamEvent[] }
    if (reqEpoch !== transcriptEpoch) return
    const rows: TranscriptExcerptRow[] = []
    for (const ev of data.events ?? []) {
      if (ev.type === 'turn_started') {
        const summary = String(ev.payload?.summary || '').replace(/\s+/g, ' ').trim()
        if (summary) {
          rows.push({
            time: formatClock(ev.created_at),
            speaker: 'Prompt',
            role: 'gate',
            text: summary.slice(0, 220),
          })
        }
      } else if (ev.type === 'turn_completed') {
        const text = String(ev.payload?.final_text || ev.payload?.assistant_text || ev.payload?.summary || '')
          .replace(/\s+/g, ' ')
          .trim()
        if (text) {
          rows.push({
            time: formatClock(ev.created_at),
            speaker: node.actorRole === 'reviewer' ? 'Reviewer' : 'Agent',
            role: node.actorRole === 'reviewer' ? 'reviewer' : 'worker',
            text: text.slice(0, 260),
          })
        }
      }
    }
    transcriptLines.value = rows.slice(-6)
  } catch {
    if (reqEpoch === transcriptEpoch) {
      transcriptError.value = 'Could not load stream preview.'
    }
  } finally {
    if (reqEpoch === transcriptEpoch) {
      transcriptLoading.value = false
    }
  }
}

async function openOrRestoreTab(tabId: string) {
  await Promise.all([
    terminalStore.fetchTabs(),
    terminalStore.fetchArchivedTabs(),
  ])
  if (terminalStore.archivedTabs.some(t => t.id === tabId)) {
    const restored = await terminalStore.unarchiveTab(tabId)
    if (!restored) return
  }
  appStore.setMode('terminal')
  terminalStore.setActiveTab(tabId)
}

const isAnyOverlayOpen = computed(() =>
  Boolean(
    taskCreateFlow.visible.value ||
      showWorkspaceModal.value ||
      showResidentAgentModal.value ||
      showEditTaskModal.value ||
      showLessonsModal.value ||
      showAgentOptionsModal.value ||
      showAgentFileBrowser.value ||
      showSwitchEnvModal.value ||
      showSwitchEnvManager.value ||
      markdownPreviewModalPath.value ||
      imageLightboxUrl.value,
  ),
)

function handleKeydown(event: KeyboardEvent) {
  if (isAnyOverlayOpen.value) return
  if (
    typeof document !== 'undefined' &&
    document.querySelector(
      '[role="dialog"], dialog[open], .workspace-picker__panel, .workspace-mobile-menu[open]',
    )
  ) {
    return
  }
  const target = event.target as HTMLElement | null
  if (
    target &&
    (target.tagName === 'INPUT' ||
      target.tagName === 'TEXTAREA' ||
      target.tagName === 'SELECT' ||
      target.isContentEditable ||
      Boolean(target.closest('[role="dialog"], [role="menu"]')))
  ) {
    return
  }
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    const current = activeItem.value
    if (current && canUseManagedTaskActions(current.task) && canMarkDoneTask(current.task)) {
      event.preventDefault()
      void acceptAndCleanupTask(current.task)
    }
    return
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return
  const list = orderedVisibleItems.value
  if (!list.length) return

  if (event.key === 'j' || event.key === 'ArrowDown') {
    event.preventDefault()
    const idx = list.findIndex(item => item.id === activeItem.value?.id)
    selectTriageTask(list[(idx + 1) % list.length].id)
  } else if (event.key === 'k' || event.key === 'ArrowUp') {
    event.preventDefault()
    const idx = list.findIndex(item => item.id === activeItem.value?.id)
    selectTriageTask(list[(idx - 1 + list.length) % list.length].id)
  } else if (event.key === 's' && selectedSession.value) {
    event.preventDefault()
    toggleSteerComposer()
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  clockTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 15_000)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
  if (clockTimer !== null) clearInterval(clockTimer)
  transcriptEpoch += 1
  resetTaskTransientState()
})
</script>

<style scoped>
/* ============================================================================
   LINEAR TRIAGE PRO — High-Contrast Architectural Tokens (Dark & Light)
   ============================================================================ */
.tp-shell {
  --tp-bg-canvas: #111113;
  --tp-bg-panel: #16171a;
  --tp-bg-elevated: #1c1d22;
  --tp-bg-hover: #1f2127;
  --tp-bg-selected: #22242b;
  --tp-line: #2a2d35;
  --tp-line-subtle: #1f2229;
  --tp-line-strong: #3a3f4b;
  --tp-fg-1: #edeef0;
  --tp-fg-2: #b8bdc7;
  --tp-fg-3: #949aa4;
  --tp-accent: #3d4fd6;
  --tp-accent-text: #a5b4ff;
  --tp-amber: #f0b45a;
  --tp-amber-bg: #261e12;
  --tp-green: #5cd696;
  --tp-green-bg: #12261c;
  --tp-live-bg: #171c30;
  --tp-red: #ff8a80;
  --tp-red-bg: #291517;

  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--tp-bg-canvas);
  color: var(--tp-fg-1);
  font-family: var(--ch-font-sans);
  font-size: 13px;
  line-height: 1.5;
  overflow: hidden;
}

:root[data-theme='light'] .tp-shell {
  --tp-bg-canvas: #f8f9fa;
  --tp-bg-panel: #f1f3f5;
  --tp-bg-elevated: #ffffff;
  --tp-bg-hover: #e9ecef;
  --tp-bg-selected: #e5e8ef;
  --tp-line: #d5d9e2;
  --tp-line-subtle: #e4e7ee;
  --tp-line-strong: #b8bfcc;
  --tp-fg-1: #141619;
  --tp-fg-2: #353942;
  --tp-fg-3: #505664;
  --tp-accent: #3645c4;
  --tp-accent-text: #313fb2;
  --tp-amber: #944f04;
  --tp-amber-bg: #fef3e2;
  --tp-green: #126940;
  --tp-green-bg: #e6f6ee;
  --tp-live-bg: #e8ecfc;
  --tp-red: #b42824;
  --tp-red-bg: #fdeaea;
}

.tp-num { font-variant-numeric: tabular-nums; }
.tp-mono { font-family: var(--ch-font-mono); font-size: 12px; }
.tp-icon { width: 14px; height: 14px; flex: 0 0 14px; }
.tp-sep { color: var(--tp-fg-3); }

.tp-ghost-btn {
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--tp-line-strong);
  border-radius: 6px;
  background: var(--tp-bg-elevated);
  color: var(--tp-fg-1);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.tp-ghost-btn:hover:not(:disabled) { background: var(--tp-bg-hover); }
.tp-ghost-btn:disabled { opacity: 0.55; cursor: default; }
.tp-danger-text { color: var(--tp-red); }

.tp-primary-btn {
  height: 28px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: var(--tp-accent);
  color: #ffffff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.tp-primary-btn:disabled { opacity: 0.55; cursor: default; }

.tp-dot, .tp-pulse-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: var(--tp-fg-3);
}
.tp-dot--attention { background: var(--tp-amber); }
.tp-dot--working, .tp-pulse-dot { background: var(--tp-accent-text); }
.tp-dot--done { background: var(--tp-green); }
.tp-dot--failed { background: var(--tp-red); }

.tp-empty-workspace,
.tp-detail-empty {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 32px;
  color: var(--tp-fg-3);
}
.tp-empty-card {
  max-width: 420px;
  text-align: center;
}
.tp-empty-card h2 {
  margin: 0 0 6px;
  font-size: 16px;
  color: var(--tp-fg-1);
}
.tp-empty-card p {
  margin: 0;
  font-size: 13px;
}

/* Split Layout */
.tp-linear {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  overflow: hidden;
}

.tp-rail {
  border-right: 1px solid var(--tp-line);
  background: var(--tp-bg-panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.tp-rail-loading,
.tp-rail-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 24px;
  text-align: center;
  color: var(--tp-fg-3);
}
.tp-rail-empty p { margin: 0; }

.tp-groups {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
}

.tp-group + .tp-group { margin-top: 14px; }

.tp-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 5px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--tp-fg-3);
}

.tp-group-empty {
  padding: 6px 16px;
  font-size: 12px;
  color: var(--tp-fg-3);
}

.tp-row {
  appearance: none;
  width: 100%;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--tp-line-subtle);
  text-align: left;
  font: inherit;
  color: inherit;
  position: relative;
  padding: 8px 16px 9px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  cursor: pointer;
}
.tp-row:focus-visible {
  outline: 1.5px solid var(--tp-accent-text);
  outline-offset: -2px;
}
.tp-row:hover { background: var(--tp-bg-hover); }
.tp-row.active { background: var(--tp-bg-selected); }
.tp-row.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--tp-accent-text);
}

.tp-row-top {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.tp-row-code { font-size: 11.5px; font-weight: 600; color: var(--tp-fg-1); }
.tp-row-ws {
  font-size: 11.5px;
  color: var(--tp-fg-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-row-state {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  color: var(--tp-fg-2);
  white-space: nowrap;
}
.tp-row-state--attention { color: var(--tp-amber); }
.tp-row-state--working { color: var(--tp-accent-text); }
.tp-row-state--done { color: var(--tp-green); }
.tp-row-state--failed { color: var(--tp-red); }
.tp-row-elapsed { font-size: 11.5px; color: var(--tp-fg-3); flex: 0 0 auto; }

.tp-row-title {
  font-size: 13px;
  color: var(--tp-fg-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-row.active .tp-row-title { color: var(--tp-fg-1); font-weight: 500; }

.tp-load-more-wrap {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tp-load-more-btn { width: 100%; }
.tp-error-inline { font-size: 11.5px; color: var(--tp-red); }

.tp-rail-footer {
  flex: 0 0 auto;
  padding: 8px 16px;
  border-top: 1px solid var(--tp-line);
  background: var(--tp-bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--tp-fg-3);
}
.tp-kbd-group { display: inline-flex; align-items: center; gap: 3px; }
.tp-kbd-group span { margin-left: 2px; }
.tp-kbd-group kbd {
  padding: 1px 4px;
  border: 1px solid var(--tp-line-strong);
  border-radius: 3px;
  background: var(--tp-bg-panel);
  color: var(--tp-fg-2);
  font-family: var(--ch-font-mono);
  font-size: 10.5px;
  line-height: 1.2;
}

/* Right Detail Canvas */
.tp-detail {
  overflow-y: auto;
  padding: 20px 36px 40px;
}
.tp-editorial {
  max-width: 740px;
  margin: 0 auto;
}

.tp-meta-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--tp-fg-3);
  margin-bottom: 8px;
}
.tp-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid var(--tp-line-strong);
  background: var(--tp-bg-panel);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--tp-fg-1);
}
.tp-status-pill--attention { color: var(--tp-amber); background: var(--tp-amber-bg); }
.tp-status-pill--working { color: var(--tp-accent-text); background: var(--tp-live-bg); }
.tp-status-pill--done { color: var(--tp-green); background: var(--tp-green-bg); }
.tp-status-pill--failed { color: var(--tp-red); background: var(--tp-red-bg); }

.tp-meta-code { font-weight: 600; color: var(--tp-fg-1); }
.tp-branch-chip { display: inline-flex; align-items: center; gap: 4px; color: var(--tp-fg-2); }
.tp-meta-updated { margin-left: auto; color: var(--tp-fg-3); }

.tp-title {
  margin: 0 0 6px;
  font-size: 19px;
  font-weight: 600;
  line-height: 1.32;
  letter-spacing: -0.018em;
  color: var(--tp-fg-1);
}
.tp-summary {
  margin: 0 0 14px;
  font-size: 13.5px;
  line-height: 1.58;
  color: var(--tp-fg-2);
}

/* Spec Frame */
.tp-spec-frame {
  border: 1px solid var(--tp-line-strong);
  border-radius: 8px;
  background: var(--tp-bg-panel);
  overflow: hidden;
}
.tp-spec-gate {
  padding: 10px 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--tp-bg-elevated);
}
.tp-spec-gate-left {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.tp-verdict-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
  border: 1px solid var(--tp-line-strong);
  color: var(--tp-fg-1);
  background: var(--tp-bg-panel);
}
.tp-verdict-badge--pass { color: var(--tp-green); background: var(--tp-green-bg); }
.tp-verdict-badge--live { color: var(--tp-accent-text); background: var(--tp-live-bg); }
.tp-verdict-badge--warn { color: var(--tp-amber); background: var(--tp-amber-bg); }
.tp-verdict-badge--auto { color: var(--tp-fg-2); }

.tp-decision-copy { font-size: 12.5px; color: var(--tp-fg-1); line-height: 1.4; }
.tp-decision-btns { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }

.tp-steer-form {
  border-top: 1px solid var(--tp-line);
  padding: 10px 14px;
  background: var(--tp-bg-canvas);
  display: grid;
  gap: 8px;
}
.tp-steer-form textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--tp-line-strong);
  border-radius: 6px;
  background: var(--tp-bg-panel);
  color: var(--tp-fg-1);
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.45;
  resize: vertical;
}
.tp-steer-attachments { display: flex; flex-wrap: wrap; gap: 6px; }
.tp-att-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid var(--tp-line-strong);
  background: var(--tp-bg-panel);
  font-size: 11px;
}
.tp-att-chip button {
  border: 0;
  background: transparent;
  color: var(--tp-fg-3);
  cursor: pointer;
}
.tp-steer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.tp-steer-hint { font-size: 11px; color: var(--tp-fg-3); }

.tp-spec-consensus {
  border-top: 1px solid var(--tp-line);
  display: grid;
  grid-template-columns: 0.95fr 1.05fr 1.2fr;
}
.tp-consensus-cell {
  padding: 7px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.tp-consensus-cell + .tp-consensus-cell { border-left: 1px solid var(--tp-line); }
.tp-cell-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--tp-fg-3);
}
.tp-cell-val {
  font-size: 12px;
  font-weight: 500;
  color: var(--tp-fg-1);
  display: flex;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-arrow { color: var(--tp-fg-3); }

.tp-spec-artifacts {
  border-top: 1px solid var(--tp-line);
  padding: 7px 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.tp-artifacts-list { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.tp-artifact-row {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  text-decoration: none;
  color: var(--tp-fg-1);
  font-size: 12px;
}
.tp-artifact-row:hover .tp-artifact-label {
  color: var(--tp-accent-text);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.tp-artifact-icon, .tp-artifact-label { color: var(--tp-accent-text); font-weight: 500; }
.tp-artifact-meta, .tp-artifact-ext { color: var(--tp-fg-3); font-size: 11px; }
.tp-doc-btn {
  padding: 2px 7px;
  border: 1px solid var(--tp-line-strong);
  border-radius: 4px;
  background: var(--tp-bg-elevated);
  color: var(--tp-accent-text);
  font-size: 11px;
  cursor: pointer;
}
.tp-checks-note { font-size: 11.5px; color: var(--tp-green); }

/* Epoch Run Spine */
.tp-spine-section { margin-top: 18px; }
.tp-spine-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.tp-micro-heading {
  margin: 0;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.07em;
  color: var(--tp-fg-3);
}
.tp-spine-overview { color: var(--tp-fg-3); font-size: 11.5px; }
.tp-spine { list-style: none; margin: 0; padding: 0; }

.tp-epoch-row { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
.tp-epoch-row:first-child { margin-top: 0; }
.tp-epoch-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--tp-fg-3);
  white-space: nowrap;
}
.tp-epoch-line { flex: 1; height: 1px; background: var(--tp-line); }

.tp-spine-item {
  display: grid;
  grid-template-columns: 44px 20px minmax(0, 1fr);
  gap: 0 8px;
}
.tp-spine-time-col { display: flex; flex-direction: column; align-items: flex-end; }
.tp-spine-time { font-size: 12px; color: var(--tp-fg-2); line-height: 1.25; }
.tp-spine-delta { font-size: 10.5px; color: var(--tp-fg-3); line-height: 1.15; }
.tp-spine-track { position: relative; display: flex; justify-content: center; }
.tp-spine-track::after {
  content: '';
  position: absolute;
  top: 13px;
  bottom: -4px;
  width: 1px;
  background: var(--tp-line-strong);
}
.tp-spine-item:last-child .tp-spine-track::after { display: none; }
.tp-spine-node {
  margin-top: 4px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--tp-bg-canvas);
  border: 1.5px solid var(--tp-fg-3);
  z-index: 1;
}
.tp-spine-role--worker .tp-spine-node { background: var(--tp-fg-2); border-color: var(--tp-fg-2); }
.tp-spine-item--warn .tp-spine-node {
  border-radius: 2px;
  transform: rotate(45deg);
  border-color: var(--tp-amber);
  background: var(--tp-amber);
}
.tp-spine-item--pass .tp-spine-node { border-color: var(--tp-green); background: var(--tp-green); }
.tp-spine-item--live .tp-spine-node { border-color: var(--tp-accent-text); background: var(--tp-accent-text); }

.tp-spine-body { padding-bottom: 12px; min-width: 0; }
.tp-spine-head { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.tp-spine-head strong { font-size: 13px; font-weight: 600; color: var(--tp-fg-1); }
.tp-spine-actor { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--tp-fg-2); }
.tp-role-tag { font-style: normal; font-size: 10.5px; color: var(--tp-fg-3); }
.tp-node-badge {
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--tp-line-strong);
  background: var(--tp-bg-panel);
  color: var(--tp-fg-2);
  font-size: 10.5px;
  font-weight: 600;
}
.tp-node-badge--warn { color: var(--tp-amber); background: var(--tp-amber-bg); }
.tp-node-badge--pass { color: var(--tp-green); background: var(--tp-green-bg); }
.tp-node-badge--live { color: var(--tp-accent-text); background: var(--tp-live-bg); }

.tp-inline-link {
  margin-left: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--tp-accent-text);
  font-size: 11.5px;
  cursor: pointer;
}
.tp-inline-link:hover { text-decoration: underline; }

.tp-spine-body p {
  margin: 2px 0 0;
  font-size: 12.5px;
  color: var(--tp-fg-2);
  line-height: 1.48;
  overflow-wrap: anywhere;
}
.tp-spine-evidence {
  margin-top: 3px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}
.tp-evidence-files { color: var(--tp-fg-2); }
.tp-evidence-receipt { color: var(--tp-fg-3); }

.tp-log-excerpt {
  margin-top: 6px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--tp-bg-panel);
  border: 1px solid var(--tp-line-strong);
  display: grid;
  gap: 5px;
}
.tp-log-status { font-size: 11.5px; color: var(--tp-fg-3); }
.tp-log-row {
  display: grid;
  grid-template-columns: 40px 84px minmax(0, 1fr);
  gap: 8px;
  font-size: 11.5px;
  color: var(--tp-fg-2);
}
.tp-log-row > span:first-child { color: var(--tp-fg-3); }
.tp-log-speaker { font-weight: 600; color: var(--tp-fg-1); }
.tp-log-speaker--gate { color: var(--tp-amber); }
.tp-log-speaker--reviewer { color: var(--tp-green); }
.tp-log-speaker--worker { color: var(--tp-accent-text); }
.tp-log-footer {
  padding-top: 4px;
  border-top: 1px solid var(--tp-line);
  display: flex;
  justify-content: flex-end;
}

/* Metadata & Secondary Disclosure */
.tp-meta-disclosure {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--tp-line);
  color: var(--tp-fg-3);
  font-size: 12px;
}
.tp-meta-disclosure summary {
  cursor: pointer;
  user-select: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tp-disclosure-body {
  margin-top: 12px;
  display: grid;
  gap: 14px;
}
.tp-secondary-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.tp-raw-prompt-block {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--tp-line);
  background: var(--tp-bg-panel);
  display: grid;
  gap: 6px;
}
.tp-prompt-md {
  color: var(--tp-fg-2);
  font-size: 12.5px;
}

@media (max-width: 860px) {
  .tp-shell { overflow-y: auto; }
  .tp-linear, .tp-spec-consensus {
    display: flex;
    flex-direction: column;
    overflow: visible;
  }
  .tp-rail {
    flex: 0 0 auto;
    border-right: 0;
    border-bottom: 1px solid var(--tp-line);
    max-height: 260px;
  }
  .tp-rail-footer { display: none; }
  .tp-detail { padding: 16px 14px 32px; overflow: visible; }
  .tp-consensus-cell + .tp-consensus-cell { border-left: 0; border-top: 1px solid var(--tp-line); }
  .tp-ghost-btn, .tp-primary-btn { min-height: 40px; }
}
</style>
