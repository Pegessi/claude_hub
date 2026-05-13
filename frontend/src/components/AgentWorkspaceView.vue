<template>
  <section class="workspace-view">
    <header class="workspace-header">
      <div>
        <h1>Agent Workspace</h1>
        <p>Tasks dispatched to a resident workspace agent terminal.</p>
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
          class="tool-button"
          :disabled="!activeWorkspaceId"
          @click="ensureWorkspaceAgent"
        >
          {{ workspaceAgent ? 'Agent ready' : 'Start agent' }}
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

    <div :class="['workspace-layout', { 'detail-open': selectedTask }]">
      <aside class="setup-panel">
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
              placeholder="/Users/Apple/Project/..."
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
            Agent
            <select
              v-model="taskForm.agent_type"
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
            Add task
          </button>
        </form>
      </aside>

      <main class="board">
        <div
          v-if="!activeWorkspaceId"
          class="empty-board"
        >
          Create a workspace to start a resident agent and assign tasks.
        </div>

        <template v-else>
          <section
            v-for="column in columns"
            :key="column.status"
            class="task-column"
          >
            <div class="column-header">
              <h2>{{ column.label }}</h2>
              <span>{{ tasksByStatus(column.status).length }}</span>
            </div>
            <div class="task-list">
              <article
                v-for="task in tasksByStatus(column.status)"
                :key="task.id"
                :class="['task-card', { selected: selectedTaskId === task.id }]"
                @click="selectTask(task.id)"
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
                  v-if="latestReportForTask(task)"
                  class="latest-report"
                >
                  {{ latestReportForTask(task)?.state }} · {{ latestReportForTask(task)?.message }}
                </div>
                <div
                  v-if="sessionForTask(task)"
                  class="session-meta"
                >
                  <span>task {{ task.status }}</span>
                  <span>agent {{ sessionForTask(task)?.status }}</span>
                  <span>{{ sessionForTask(task)?.branch || 'no branch' }}</span>
                </div>
                <div class="task-actions">
                  <button
                    v-if="task.status === 'todo'"
                    type="button"
                    @click.stop="startTask(task)"
                  >
                    Start task
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
                    v-if="task.status !== 'done'"
                    type="button"
                    @click.stop="markTask(task.id, nextStatus(task.status))"
                  >
                    Move {{ nextStatus(task.status) }}
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
            ×
          </button>
        </div>

        <div class="detail-body">
          <section class="detail-section">
            <div class="detail-section-title">Task description</div>
            <p class="detail-copy">{{ selectedTask.prompt }}</p>
          </section>

          <section class="detail-section">
            <div class="detail-section-title">Session</div>
            <div class="fact-grid">
              <div>
                <span>Task stage</span>
                <strong>{{ selectedTask.status }}</strong>
              </div>
              <div>
                <span>Agent runtime</span>
                <strong>{{ selectedSession?.status || 'not spawned' }}</strong>
              </div>
              <div>
                <span>Agent</span>
                <strong>{{ selectedTask.agent_type }}</strong>
              </div>
              <div>
                <span>Branch</span>
                <strong>{{ selectedSession?.branch || 'none' }}</strong>
              </div>
              <div>
                <span>Worktree</span>
                <strong>{{ selectedSession?.workspace_path || 'none' }}</strong>
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
                Start task
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
                v-if="selectedTask.status !== 'done'"
                type="button"
                class="tool-button"
                @click="markTask(selectedTask.id, nextStatus(selectedTask.status))"
              >
                Move {{ nextStatus(selectedTask.status) }}
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
                placeholder="Send follow-up instructions..."
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

const appStore = useAppStore()
const terminalStore = useTerminalStore()
const workspaceStore = useWorkspaceStore()
const {
  workspaces,
  activeWorkspaceId,
  tasks,
  workspaceAgent,
  isLoading,
  error,
} = storeToRefs(workspaceStore)

const selectedWorkspaceId = ref(activeWorkspaceId.value || '')
const selectedTaskId = ref<string | null>(null)
const detailMessage = ref('')
let boardPollTimer: number | null = null

const columns: { status: WorkspaceTaskStatus; label: string }[] = [
  { status: 'todo', label: 'Todo' },
  { status: 'assigned', label: 'Assigned' },
  { status: 'working', label: 'Working' },
  { status: 'review', label: 'Review' },
  { status: 'done', label: 'Done' },
]

const workspaceForm = reactive({
  name: 'Claude Hub',
  path: '/Users/Apple/Project/claude_projects/claude_hub',
  default_branch: 'main',
  session_prefix: 'chub',
})

