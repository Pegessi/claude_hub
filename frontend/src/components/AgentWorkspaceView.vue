<template>
  <section class="workspace-view">
    <header class="workspace-header">
      <div>
        <h1>Agent Workspace</h1>
        <p>Manual task queue with multiple resident workspace agents.</p>
      </div>
      <div class="workspace-actions">
        <select
          v-model="selectedWorkspaceId"
          class="workspace-select"
          @change="handleWorkspaceChange"
        >
          <option
            v-for="workspace in workspaces"
            :key="workspace.id"
            :value="workspace.id"
          >
            {{ workspace.name }}
          </option>
        </select>
        <button
          type="button"
          class="tool-button mobile-setup-button"
          @click="mobileSetupOpen = !mobileSetupOpen"
        >
          {{ mobileSetupOpen ? 'Board' : 'Setup' }}
        </button>
        <button
          type="button"
          class="tool-button"
          :disabled="!activeWorkspaceId"
          @click="dispatchWorkspace"
        >
          Dispatch
        </button>
        <button
          type="button"
          class="tool-button"
          :disabled="!activeWorkspaceId"
          @click="refreshBoard"
        >
          Refresh
        </button>
      </div>
    </header>

    <div
      v-if="error"
      class="workspace-error"
    >
      {{ error }}
    </div>

    <div
      v-if="activeWorkspaceId"
      class="mobile-status-strip"
    >
      <div class="mobile-agent-summary">
        <span>{{ workspaceAgents.length }} agents</span>
        <strong>{{ workspaceAgents.filter(agent => agent.runtime_status === 'working').length }} working</strong>
      </div>
      <div class="mobile-column-tabs">
        <span
          v-for="column in columns"
          :key="column.status"
        >
          {{ column.label }} {{ tasksByStatus(column.status).length }}
        </span>
      </div>
    </div>

    <div :class="['workspace-layout', { 'detail-open': selectedTask, 'mobile-setup-open': mobileSetupOpen }]">
      <aside :class="['setup-panel', { 'mobile-open': mobileSetupOpen }]">
        <form
          class="setup-section"
          @submit.prevent="handleCreateWorkspace"
        >
          <h2>Workspace</h2>
          <label>
            Name
            <input
              v-model="workspaceForm.name"
              placeholder="claude_hub"
            />
          </label>
          <label>
            Repository path
            <input
              v-model="workspaceForm.path"
              placeholder="/Users/me/project"
            />
          </label>
          <div class="form-row">
            <label>
              Base
              <input
                v-model="workspaceForm.default_branch"
                placeholder="main"
              />
            </label>
            <label>
              Prefix
              <input
                v-model="workspaceForm.session_prefix"
                placeholder="chub"
              />
            </label>
          </div>
          <button
            type="submit"
            class="primary-button"
            :disabled="isLoading"
          >
            Create workspace
          </button>
        </form>

        <form
          class="setup-section"
          @submit.prevent="handleCreateAgent"
        >
          <h2>Agents</h2>
          <label>
            Title
            <input
              v-model="agentForm.title"
              placeholder="Workspace Agent"
              :disabled="!activeWorkspaceId"
            />
          </label>
          <label>
            Type
            <select
              v-model="agentForm.agent_type"
              :disabled="!activeWorkspaceId"
            >
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
              <option value="cursor">Terminal</option>
            </select>
          </label>
          <button
            type="submit"
            class="primary-button"
            :disabled="!activeWorkspaceId || isLoading"
          >
            Add agent
          </button>

          <div class="agent-list">
            <article
              v-for="agent in workspaceAgents"
              :key="agent.id"
              class="agent-row"
            >
              <div>
                <strong>{{ agent.title }}</strong>
                <span>{{ agent.agent_type }} · {{ agent.id }}</span>
              </div>
              <div class="agent-row-meta">
                <span :class="['runtime-pill', `runtime-pill--${agent.runtime_status}`]">
                  {{ agent.runtime_status }}
                </span>
                <span>current {{ taskTitle(agent.current_task_id) }}</span>
                <span>queued {{ agent.queued_count }}</span>
              </div>
              <div class="agent-row-actions">
                <button
                  type="button"
                  @click="openSession(agent)"
                >
                  Open
                </button>
                <button
                  type="button"
                  class="danger-button"
                  :disabled="agent.queued_count > 0 || Boolean(agent.current_task_id)"
                  @click="deleteAgent(agent)"
                >
                  Delete
                </button>
              </div>
            </article>
            <div
              v-if="workspaceAgents.length === 0"
              class="empty-inline"
            >
              No workspace agents.
            </div>
            <article
              v-if="dispatcherAgent"
              class="agent-row dispatcher-row"
            >
              <div>
                <strong>{{ dispatcherAgent.title }}</strong>
                <span>dispatcher · {{ dispatcherAgent.runtime_status }}</span>
              </div>
              <button
                type="button"
                @click="openSession(dispatcherAgent)"
              >
                Open
              </button>
            </article>
          </div>
        </form>

        <form
          class="setup-section"
          @submit.prevent="handleCreateTask"
        >
          <h2>Task</h2>
          <label>
            Title
            <input
              v-model="taskForm.title"
              placeholder="Implement a focused change"
              :disabled="!activeWorkspaceId"
            />
          </label>
          <label>
            Task description
            <textarea
              v-model="taskForm.prompt"
              placeholder="Describe what the workspace agent should implement..."
              :disabled="!activeWorkspaceId"
            />
          </label>
          <label>
            Agent type
            <select
              v-model="taskForm.agent_type"
              :disabled="!activeWorkspaceId"
            >
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
              <option value="cursor">Terminal</option>
            </select>
          </label>
          <label>
            Related task
            <select
              v-model="taskForm.related_task_id"
              :disabled="!activeWorkspaceId"
            >
              <option value="">None</option>
              <option
                v-for="task in tasks"
                :key="task.id"
                :value="task.id"
              >
                {{ task.title }}
              </option>
            </select>
          </label>
          <button
            type="submit"
            class="primary-button"
            :disabled="!activeWorkspaceId || isLoading"
          >
            Add task
          </button>
        </form>
      </aside>

      <main class="board">
        <div
          v-if="!activeWorkspaceId"
          class="empty-board"
        >
          Create a workspace to start agents and queue tasks.
        </div>

        <template v-else>
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
                <span>{{ tasksByStatus(column.status).length }}</span>
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
                :class="['task-card', { selected: selectedTaskId === task.id }]"
                @click="selectTask($event, task.id)"
              >
                <div class="task-card-header">
                  <h3>{{ task.title }}</h3>
                  <span class="agent-badge">
                    <span :class="['status-dot', `status-dot--${task.status}`]" />
                    {{ task.agent_type }}
                  </span>
                </div>
                <p>{{ task.prompt }}</p>
                <div
                  v-if="task.dispatch_pending"
                  class="latest-report"
                >
                  Waiting for dispatcher decision
                </div>
                <div
                  v-else-if="task.dispatch_reason"
                  class="latest-report"
                >
                  {{ task.dispatch_reason }}
                </div>
                <div
                  v-if="latestReportForTask(task)"
                  class="latest-report"
                >
                  {{ latestReportForTask(task)?.state }} · {{ latestReportForTask(task)?.message }}
                </div>
                <div class="session-meta">
                  <span>task {{ task.status }}</span>
                  <span>agent {{ agentTitle(task.session_id) }}</span>
                  <span v-if="sessionForTask(task)">
                    runtime {{ sessionForTask(task)?.runtime_status }}
                  </span>
                </div>
                <details
                  v-if="task.status === 'todo'"
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
                    />
                    Clear context
                  </label>
                </details>
                <div class="task-actions">
                  <button
                    v-if="task.status === 'todo'"
                    type="button"
                    @click.stop="startTask(task)"
                  >
                    Start
                  </button>
                  <button
                    v-if="task.status === 'review'"
                    type="button"
                    @click.stop="continueTask(task)"
                  >
                    Continue
                  </button>
                  <button
                    v-if="task.status === 'review'"
                    type="button"
                    @click.stop="markTask(task.id, 'done')"
                  >
                    Done
                  </button>
                  <button
                    v-if="sessionForTask(task)"
                    type="button"
                    @click.stop="openSession(sessionForTask(task)!)"
                  >
                    Open tab
                  </button>
                  <button
                    v-if="sessionForTask(task)"
                    type="button"
                    @click.stop="openMessagePrompt(sessionForTask(task)!.id)"
                  >
                    Send
                  </button>
                  <button
                    type="button"
                    class="danger-button"
                    @click.stop="deleteTask(task)"
                  >
                    Delete
                  </button>
                </div>
              </article>
              <div
                v-if="tasksByStatus(column.status).length === 0"
                class="column-empty"
              >
                No tasks
              </div>
            </div>
          </section>
        </template>
      </main>

      <aside
        v-if="selectedTask"
        class="task-detail-panel"
      >
        <div class="detail-header">
          <div>
            <span class="detail-eyebrow">{{ selectedTask.status }}</span>
            <h2>{{ selectedTask.title }}</h2>
          </div>
          <button
            type="button"
            class="icon-button"
            @click="closeTaskDetail"
          >
            x
          </button>
        </div>

        <div class="detail-body">
          <section class="detail-section">
            <div class="detail-section-title">Task description</div>
            <p class="detail-copy">{{ selectedTask.prompt }}</p>
          </section>

          <section class="detail-section">
            <div class="detail-section-title">Assignment</div>
            <div class="fact-grid">
              <div>
                <span>Task stage</span>
                <strong>{{ selectedTask.status }}</strong>
              </div>
              <div>
                <span>Agent runtime</span>
                <strong>{{ selectedSession?.runtime_status || 'none' }}</strong>
              </div>
              <div>
                <span>Agent</span>
                <strong>{{ selectedSession?.title || 'auto' }}</strong>
              </div>
              <div>
                <span>Queued behind</span>
                <strong>{{ selectedSession?.queued_count || 0 }}</strong>
              </div>
              <div>
                <span>Clear context</span>
                <strong>{{ selectedTask.clear_context ? 'yes' : 'no' }}</strong>
              </div>
              <div>
                <span>Snapshot</span>
                <strong>{{ board?.snapshot_path || 'none' }}</strong>
              </div>
            </div>
          </section>

          <section class="detail-section">
            <div class="detail-section-title">Actions</div>
            <div class="detail-actions">
              <button
                v-if="selectedTask.status === 'todo'"
                type="button"
                class="primary-button"
                @click="startTask(selectedTask)"
              >
                Start
              </button>
              <button
                v-if="selectedTask.status === 'review'"
                type="button"
                class="primary-button"
                @click="continueTask(selectedTask)"
              >
                Continue
              </button>
              <button
                v-if="selectedTask.status === 'review'"
                type="button"
                class="tool-button"
                @click="markTask(selectedTask.id, 'done')"
              >
                Done
              </button>
              <button
                v-if="selectedSession"
                type="button"
                class="tool-button"
                @click="openSession(selectedSession)"
              >
                Open terminal
              </button>
              <button
                type="button"
                class="danger-button"
                @click="deleteTask(selectedTask)"
              >
                Delete
              </button>
            </div>
            <form
              v-if="selectedSession"
              class="send-form"
              @submit.prevent="sendDetailMessage"
            >
              <textarea
                v-model="detailMessage"
                placeholder="Follow-up instructions..."
              />
              <button
                type="submit"
                class="primary-button"
                :disabled="!detailMessage.trim()"
              >
                Send
              </button>
            </form>
          </section>

          <section class="detail-section">
            <div class="detail-section-title">Progress</div>
            <div
              v-if="selectedReports.length === 0"
              class="empty-timeline"
            >
              No worker reports yet.
            </div>
            <ol
              v-else
              class="timeline"
            >
              <li
                v-for="report in selectedReports"
                :key="report.id"
              >
                <div class="timeline-dot" />
                <div>
                  <div class="timeline-head">
                    <strong>{{ report.state }}</strong>
                    <span>{{ formatTime(report.created_at) }}</span>
                  </div>
                  <p>{{ report.message }}</p>
                  <div
                    v-if="report.changed_files.length > 0"
                    class="report-files"
                  >
                    <span
                      v-for="file in report.changed_files"
                      :key="file"
                    >
                      {{ file }}
                    </span>
                  </div>
                  <p
                    v-if="report.validation"
                    class="report-note"
                  >
                    Validation: {{ report.validation }}
                  </p>
                  <p
                    v-if="report.risks"
                    class="report-note"
                  >
                    Risks: {{ report.risks }}
                  </p>
                </div>
              </li>
            </ol>
          </section>
        </div>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type {
  AgentReport,
  AgentType,
  ManagedSession,
  WorkspaceTask,
  WorkspaceTaskStatus,
} from '@/types'

