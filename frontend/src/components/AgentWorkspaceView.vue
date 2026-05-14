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
          class="tool-button"
          @click="openWorkspaceModal"
        >
          New Workspace
        </button>
        <button
          type="button"
          class="tool-button"
          :disabled="!activeWorkspaceId"
          @click="openAgentOptionsModal"
        >
          Add Agent
        </button>
        <button
          type="button"
          class="primary-button"
          :disabled="!activeWorkspaceId"
          @click="openTaskModal"
        >
          Add Task
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
      class="workspace-summary-strip"
    >
      <div class="workspace-summary-primary">
        <span>{{ workspaceAgents.length }} agents</span>
        <strong>{{ workspaceAgents.filter(agent => agent.runtime_status === 'working').length }} working</strong>
        <span>{{ tasksByStatus('queued').length }} queued</span>
      </div>
      <div class="workspace-column-tabs">
        <span
          v-for="column in columns"
          :key="column.status"
        >
          {{ column.label }} {{ tasksByStatus(column.status).length }}
        </span>
      </div>
    </div>

    <div class="workspace-layout">
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
                    {{ task.status }}
                  </span>
                </div>
                <p class="task-card-description">
                  {{ task.prompt }}
                </p>
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
                  <strong>{{ latestReportForTask(task)?.state }}</strong>
                  <span>{{ latestReportForTask(task)?.message }}</span>
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
    </div>

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
              x
            </button>
          </div>

          <div class="detail-body">
            <section class="detail-section">
              <div class="detail-section-title">
                Task description
              </div>
              <MarkdownContent
                class="detail-copy"
                :text="selectedTask.prompt"
              />
            </section>

            <section class="detail-section">
              <div class="detail-section-title">
                Assignment
              </div>
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
              <div class="detail-section-title">
                Progress
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
                  v-for="report in selectedReports"
                  :key="report.id"
                >
                  <details
                    class="report-card"
                    :open="isLatestSelectedReport(report)"
                  >
                    <summary>
                      <span class="report-state">{{ report.state }}</span>
                      <span class="report-time">{{ formatTime(report.created_at) }}</span>
                    </summary>
                    <MarkdownContent
                      class="report-message"
                      :text="report.message"
                    />
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
                    <div
                      v-if="report.validation"
                      class="report-note"
                    >
                      <strong>Validation</strong>
                      <MarkdownContent
                        compact
                        :text="report.validation"
                      />
                    </div>
                    <div
                      v-if="report.risks"
                      class="report-note"
                    >
                      <strong>Risks</strong>
                      <MarkdownContent
                        compact
                        :text="report.risks"
                      />
                    </div>
                  </details>
                </li>
              </ol>
            </section>
          </div>

          <div class="detail-footer">
            <button
              type="button"
              class="detail-footer-toggle"
              :aria-expanded="isDetailActionsExpanded"
              @click="toggleDetailActions"
            >
              <span>{{ isDetailActionsExpanded ? 'Hide actions' : 'Actions' }}</span>
              <span class="detail-footer-chevron">
                {{ isDetailActionsExpanded ? 'v' : '^' }}
              </span>
            </button>
            <div class="detail-action-drawer">
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
            </div>
          </div>
        </aside>
      </div>
    </Teleport>

    <div
      v-if="showWorkspaceModal"
      class="workspace-modal-overlay"
      @click.self="closeWorkspaceModal"
    >
      <div class="workspace-modal">
        <h3>Create Workspace</h3>
        <form @submit.prevent="handleCreateWorkspace">
          <div class="modal-field">
            <label>Name</label>
            <input
              v-model="workspaceForm.name"
              placeholder="claude_hub"
              autofocus
            />
          </div>
          <div class="modal-field">
            <label>Local workspace dir</label>
            <input
              v-model="workspaceForm.path"
              placeholder="/Users/me/workspace"
            />
          </div>
          <div class="modal-field">
            <label>Environment</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.target === 'local' }]"
                @click="workspaceForm.target = 'local'"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.target === 'remote' }]"
                @click="workspaceForm.target = 'remote'"
              >
                Remote
              </button>
            </div>
          </div>
          <template v-if="workspaceForm.target === 'remote'">
            <div class="modal-field">
              <label>Remote profile</label>
              <select
                v-model="workspaceForm.remote_profile_id"
                :disabled="remoteProfilesLoading"
              >
                <option value="">Select server</option>
                <option
                  v-for="profile in remoteProfiles"
                  :key="profile.id"
                  :value="profile.id"
                >
                  {{ profile.name }}
                </option>
              </select>
            </div>
            <div class="modal-field">
              <label>Remote start dir</label>
              <input
                v-model="workspaceForm.remote_cwd"
                placeholder="~"
              />
            </div>
            <div class="modal-field">
              <label class="checkbox-label">
                <input
                  v-model="workspaceForm.remote_reconnect"
                  type="checkbox"
                />
                Reconnect
              </label>
            </div>
          </template>
          <div class="form-row">
            <div class="modal-field">
              <label>Branch hint</label>
              <input
                v-model="workspaceForm.default_branch"
                placeholder="main"
              />
            </div>
            <div class="modal-field">
              <label>Prefix</label>
              <input
                v-model="workspaceForm.session_prefix"
                placeholder="chub"
              />
            </div>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeWorkspaceModal"
            >
              Cancel
            </button>
            <button
              type="submit"
              class="primary-button"
              :disabled="isLoading || (workspaceForm.target === 'remote' && !workspaceForm.remote_profile_id)"
            >
              Create workspace
            </button>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showTaskModal"
      class="workspace-modal-overlay"
      @click.self="closeTaskModal"
    >
      <div class="workspace-modal">
        <h3>Add Task</h3>
        <form @submit.prevent="handleCreateTask">
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="taskForm.title"
              placeholder="Implement a focused change"
              :disabled="!activeWorkspaceId"
              autofocus
            />
          </div>
          <div class="modal-field">
            <label>Task description</label>
            <textarea
              v-model="taskForm.prompt"
              placeholder="Describe what the workspace agent should implement..."
              :disabled="!activeWorkspaceId"
            />
          </div>
          <div class="modal-field">
            <label>Related task</label>
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
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeTaskModal"
            >
              Cancel
            </button>
            <button
              type="submit"
              class="primary-button"
              :disabled="!activeWorkspaceId || isLoading || !taskForm.title.trim() || !taskForm.prompt.trim()"
            >
              Add task
            </button>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showAgentOptionsModal"
      class="workspace-modal-overlay"
      @click.self="closeAgentOptionsModal"
    >
      <div class="workspace-modal">
        <h3>Add Agent</h3>
        <form @submit.prevent="handleCreateAdvancedAgent">
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="agentOptionsForm.title"
              placeholder="Workspace Agent"
              autofocus
            />
          </div>

          <div class="modal-field">
            <label>Agent Type</label>
            <select v-model="agentOptionsForm.agent_type">
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
              <option value="cursor">Terminal</option>
            </select>
          </div>

          <div class="modal-field">
            <label>Run On</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: agentOptionsForm.target === 'local' }]"
                @click="agentOptionsForm.target = 'local'"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: agentOptionsForm.target === 'remote' }]"
                @click="agentOptionsForm.target = 'remote'"
              >
                Remote
              </button>
            </div>
          </div>

          <div
            v-if="agentOptionsForm.target === 'remote'"
            class="modal-field"
          >
            <label>Remote Server</label>
            <select
              v-model="agentOptionsForm.remote_profile_id"
              :disabled="remoteProfilesLoading"
            >
              <option value="">Select server</option>
              <option
                v-for="profile in remoteProfiles"
                :key="profile.id"
                :value="profile.id"
              >
                {{ profile.name }}
              </option>
            </select>
            <p
              v-if="remoteProfiles.length === 0"
              class="modal-hint"
            >
              Add profiles in ~/.claude_hub/remote_profiles.json or ~/.ssh/config
            </p>
          </div>

          <div class="modal-field">
            <label>Working Directory</label>
            <div class="path-input-row">
              <input
                v-model="agentOptionsForm.cwd"
                :placeholder="agentOptionsForm.target === 'remote' ? '~/workspace/project' : '/Users/me/workspace'"
              />
              <button
                type="button"
                class="tool-button"
                :disabled="agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id"
                @click="openAgentDirectoryBrowser"
              >
                Browse
              </button>
            </div>
          </div>

          <div
            v-if="agentOptionsForm.agent_type !== 'cursor'"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="agentOptionsForm.solo_mode"
                type="checkbox"
              />
              YOLO mode
            </label>
            <p class="modal-hint">{{ agentYoloHint }}</p>
          </div>

          <div
            v-if="agentOptionsForm.target === 'remote'"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="agentOptionsForm.remote_reconnect"
                type="checkbox"
              />
              Auto reconnect
            </label>
          </div>

          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeAgentOptionsModal"
            >
              Cancel
            </button>
            <button
              type="submit"
              class="primary-button"
              :disabled="isAgentOptionsCreateDisabled"
            >
              Create agent
            </button>
          </div>
        </form>

        <section class="modal-section">
          <div class="modal-section-header">
            <h4>Workspace Agents</h4>
            <span>{{ workspaceAgents.length }}</span>
          </div>
          <div class="agent-list">
            <article
              v-for="agent in workspaceAgents"
              :key="agent.id"
              class="agent-row"
            >
              <div>
                <strong>{{ agent.title }}</strong>
                <span>{{ agent.agent_type }} · {{ agent.id }}</span>
                <span>{{ agent.target }} · {{ agent.workspace_path }}</span>
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
        </section>
      </div>
    </div>

    <div
      v-if="showAgentFileBrowser"
      class="workspace-modal-overlay file-browser-overlay"
      @click.self="showAgentFileBrowser = false"
    >
      <div class="workspace-modal file-browser-modal">
        <div class="file-browser-header">
          <h3>{{ agentOptionsForm.target === 'remote' ? 'Select Remote Directory' : 'Select Working Directory' }}</h3>
          <button
            type="button"
            class="tool-button"
            @click="showAgentFileBrowser = false"
          >
            Close
          </button>
        </div>
        <div class="file-browser-path">
          <button
            type="button"
            class="path-nav-button"
            @click="navigateAgentBrowserHome"
          >
            Home
          </button>
          <button
            v-if="agentBrowserParentPath"
            type="button"
            class="path-nav-button"
            @click="loadAgentDirectory(agentBrowserParentPath)"
          >
            Up
          </button>
          <input
            v-model="agentBrowserPathInput"
            @keyup.enter="loadAgentDirectory(agentBrowserPathInput)"
          />
          <button
            type="button"
            class="path-nav-button"
            @click="loadAgentDirectory(agentBrowserCurrentPath || agentBrowserPathInput || '~')"
          >
            Refresh
          </button>
        </div>
        <div class="file-browser-list">
          <div
            v-if="agentBrowserParentPath"
            class="file-item is-dir"
            @click="loadAgentDirectory(agentBrowserParentPath)"
          >
            <span>Dir</span>
            <strong>..</strong>
          </div>
          <div
            v-for="item in agentBrowserItems"
            :key="item.path"
            :class="['file-item', { 'is-dir': item.is_dir }]"
            @click="handleAgentFileItemClick(item)"
          >
            <span>{{ item.is_dir ? 'Dir' : 'File' }}</span>
            <strong>{{ item.name }}</strong>
          </div>
          <div
            v-if="agentBrowserLoading"
            class="file-status"
          >
            Loading...
          </div>
          <div
            v-if="agentBrowserError"
            class="file-status file-error"
          >
            {{ agentBrowserError }}
          </div>
        </div>
        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="showAgentFileBrowser = false"
          >
            Cancel
          </button>
          <button
            type="button"
            class="primary-button"
            @click="selectAgentCurrentDirectory"
          >
            Select directory
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type {
  AgentReport,
  AgentType,
  ExecutionTarget,
  ManagedSession,
  RemoteProfile,
  WorkspaceTask,
  WorkspaceTaskStatus,
} from '@/types'