const taskForm = reactive({
  title: '',
  prompt: '',
  agent_type: 'codex' as AgentType,
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

function tasksByStatus(status: WorkspaceTaskStatus) {
  return tasks.value.filter(task => task.status === status)
}

function sessionForTask(task: WorkspaceTask) {
  return workspaceStore.sessionForTask(task)
}

function latestReportForTask(task: WorkspaceTask) {
  return workspaceStore.latestReportForTask(task)
}

function selectTask(taskId: string) {
  selectedTaskId.value = taskId
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

function nextStatus(status: WorkspaceTaskStatus): WorkspaceTaskStatus {
  if (status === 'todo') return 'assigned'
  if (status === 'assigned') return 'working'
  if (status === 'working') return 'review'
  if (status === 'review') return 'done'
  return 'done'
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
  }
}

async function handleCreateTask() {
  if (!taskForm.title.trim() || !taskForm.prompt.trim()) return
  await workspaceStore.createTask({
    title: taskForm.title.trim(),
    prompt: taskForm.prompt.trim(),
    agent_type: taskForm.agent_type,
  })
  taskForm.title = ''
  taskForm.prompt = ''
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

async function ensureWorkspaceAgent() {
  await workspaceStore.ensureWorkspaceAgent(taskForm.agent_type)
  await terminalStore.fetchTabs()
}

async function startTask(task: WorkspaceTask) {
  await workspaceStore.startTask(task.id, task.agent_type)
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

watch(tasks, value => {
  if (selectedTaskId.value && !value.some(task => task.id === selectedTaskId.value)) {
    closeTaskDetail()
  }
})

watch(activeWorkspaceId, value => {
  selectedWorkspaceId.value = value || ''
  closeTaskDetail()
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
.session-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button,
.setup-panel input,
.setup-panel textarea,
.setup-panel select {
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
.task-actions button {
  cursor: pointer;
}

.tool-button:disabled,
.primary-button:disabled,
.danger-button:disabled {
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
  grid-template-columns: 320px minmax(0, 1fr);
}

.workspace-layout.detail-open {
  grid-template-columns: 320px minmax(0, 1fr) minmax(380px, 28vw);
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

.setup-section label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  color: #a1a1aa;
  font-size: 12px;
}

.setup-panel input,
.setup-panel select {
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

.column-header span {
  color: #9ca3af;
  font-size: 12px;
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

.status-dot--assigned {
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

.task-actions {
  flex-wrap: wrap;
}

.task-actions button {
  height: 26px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #303030;
  color: #f4f4f5;
  padding: 0 8px;
  font-size: 11px;
}

.task-actions button:hover {
  background: #3a3a3a;
}

.task-actions .danger-button:hover {
  background: #5f1f1f;
}

.column-empty {
  padding: 14px;
  color: #71717a;
  font-size: 12px;
  text-align: center;
}

.task-detail-panel {
  min-width: 0;
  min-height: 0;
  border-left: 1px solid #303030;
  background: #1f1f1f;
  display: flex;
  flex-direction: column;
}

.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid #303030;
  padding: 14px;
}

.detail-eyebrow {
  display: inline-flex;
  margin-bottom: 6px;
  color: #93c5fd;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.detail-header h2 {
  margin: 0;
  color: #fafafa;
  font-size: 16px;
  line-height: 1.3;
}

.icon-button {
  width: 30px;
  height: 30px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
}

.detail-body {
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px 16px;
}

.detail-section {
  padding: 12px 0;
  border-bottom: 1px solid #303030;
}

.detail-section:last-child {
  border-bottom: 0;
}

.detail-section-title {
  margin-bottom: 8px;
  color: #a1a1aa;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.detail-copy {
  margin: 0;
  color: #d4d4d8;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.fact-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.fact-grid div {
  min-width: 0;
  border: 1px solid #303030;
  border-radius: 6px;
  background: #242424;
  padding: 8px;
}

.fact-grid span {
  display: block;
  margin-bottom: 4px;
  color: #71717a;
  font-size: 10px;
}

.fact-grid strong {
  display: block;
  overflow-wrap: anywhere;
  color: #f4f4f5;
  font-size: 11px;
  line-height: 1.35;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.send-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}

.send-form textarea {
  min-height: 90px;
  resize: vertical;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  padding: 9px;
}

.empty-timeline {
  color: #71717a;
  font-size: 12px;
}

.timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.timeline li {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr);
  gap: 8px;
}

.timeline-dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 999px;
  background: #60a5fa;
}

.timeline-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.timeline-head strong {
  color: #f4f4f5;
  font-size: 12px;
}

.timeline-head span {
  color: #71717a;
  font-size: 10px;
  white-space: nowrap;
}

.timeline p {
  margin: 4px 0 0;
  color: #d4d4d8;
  font-size: 12px;
  line-height: 1.45;
}

.report-files {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 7px;
}

.report-files span,
.report-note {
  border-radius: 4px;
  background: #303030;
  color: #cbd5e1;
  font-size: 10px;
  padding: 3px 6px;
}

@media (max-width: 900px) {
  .workspace-layout {
    grid-template-columns: 1fr;
  }

  .workspace-layout.detail-open {
    grid-template-columns: 1fr;
  }

  .setup-panel {
    max-height: 44vh;
    border-right: 0;
    border-bottom: 1px solid #303030;
  }

  .board {
    grid-template-columns: repeat(5, minmax(220px, 70vw));
  }

  .task-detail-panel {
    position: fixed;
    inset: auto 0 0 0;
    z-index: 30;
    max-height: min(78vh, 720px);
    border-top: 1px solid #3f3f46;
    border-left: 0;
    border-radius: 12px 12px 0 0;
    box-shadow: 0 -16px 48px rgb(0 0 0 / 0.45);
  }

  .detail-header {
    padding: 12px 14px;
  }

  .detail-body {
    padding-bottom: calc(16px + env(safe-area-inset-bottom));
  }

  .fact-grid {
    grid-template-columns: 1fr;
  }
}
</style>