interface TaskStartOptions {
  target_session_id: string
  related_task_id: string
  clear_context: boolean
}

const appStore = useAppStore()
const terminalStore = useTerminalStore()
const workspaceStore = useWorkspaceStore()
const {
  workspaces,
  activeWorkspaceId,
  board,
  tasks,
  workspaceAgents,
  dispatcherAgent,
  isLoading,
  error,
} = storeToRefs(workspaceStore)

const selectedWorkspaceId = ref(activeWorkspaceId.value || '')
const selectedTaskId = ref<string | null>(null)
const detailMessage = ref('')
const mobileSetupOpen = ref(false)
const mobileCollapsedColumns = reactive<Record<WorkspaceTaskStatus, boolean>>({
  todo: false,
  queued: false,
  working: false,
  review: false,
  done: false,
})
const startOptions = reactive<Record<string, TaskStartOptions>>({})
let boardPollTimer: number | null = null

const columns: { status: WorkspaceTaskStatus; label: string }[] = [
  { status: 'todo', label: 'Todo' },
  { status: 'queued', label: 'Queued' },
  { status: 'working', label: 'Working' },
  { status: 'review', label: 'Review' },
  { status: 'done', label: 'Done' },
]

const workspaceForm = reactive({
  name: 'Claude Hub',
  path: '/Users/bytedance/claude_hub',
  default_branch: 'main',
  session_prefix: 'chub',
})