interface TaskStartOptions {
  target_session_id: string
  related_task_id: string
  clear_context: boolean
}

interface FileInfo {
  name: string
  path: string
  is_dir: boolean
}

interface DirectoryListing {
  current_path: string
  parent_path: string | null
  items: FileInfo[]
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
const isDetailActionsExpanded = ref(false)
const showWorkspaceModal = ref(false)
const showAgentOptionsModal = ref(false)
const showAgentFileBrowser = ref(false)
const showTaskModal = ref(false)
const remoteProfiles = ref<RemoteProfile[]>([])
const remoteProfilesLoading = ref(false)
const agentBrowserCurrentPath = ref('')
const agentBrowserPathInput = ref('')
const agentBrowserParentPath = ref<string | null>(null)
const agentBrowserItems = ref<FileInfo[]>([])
const agentBrowserLoading = ref(false)
const agentBrowserError = ref<string | null>(null)
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
  target: 'local' as ExecutionTarget,
  remote_profile_id: '',
  remote_cwd: '',
  remote_reconnect: true,
})

const agentOptionsForm = reactive({
  title: '',
  agent_type: 'codex' as AgentType,
  target: 'local' as ExecutionTarget,
  cwd: '',
  solo_mode: true,
  remote_profile_id: '',
  remote_reconnect: true,
})