const agentForm = reactive({
  title: '',
  agent_type: 'codex' as AgentType,
})

const taskForm = reactive({
  title: '',
  prompt: '',
  agent_type: 'codex' as AgentType,
  related_task_id: '',
})

const activeWorkspace = computed(() =>
  workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
)

const selectedTask = computed(() =>
  tasks.value.find(task => task.id === selectedTaskId.value) || null
)

const selectedSession = computed(() =>
  selectedTask.value ? workspaceStore.sessionForTask(selectedTask.value) : null
)

const selectedReports = computed<AgentReport[]>(() =>
  selectedTask.value ? workspaceStore.reportsForTask(selectedTask.value) : []
)

function startOptionsFor(task: WorkspaceTask): TaskStartOptions {
  if (!startOptions[task.id]) {
    startOptions[task.id] = {
      target_session_id: '',
      related_task_id: '',
      clear_context: false,
    }
  }
  return startOptions[task.id]
}

function tasksByStatus(status: WorkspaceTaskStatus) {
  return tasks.value.filter(task => task.status === status)
}

function sessionForTask(task: WorkspaceTask) {
  return workspaceStore.sessionForTask(task)
}

function latestReportForTask(task: WorkspaceTask) {
  return workspaceStore.latestReportForTask(task)
}

function agentTitle(sessionId?: string | null) {
  if (!sessionId) return 'auto'
  return workspaceAgents.value.find(agent => agent.id === sessionId)?.title || sessionId
}

function taskTitle(taskId?: string | null) {
  if (!taskId) return 'none'
  return tasks.value.find(task => task.id === taskId)?.title || taskId
}

function isInteractiveElement(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest(
    'button, a, input, select, textarea, summary, details, label'
  ))
}

function selectTask(event: MouseEvent, taskId: string) {
  if (isInteractiveElement(event.target)) return
  selectedTaskId.value = taskId
}

function isMobileColumnCollapsed(status: WorkspaceTaskStatus) {
  return mobileCollapsedColumns[status]
}

function toggleMobileColumn(status: WorkspaceTaskStatus) {
  mobileCollapsedColumns[status] = !mobileCollapsedColumns[status]
}

function closeTaskDetail() {
  selectedTaskId.value = null
  detailMessage.value = ''
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function handleCreateWorkspace() {
  const workspace = await workspaceStore.createWorkspace({
    name: workspaceForm.name.trim(),
    path: workspaceForm.path.trim(),
    default_branch: workspaceForm.default_branch.trim() || 'main',
    session_prefix: workspaceForm.session_prefix.trim() || undefined,
  })
  if (workspace) {
    selectedWorkspaceId.value = workspace.id
    mobileSetupOpen.value = false
  }
}

async function handleCreateAgent() {
  await workspaceStore.ensureWorkspaceAgent({
    agent_type: agentForm.agent_type,
    title: agentForm.title.trim() || null,
    role: 'orchestrator',
    reuse_existing: false,
  })
  agentForm.title = ''
  mobileSetupOpen.value = false
  await terminalStore.fetchTabs()
}

async function handleCreateTask() {
  if (!taskForm.title.trim() || !taskForm.prompt.trim()) return
  await workspaceStore.createTask({
    title: taskForm.title.trim(),
    prompt: taskForm.prompt.trim(),
    agent_type: taskForm.agent_type,
    related_task_id: taskForm.related_task_id || null,
  })
  taskForm.title = ''
  taskForm.prompt = ''
  taskForm.related_task_id = ''
  mobileSetupOpen.value = false
}

async function handleWorkspaceChange() {
  if (!selectedWorkspaceId.value) return
  workspaceStore.setActiveWorkspace(selectedWorkspaceId.value)
  await workspaceStore.fetchBoard(selectedWorkspaceId.value)
}

async function refreshBoard() {
  try {
    await workspaceStore.fetchBoard()
  } catch {
    // Error state is owned by the workspace store.
  }
}

async function dispatchWorkspace() {
  try {
    await workspaceStore.dispatchWorkspace()
  } catch {
    // Error state is owned by the workspace store.
  }
}

async function startTask(task: WorkspaceTask) {
  const options = startOptionsFor(task)
  await workspaceStore.startTask(task.id, {
    agent_type: task.agent_type,
    target_session_id: options.target_session_id || null,
    related_task_id: options.related_task_id || null,
    clear_context: options.clear_context ? true : null,
  })
  await terminalStore.fetchTabs()
}

async function continueTask(task: WorkspaceTask) {
  await workspaceStore.continueTask(task.id, {
    message: detailMessage.value.trim() || null,
  })
  detailMessage.value = ''
  await terminalStore.fetchTabs()
}

async function openSession(session: ManagedSession) {
  await terminalStore.fetchTabs()
  appStore.setMode('terminal')
  terminalStore.setActiveTab(session.tab_id)
}

async function openMessagePrompt(sessionId: string) {
  const message = window.prompt('Message to send')
  if (!message?.trim()) return
  await workspaceStore.sendMessage(sessionId, message.trim())
}

async function sendDetailMessage() {
  if (!selectedSession.value || !detailMessage.value.trim()) return
  await workspaceStore.sendMessage(selectedSession.value.id, detailMessage.value.trim())
  detailMessage.value = ''
}

async function markTask(taskId: string, status: WorkspaceTaskStatus) {
  await workspaceStore.updateTaskStatus(taskId, status)
}

async function deleteTask(task: WorkspaceTask) {
  const confirmed = window.confirm(`Delete task "${task.title}"?`)
  if (!confirmed) return
  await workspaceStore.deleteTask(task.id)
  if (selectedTaskId.value === task.id) {
    closeTaskDetail()
  }
}

async function deleteAgent(agent: ManagedSession) {
  const confirmed = window.confirm(`Delete agent "${agent.title}"?`)
  if (!confirmed) return
  await workspaceStore.deleteSession(agent.id)
  await terminalStore.fetchTabs()
}

watch(tasks, value => {
  if (selectedTaskId.value && !value.some(task => task.id === selectedTaskId.value)) {
    closeTaskDetail()
  }
})

watch(activeWorkspaceId, value => {
  selectedWorkspaceId.value = value || ''
  closeTaskDetail()
  mobileSetupOpen.value = false
})

watch(activeWorkspace, workspace => {
  if (!workspace) return
  workspaceForm.name = workspace.name
  workspaceForm.path = workspace.path
  workspaceForm.default_branch = workspace.default_branch
  workspaceForm.session_prefix = workspace.session_prefix
})

onMounted(async () => {
  await workspaceStore.fetchWorkspaces()
  boardPollTimer = window.setInterval(refreshBoard, 2500)
})

onUnmounted(() => {
  if (boardPollTimer !== null) {
    window.clearInterval(boardPollTimer)
    boardPollTimer = null
  }
})
</script>

<style scoped>
.workspace-view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #181818;
  color: #e5e5e5;
}