const taskForm = reactive({
  title: '',
  prompt: '',
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

const selectedRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === workspaceForm.remote_profile_id) || null
)

const selectedAgentRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === agentOptionsForm.remote_profile_id) || null
)

const agentYoloHint = computed(() => {
  if (agentOptionsForm.agent_type === 'codex') {
    return 'Runs Codex with --ask-for-approval never and --sandbox danger-full-access'
  }
  return 'Runs Claude with IS_SANDBOX=1 and --dangerously-skip-permissions'
})

const isAgentOptionsCreateDisabled = computed(
  () =>
    isLoading.value ||
    (agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id)
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

function isLatestSelectedReport(report: AgentReport) {
  return selectedReports.value[selectedReports.value.length - 1]?.id === report.id
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
  if (selectedTaskId.value !== taskId) {
    detailMessage.value = ''
    isDetailActionsExpanded.value = false
  }
  selectedTaskId.value = taskId
}

function isMobileColumnCollapsed(status: WorkspaceTaskStatus) {
  return mobileCollapsedColumns[status]
}

function toggleMobileColumn(status: WorkspaceTaskStatus) {
  mobileCollapsedColumns[status] = !mobileCollapsedColumns[status]
}

function toggleDetailActions() {
  isDetailActionsExpanded.value = !isDetailActionsExpanded.value
}

function closeTaskDetail() {
  selectedTaskId.value = null
  detailMessage.value = ''
  isDetailActionsExpanded.value = false
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function fetchRemoteProfiles() {
  remoteProfilesLoading.value = true
  try {
    const response = await fetch('/api/remote/profiles')
    if (!response.ok) throw new Error('Failed to load remote profiles')
    remoteProfiles.value = await response.json()
    if (!workspaceForm.remote_profile_id && remoteProfiles.value.length > 0) {
      workspaceForm.remote_profile_id = remoteProfiles.value[0].id
    }
  } catch (e) {
    workspaceStore.error = e instanceof Error ? e.message : 'Failed to load remote profiles'
  } finally {
    remoteProfilesLoading.value = false
  }
}

async function handleCreateWorkspace() {
  const workspace = await workspaceStore.createWorkspace({
    name: workspaceForm.name.trim(),
    path: workspaceForm.path.trim(),
    default_branch: workspaceForm.default_branch.trim() || 'main',
    session_prefix: workspaceForm.session_prefix.trim() || undefined,
    target: workspaceForm.target,
    remote_profile_id:
      workspaceForm.target === 'remote' ? workspaceForm.remote_profile_id || null : null,
    remote_cwd:
      workspaceForm.target === 'remote' ? workspaceForm.remote_cwd.trim() || null : null,
    remote_reconnect: workspaceForm.remote_reconnect,
  })
  if (workspace) {
    selectedWorkspaceId.value = workspace.id
    showWorkspaceModal.value = false
  }
}

function resetWorkspaceForm() {
  workspaceForm.name = 'Claude Hub'
  workspaceForm.path = '/Users/bytedance/claude_hub'
  workspaceForm.default_branch = 'main'
  workspaceForm.session_prefix = 'chub'
  workspaceForm.target = 'local'
  workspaceForm.remote_profile_id = remoteProfiles.value[0]?.id || ''
  workspaceForm.remote_cwd = ''
  workspaceForm.remote_reconnect = true
}

function openWorkspaceModal() {
  resetWorkspaceForm()
  showWorkspaceModal.value = true
}

function closeWorkspaceModal() {
  showWorkspaceModal.value = false
}

function resetAgentOptionsForm() {
  const workspace = activeWorkspace.value
  agentOptionsForm.title = ''
  agentOptionsForm.agent_type = 'codex'
  agentOptionsForm.target = 'local'
  agentOptionsForm.solo_mode = true
  agentOptionsForm.remote_reconnect = workspace?.remote_reconnect ?? true
  agentOptionsForm.remote_profile_id =
    workspace?.remote_profile_id || remoteProfiles.value[0]?.id || ''
  agentOptionsForm.cwd = workspace?.path || ''
}

function openAgentOptionsModal() {
  resetAgentOptionsForm()
  if (agentOptionsForm.target === 'remote') {
    fetchRemoteProfiles()
  }
  showAgentOptionsModal.value = true
}

function closeAgentOptionsModal() {
  showAgentOptionsModal.value = false
  showAgentFileBrowser.value = false
}

async function handleCreateAdvancedAgent() {
  const cwd = agentOptionsForm.cwd.trim()
  await workspaceStore.ensureWorkspaceAgent({
    agent_type: agentOptionsForm.agent_type,
    title: agentOptionsForm.title.trim() || null,
    role: 'orchestrator',
    reuse_existing: false,
    target: agentOptionsForm.target,
    cwd: agentOptionsForm.target === 'local' ? cwd || null : null,
    remote_profile_id:
      agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_profile_id || null : null,
    remote_cwd: agentOptionsForm.target === 'remote' ? cwd || null : null,
    remote_reconnect:
      agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_reconnect : null,
    solo_mode: agentOptionsForm.agent_type === 'cursor' ? false : agentOptionsForm.solo_mode,
  })
  showAgentOptionsModal.value = false
  showAgentFileBrowser.value = false
  await terminalStore.fetchTabs()
}

async function listAgentDirectory(path?: string): Promise<DirectoryListing> {
  const params = new URLSearchParams()
  if (path) {
    params.append('path', path)
  }
  if (agentOptionsForm.target === 'remote') {
    if (!agentOptionsForm.remote_profile_id) {
      throw new Error('Select a remote server first')
    }
    params.append('profile_id', agentOptionsForm.remote_profile_id)
  }
  const endpoint =
    agentOptionsForm.target === 'remote' ? '/api/remote/filesystem/list' : '/api/filesystem/list'
  const queryString = params.toString()
  const response = await fetch(`${endpoint}${queryString ? `?${queryString}` : ''}`)
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to list directory')
  }
  return await response.json()
}

async function loadAgentDirectory(path?: string) {
  agentBrowserLoading.value = true
  agentBrowserError.value = null
  try {
    const listing = await listAgentDirectory(path)
    agentBrowserCurrentPath.value = listing.current_path
    agentBrowserPathInput.value = listing.current_path
    agentBrowserParentPath.value = listing.parent_path
    agentBrowserItems.value = listing.items
  } catch (e) {
    agentBrowserError.value = e instanceof Error ? e.message : 'Failed to load directory'
  } finally {
    agentBrowserLoading.value = false
  }
}

function openAgentDirectoryBrowser() {
  showAgentFileBrowser.value = true
  if (agentOptionsForm.cwd) {
    loadAgentDirectory(agentOptionsForm.cwd)
  } else if (agentOptionsForm.target === 'remote') {
    loadAgentDirectory(selectedAgentRemoteProfile.value?.default_cwd || '~')
  } else {
    loadAgentDirectory('~')
  }
}

function navigateAgentBrowserHome() {
  if (agentOptionsForm.target === 'remote') {
    loadAgentDirectory(selectedAgentRemoteProfile.value?.default_cwd || '~')
  } else {
    loadAgentDirectory('~')
  }
}

function handleAgentFileItemClick(item: FileInfo) {
  if (item.is_dir) {
    loadAgentDirectory(item.path)
  }
}

function selectAgentCurrentDirectory() {
  agentOptionsForm.cwd = agentBrowserCurrentPath.value
  showAgentFileBrowser.value = false
}

function resetTaskForm() {
  taskForm.title = ''
  taskForm.prompt = ''
  taskForm.related_task_id = ''
}

function openTaskModal() {
  resetTaskForm()
  showTaskModal.value = true
}

function closeTaskModal() {
  showTaskModal.value = false
}

async function handleCreateTask() {
  if (!taskForm.title.trim() || !taskForm.prompt.trim()) return
  await workspaceStore.createTask({
    title: taskForm.title.trim(),
    prompt: taskForm.prompt.trim(),
    related_task_id: taskForm.related_task_id || null,
  })
  taskForm.title = ''
  taskForm.prompt = ''
  taskForm.related_task_id = ''
  showTaskModal.value = false
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

async function startTask(task: WorkspaceTask) {
  const options = startOptionsFor(task)
  await workspaceStore.startTask(task.id, {
    target_session_id: options.target_session_id || null,
    related_task_id: options.related_task_id || null,
    clear_context: options.clear_context ? true : null,
  })
  await terminalStore.fetchTabs()
}

async function openSession(session: ManagedSession) {
  await terminalStore.fetchTabs()
  appStore.setMode('terminal')
  terminalStore.setActiveTab(session.tab_id)
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
})

watch(
  () => workspaceForm.target,
  target => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      if (!workspaceForm.remote_cwd && selectedRemoteProfile.value?.default_cwd) {
        workspaceForm.remote_cwd = selectedRemoteProfile.value.default_cwd
      }
    }
  }
)