.workspace-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border-bottom: 1px solid #303030;
  background: #202020;
}

.workspace-header h1 {
  font-size: 18px;
  line-height: 1.2;
  margin: 0 0 4px;
}

.workspace-header p {
  margin: 0;
  color: #9ca3af;
  font-size: 12px;
}

.workspace-actions,
.form-row,
.task-actions,
.session-meta,
.agent-row-actions,
.agent-row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mobile-setup-button,
.mobile-status-strip {
  display: none;
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button,
.setup-panel input,
.setup-panel textarea,
.setup-panel select,
.advanced-start select {
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button {
  height: 30px;
  padding: 0 10px;
}

.tool-button,
.primary-button,
.danger-button,
.task-actions button,
.agent-row button {
  cursor: pointer;
}

.tool-button:disabled,
.primary-button:disabled,
.danger-button:disabled,
.task-actions button:disabled,
.agent-row button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.primary-button {
  background: #3b82f6;
  border-color: #3b82f6;
  font-weight: 700;
}

.danger-button {
  background: #3f1d1d;
  border-color: #7f1d1d;
  color: #fecaca;
}

.workspace-error {
  padding: 8px 16px;
  background: #7f1d1d;
  color: white;
  font-size: 13px;
}

.workspace-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
}

.workspace-layout.detail-open {
  grid-template-columns: 340px minmax(0, 1fr) minmax(380px, 28vw);
}

.setup-panel {
  border-right: 1px solid #303030;
  overflow-y: auto;
  background: #1f1f1f;
}

.setup-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-bottom: 1px solid #303030;
}

.setup-section h2,
.column-header h2,
.task-card h3 {
  margin: 0;
}

.setup-section h2 {
  font-size: 14px;
}

.setup-section label,
.advanced-start label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  color: #a1a1aa;
  font-size: 12px;
}

.setup-panel input,
.setup-panel select,
.advanced-start select {
  height: 32px;
  padding: 0 9px;
}

.setup-panel textarea {
  min-height: 120px;
  resize: vertical;
  padding: 9px;
}

.form-row > label {
  flex: 1;
}

.agent-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-row {
  border: 1px solid #303030;
  border-radius: 6px;
  background: #252525;
  padding: 9px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-row strong {
  display: block;
  color: #fafafa;
  font-size: 12px;
}

.agent-row span {
  color: #a1a1aa;
  font-size: 11px;
}

.dispatcher-row {
  border-color: #4c1d95;
  background: #281f35;
}

.runtime-pill {
  border-radius: 999px;
  padding: 3px 7px;
  background: #3f3f46;
  color: #d4d4d8;
}

.runtime-pill--idle {
  background: #14532d;
  color: #bbf7d0;
}

.runtime-pill--working {
  background: #78350f;
  color: #fde68a;
}

.runtime-pill--attention {
  background: #7f1d1d;
  color: #fecaca;
}

.runtime-pill--offline {
  background: #27272a;
  color: #a1a1aa;
}

.empty-inline {
  color: #71717a;
  font-size: 12px;
}

.board {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(5, minmax(220px, 1fr));
  gap: 10px;
  overflow: auto;
  padding: 10px;
}

.empty-board {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #9ca3af;
}

.task-column {
  min-width: 220px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid #303030;
  border-radius: 8px;
  background: #202020;
}

.column-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px;
  border-bottom: 1px solid #303030;
}

.column-header h2 {
  font-size: 13px;
}