watch(
  () => workspaceForm.remote_profile_id,
  () => {
    if (workspaceForm.target === 'remote' && !workspaceForm.remote_cwd) {
      workspaceForm.remote_cwd = selectedRemoteProfile.value?.default_cwd || ''
    }
  }
)

watch(
  () => agentOptionsForm.agent_type,
  agentType => {
    if (agentType === 'cursor') {
      agentOptionsForm.solo_mode = false
    } else if (!showAgentOptionsModal.value) {
      agentOptionsForm.solo_mode = true
    }
  }
)

watch(
  () => agentOptionsForm.target,
  target => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      if (!agentOptionsForm.remote_profile_id) {
        agentOptionsForm.remote_profile_id =
          activeWorkspace.value?.remote_profile_id || remoteProfiles.value[0]?.id || ''
      }
      agentOptionsForm.cwd =
        activeWorkspace.value?.remote_cwd || selectedAgentRemoteProfile.value?.default_cwd || '~'
    } else {
      agentOptionsForm.cwd = activeWorkspace.value?.path || ''
    }
  }
)

watch(
  () => agentOptionsForm.remote_profile_id,
  () => {
    if (agentOptionsForm.target === 'remote' && !agentOptionsForm.cwd) {
      agentOptionsForm.cwd = selectedAgentRemoteProfile.value?.default_cwd || '~'
    }
  }
)

onMounted(async () => {
  await fetchRemoteProfiles()
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

.workspace-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
}

.workspace-summary-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid #303030;
  background: #1f1f1f;
}

.workspace-summary-primary,
.workspace-column-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.workspace-summary-primary {
  color: #a1a1aa;
  font-size: 12px;
}

.workspace-summary-primary strong {
  color: #f4f4f5;
}

.workspace-column-tabs {
  overflow-x: auto;
}

.workspace-column-tabs span {
  flex: 0 0 auto;
  border: 1px solid #3f3f46;
  border-radius: 999px;
  background: #27272a;
  color: #d4d4d8;
  font-size: 11px;
  padding: 4px 8px;
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button,
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

.workspace-select {
  min-width: 220px;
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
  color: #a1a1aa;
  font-size: 12px;
}

.advanced-start select {
  height: 32px;
  padding: 0 9px;
}

.form-row > label,
.form-row > .modal-field {
  flex: 1;
}

.agent-submit-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.compact-button {
  flex: 0 0 auto;
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
  display: block;
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

.task-card-description {
  max-width: 100%;
  margin: 6px 0 8px;
  color: #aeb7c3;
  font-size: 11px;
  line-height: 1.35;
  white-space: normal;
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
  word-break: break-all;
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
  overflow-wrap: anywhere;
  word-break: break-all;
}

.latest-report strong {
  color: #d4d4d8;
  font-weight: 700;
}

.latest-report span {
  margin-left: 4px;
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

.task-detail-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  overscroll-behavior: contain;
  background: rgba(0, 0, 0, 0.58);
  padding: 24px;
}

.task-detail-panel {
  width: min(960px, calc(100vw - 48px));
  max-height: min(860px, calc(100dvh - 48px));
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #3f3f46;
  border-radius: 8px;
  background: #1f1f1f;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
}

.detail-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid #303030;
  background: #1f1f1f;
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
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  overflow-y: auto;
  padding: 14px 16px;
}

.detail-section {
  border-bottom: 1px solid #303030;
  padding-bottom: 14px;
}

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
}