.column-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.column-header span {
  color: #9ca3af;
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
  padding: 8px;
}

.task-card {
  border: 1px solid #3f3f46;
  border-radius: 6px;
  background: #262626;
  padding: 10px;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.task-card.selected {
  border-color: #60a5fa;
  background: #2b3440;
}

.task-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.task-card h3 {
  color: #fafafa;
  font-size: 13px;
  line-height: 1.35;
}

.task-card p {
  margin: 8px 0;
  color: #cbd5e1;
  font-size: 12px;
  line-height: 1.4;
  white-space: pre-wrap;
}

.agent-badge,
.session-meta span {
  border-radius: 999px;
  background: #3f3f46;
  color: #d4d4d8;
  font-size: 10px;
  padding: 3px 7px;
  white-space: nowrap;
}

.agent-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: #71717a;
}

.status-dot--todo {
  background: #71717a;
}

.status-dot--queued {
  background: #38bdf8;
}

.status-dot--working {
  background: #f59e0b;
}

.status-dot--review {
  background: #a855f7;
}

.status-dot--done {
  background: #22c55e;
}

.latest-report {
  margin: 0 0 8px;
  border-left: 2px solid #52525b;
  padding-left: 7px;
  color: #a1a1aa;
  font-size: 11px;
  line-height: 1.35;
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.session-meta {
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.advanced-start {
  margin: 0 0 8px;
  border-top: 1px solid #3f3f46;
  padding-top: 8px;
}

.advanced-start summary {
  margin-bottom: 8px;
  color: #93c5fd;
  font-size: 11px;
  cursor: pointer;
}

.advanced-start label {
  margin-bottom: 8px;
}

.advanced-start .checkbox-label {
  flex-direction: row;
  align-items: center;
}

.advanced-start input[type='checkbox'] {
  width: 14px;
  height: 14px;
}

.task-actions {
  flex-wrap: wrap;
}

.task-actions button,
.agent-row button {
  height: 26px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #303030;
  color: #f4f4f5;
  padding: 0 8px;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.08s ease;
  -webkit-tap-highlight-color: rgba(96, 165, 250, 0.22);
}

.task-actions button:active,
.agent-row button:active,
.tool-button:active,
.primary-button:active,
.danger-button:active {
  transform: translateY(1px);
  background: #3a3a3a;
}

.task-actions .danger-button,
.agent-row .danger-button {
  background: #3f1d1d;
  border-color: #7f1d1d;
}

.column-empty {
  color: #71717a;
  font-size: 12px;
  text-align: center;
  padding: 18px 0;
}

.task-detail-panel {
  border-left: 1px solid #303030;
  background: #1f1f1f;
  min-width: 0;
  overflow-y: auto;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border-bottom: 1px solid #303030;
}

.detail-eyebrow,
.detail-section-title {
  display: block;
  color: #93c5fd;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.detail-header h2 {
  margin: 4px 0 0;
  font-size: 18px;
}

.icon-button {
  width: 28px;
  height: 28px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  cursor: pointer;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 14px;
}

.detail-section {
  border-bottom: 1px solid #303030;
  padding-bottom: 14px;
}

.detail-copy {
  margin: 8px 0 0;
  color: #d4d4d8;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.fact-grid {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.fact-grid div {
  min-width: 0;
  border: 1px solid #303030;
  border-radius: 6px;
  background: #262626;
  padding: 8px;
}

.fact-grid span {
  display: block;
  color: #a1a1aa;
  font-size: 11px;
}

.fact-grid strong {
  display: block;
  margin-top: 4px;
  color: #f4f4f5;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.send-form {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.send-form textarea {
  min-height: 100px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  padding: 9px;
  resize: vertical;
}

.empty-timeline {
  margin-top: 8px;
  color: #71717a;
  font-size: 12px;
}

.timeline {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.timeline li {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr);
  gap: 8px;
}

.timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #60a5fa;
  margin-top: 5px;
}

.timeline-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: #f4f4f5;
  font-size: 12px;
}

.timeline-head span {
  color: #71717a;
}

.timeline p {
  margin: 4px 0 0;
  color: #cbd5e1;
  font-size: 12px;
  line-height: 1.45;
}