.detail-footer-toggle {
  display: none;
}

.detail-action-drawer {
  display: block;
}

.send-form {
  margin-top: 10px;
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.send-form textarea {
  flex: 1;
  min-height: 100px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  padding: 9px;
  resize: vertical;
}

.detail-footer {
  position: sticky;
  bottom: 0;
  z-index: 2;
  border-top: 1px solid #303030;
  background: #1f1f1f;
  padding: 12px 16px;
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
  gap: 10px;
}

.timeline li {
  min-width: 0;
}

.report-card {
  border: 1px solid #303030;
  border-radius: 6px;
  background: #262626;
  padding: 0;
}

.report-card summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  padding: 10px;
  color: #f4f4f5;
  font-size: 12px;
}

.report-card summary::-webkit-details-marker {
  display: none;
}

.report-card[open] summary {
  border-bottom: 1px solid #303030;
}

.report-state {
  position: relative;
  padding-left: 15px;
  font-weight: 700;
}

.report-state::before {
  content: '';
  position: absolute;
  top: 50%;
  left: 0;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #60a5fa;
  transform: translateY(-50%);
}

.report-time {
  flex: 0 0 auto;
  color: #71717a;
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

.report-files span {
  border-radius: 999px;
  background: #303030;
  color: #d4d4d8;
  font-size: 10px;
  padding: 3px 7px;
}

.report-note {
  margin: 0 10px 10px;
  border-left: 2px solid #52525b;
  padding-left: 8px;
  color: #a1a1aa;
}

.report-note strong {
  display: block;
  margin-bottom: 5px;
  color: #d4d4d8;
  font-size: 11px;
  text-transform: uppercase;
}

.workspace-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  background: rgb(0 0 0 / 55%);
  padding: 16px;
}

.file-browser-overlay {
  z-index: 1100;
}

.workspace-modal {
  width: min(520px, 100%);
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  border: 1px solid #333;
  border-radius: 8px;
  background: #1e1e1e;
  padding: 20px;
}

.workspace-modal h3 {
  margin: 0 0 16px;
  color: #fafafa;
  font-size: 18px;
}

.modal-field {
  margin-bottom: 14px;
}

.modal-field label {
  display: block;
  margin-bottom: 6px;
  color: #c4c4cc;
  font-size: 13px;
}

.modal-field input,
.modal-field textarea,
.modal-field select,
.file-browser-path input {
  width: 100%;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  font-size: 14px;
  box-sizing: border-box;
}

.modal-field input,
.modal-field select {
  height: 34px;
  padding: 0 10px;
}

.modal-field textarea {
  min-height: 120px;
  resize: vertical;
  padding: 10px;
}

.modal-field input:focus,
.modal-field textarea:focus,
.modal-field select:focus,
.file-browser-path input:focus {
  outline: none;
  border-color: #60a5fa;
}

.modal-field .checkbox-label {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin-bottom: 0;
  color: #f4f4f5;
}

.modal-field .checkbox-label input {
  width: 16px;
  height: 16px;
}

.modal-hint {
  margin: 6px 0 0;
  color: #8f8f99;
  font-size: 12px;
  line-height: 1.35;
}

.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  border: 1px solid #333;
  border-radius: 4px;
  background: #171717;
  padding: 4px;
}

.segment-button {
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: #a1a1aa;
  cursor: pointer;
  font-size: 14px;
  padding: 8px 10px;
}

.segment-button.active {
  border-color: #555;
  background: #2d2d2d;
  color: #fafafa;
}

.path-input-row {
  display: flex;
  gap: 8px;
}

.path-input-row input {
  min-width: 0;
  flex: 1;
}

.path-input-row .tool-button {
  height: 34px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}

.modal-section {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid #333;
}

.modal-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.modal-section-header h4 {
  margin: 0;
  color: #fafafa;
  font-size: 13px;
}

.modal-section-header span {
  color: #a1a1aa;
  font-size: 12px;
}