.report-files {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
}

.report-files span {
  border-radius: 999px;
  background: #303030;
  color: #d4d4d8;
  font-size: 10px;
  padding: 3px 7px;
}

.report-note {
  color: #a1a1aa;
}

@media (max-width: 760px) {
  .workspace-view {
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  .workspace-header {
    position: sticky;
    top: 0;
    z-index: 20;
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
    padding: 10px 12px;
  }

  .workspace-header h1 {
    font-size: 16px;
    margin: 0;
  }

  .workspace-header p {
    display: none;
  }

  .workspace-actions {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto auto;
    gap: 6px;
  }

  .workspace-select {
    width: 100%;
    min-width: 0;
  }

  .workspace-select,
  .workspace-actions .tool-button {
    height: 36px;
  }

  .workspace-actions .tool-button {
    padding: 0 8px;
  }

  .mobile-setup-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .mobile-status-strip {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid #303030;
    background: #1f1f1f;
  }

  .mobile-agent-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    color: #a1a1aa;
    font-size: 12px;
  }

  .mobile-agent-summary strong {
    color: #f4f4f5;
    font-size: 12px;
  }

  .mobile-column-tabs {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  .mobile-column-tabs span {
    flex: 0 0 auto;
    border: 1px solid #3f3f46;
    border-radius: 999px;
    background: #27272a;
    color: #d4d4d8;
    font-size: 11px;
    padding: 4px 8px;
  }

  .workspace-layout,
  .workspace-layout.detail-open {
    display: block;
    min-height: auto;
  }

  .setup-panel {
    display: none;
    border-right: 0;
    border-bottom: 1px solid #303030;
    overflow: visible;
  }

  .setup-panel.mobile-open {
    display: block;
  }

  .setup-section {
    gap: 8px;
    padding: 12px;
  }

  .setup-panel input,
  .setup-panel select,
  .setup-panel textarea {
    font-size: 16px;
  }

  .setup-panel textarea {
    min-height: 84px;
  }

  .agent-row-meta,
  .agent-row-actions {
    flex-wrap: wrap;
  }

  .board {
    min-height: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: visible;
    padding: 8px;
  }

  .task-column {
    min-width: 0;
    border-radius: 6px;
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
    border: 1px solid #3f3f46;
    border-radius: 4px;
    background: #303030;
    color: #f4f4f5;
    font-size: 12px;
    padding: 0 8px;
    cursor: pointer;
    -webkit-tap-highlight-color: rgba(96, 165, 250, 0.22);
  }

  .column-collapse-button:active {
    background: #3a3a3a;
    transform: translateY(1px);
  }

  .task-list {
    overflow: visible;
    padding: 8px;
  }

  .task-card {
    padding: 10px;
  }

  .task-card.selected {
    border-color: #3f3f46;
    background: #262626;
  }

  .task-card-header {
    align-items: center;
  }

  .task-card p {
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

  .task-actions button,
  .agent-row button {
    width: 100%;
    height: 34px;
  }

  .advanced-start select {
    width: 100%;
    font-size: 16px;
  }

  .task-detail-panel {
    position: fixed;
    inset: 0;
    z-index: 60;
    border-left: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  .detail-header {
    position: sticky;
    top: 0;
    z-index: 2;
    align-items: center;
    background: #1f1f1f;
    padding: 12px;
  }

  .detail-header h2 {
    font-size: 17px;
  }

  .icon-button {
    width: 36px;
    height: 36px;
  }

  .detail-body {
    padding: 12px 12px 80px;
  }

  .fact-grid {
    grid-template-columns: 1fr;
  }

  .detail-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .detail-actions button,
  .send-form button {
    width: 100%;
    height: 38px;
  }

  .send-form textarea {
    min-height: 96px;
    font-size: 16px;
  }
}

@media (max-width: 480px) {
  .workspace-actions {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .workspace-select {
    grid-column: 1 / -1;
  }

  .form-row {
    flex-direction: column;
    align-items: stretch;
  }

  .task-actions,
  .detail-actions {
    grid-template-columns: 1fr;
  }
}
</style>