.file-browser-modal {
  width: min(720px, 100%);
  height: min(70dvh, 620px);
  display: flex;
  flex-direction: column;
}

.file-browser-header,
.file-browser-path {
  display: flex;
  align-items: center;
  gap: 8px;
}

.file-browser-header {
  justify-content: space-between;
  margin-bottom: 12px;
}

.file-browser-header h3 {
  margin: 0;
}

.file-browser-path {
  border-radius: 4px;
  background: #252525;
  padding: 8px;
}

.file-browser-path input {
  min-width: 0;
  flex: 1;
  height: 30px;
  padding: 0 8px;
  font-family: monospace;
  font-size: 12px;
}

.path-nav-button {
  height: 30px;
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #2b2b2b;
  color: #f4f4f5;
  cursor: pointer;
  padding: 0 8px;
}

.file-browser-list {
  flex: 1;
  min-height: 160px;
  overflow-y: auto;
  border: 1px solid #333;
  border-radius: 4px;
  background: #1a1a1a;
  margin-top: 12px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  color: #a1a1aa;
  cursor: default;
}

.file-item.is-dir {
  color: #93c5fd;
  cursor: pointer;
}

.file-item:hover {
  background: #252525;
}

.file-item span {
  width: 28px;
  color: #71717a;
  font-size: 11px;
}

.file-item strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 500;
}

.file-status {
  padding: 16px;
  text-align: center;
  color: #8f8f99;
  font-size: 13px;
}

.file-error {
  color: #f87171;
}

@media (max-width: 760px) {
  .workspace-view {
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  .workspace-modal-overlay {
    align-items: flex-start;
    padding: 10px;
  }

  .workspace-modal {
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 14px;
    border-radius: 6px;
  }

  .file-browser-modal {
    height: calc(100dvh - 20px);
  }

  .path-input-row,
  .file-browser-path {
    flex-wrap: wrap;
  }

  .path-input-row .tool-button,
  .file-browser-path input {
    flex: 1 1 100%;
  }

  .modal-actions {
    position: sticky;
    bottom: -1px;
    background: #1e1e1e;
    padding-top: 10px;
  }

  .modal-actions .tool-button,
  .modal-actions .primary-button {
    flex: 1;
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
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  .workspace-select {
    grid-column: 1 / -1;
    width: 100%;
    min-width: 0;
  }

  .workspace-select,
  .workspace-actions .tool-button,
  .workspace-actions .primary-button {
    height: 36px;
  }

  .workspace-actions .tool-button,
  .workspace-actions .primary-button {
    width: 100%;
    padding: 0 8px;
  }

  .workspace-summary-strip {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    padding: 8px 12px;
  }

  .workspace-summary-primary {
    justify-content: space-between;
  }

  .workspace-column-tabs {
    padding-bottom: 2px;
  }

  .workspace-layout {
    display: block;
    min-height: auto;
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

  .task-actions button,
  .agent-row button {
    width: 100%;
    height: 34px;
  }

  .advanced-start select {
    width: 100%;
    font-size: 16px;
  }

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
    background: #1f1f1f;
    padding: 12px;
  }

  .detail-header h2 {
    font-size: 17px;
  }

  .icon-button {
    width: 34px;
    height: 34px;
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

  .fact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .detail-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .detail-actions .primary-button {
    grid-column: 1 / -1;
  }

  .detail-footer {
    position: sticky;
    bottom: 0;
    padding: 8px 12px max(10px, env(safe-area-inset-bottom));
    box-shadow: 0 -12px 24px rgba(0, 0, 0, 0.24);
  }

  .detail-footer-toggle {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    background: #2b2b2b;
    color: #f4f4f5;
    font-size: 14px;
    font-weight: 700;
  }

  .detail-footer-chevron {
    color: #93c5fd;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }

  .detail-action-drawer {
    display: none;
    margin-top: 8px;
  }

  .task-detail-panel.mobile-actions-expanded .detail-action-drawer {
    display: block;
  }

  .send-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 88px;
    align-items: end;
  }

  .detail-actions button,
  .send-form button {
    width: auto;
    min-width: 0;
    height: 36px;
  }

  .send-form textarea {
    min-height: 44px;
    max-height: 92px;
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

  .task-actions {
    grid-template-columns: 1fr;
  }
}
</style>
