<template>
  <div
    v-if="visible"
    class="modal-overlay"
    @click.self="handleClose"
  >
    <div class="modal st-modal">
      <div class="st-header">
        <h3>Scheduled Tasks</h3>
        <button
          type="button"
          class="ch-btn ch-btn--sm"
          @click="handleClose"
        >
          Close
        </button>
      </div>

      <div
        v-if="formError"
        class="st-banner"
        role="alert"
      >
        <span class="st-banner-text">{{ formError }}</span>
        <button
          type="button"
          class="st-banner-close"
          :aria-label="'Dismiss error'"
          @click="formError = ''"
        >
          ×
        </button>
      </div>

      <!-- ===================== LIST VIEW ===================== -->
      <div
        v-if="mode === 'list'"
        class="st-body"
      >
        <div class="st-toolbar">
          <span class="st-count">
            {{ store.tasks.length }} task{{ store.tasks.length === 1 ? '' : 's' }} ·
            {{ store.enabledCount }} enabled
          </span>
          <button
            type="button"
            class="ch-btn ch-btn--sm ch-btn--primary"
            @click="startCreate()"
          >
            + New Task
          </button>
        </div>

        <div
          v-if="store.isLoading"
          class="st-empty"
        >
          Loading…
        </div>
        <div
          v-else-if="store.error"
          class="st-empty st-error"
        >
          {{ store.error }}
          <button
            type="button"
            class="ch-btn ch-btn--sm"
            @click="store.fetchTasks()"
          >
            Retry
          </button>
        </div>
        <div
          v-else-if="store.tasks.length === 0"
          class="st-empty"
        >
          <p>No scheduled tasks yet.</p>
          <p class="st-empty-hint">
            Schedule a message in an existing Chat, or configure an advanced workspace action.
          </p>
        </div>

        <div
          v-else
          class="st-list"
        >
          <div
            v-for="task in store.tasks"
            :key="task.id"
            :class="['st-item', { disabled: !task.enabled }]"
          >
            <div class="st-item-main">
              <div class="st-item-top">
                <span
                  class="st-item-name"
                  :title="task.name"
                >{{ task.name }}</span>
                <span :class="['st-kind', `st-kind--${task.kind}`]">{{ kindLabel(task.kind) }}</span>
              </div>
              <div class="st-item-meta">
                <span class="st-schedule">{{ scheduleSummary(task) }}</span>
                <span class="st-next">
                  Next:
                  <strong>{{ task.next_run_at ? formatRelative(task.next_run_at) : '—' }}</strong>
                </span>
                <span
                  v-if="task.last_run_at"
                  class="st-last"
                >
                  Last: {{ formatRelative(task.last_run_at) }}
                  <span
                    v-if="task.last_status"
                    :class="['st-status', `st-status--${task.last_status}`]"
                  >{{ task.last_status }}</span>
                  <span
                    v-if="task.last_error"
                    class="st-error-text"
                    :title="task.last_error"
                  >⚠</span>
                </span>
                <span class="st-runs">{{ task.run_count }} run{{ task.run_count === 1 ? '' : 's' }}</span>
              </div>
              <div
                v-if="task.kind === 'chat_turn' && (task.in_flight_run_count || task.queued_run_count)"
                class="st-backlog"
              >
                <span
                  v-if="task.in_flight_run_count"
                  class="st-backlog-pill"
                  :title="`A scheduled turn has been running in the target Chat for ${flightDuration(task)}. Long runs are normal (reviews, test runs); the backend only reconciles a turn once the provider is idle. Cancel to release the queue without stopping the Chat turn.`"
                >
                  ● run in flight · {{ flightDuration(task) }}
                  <button
                    v-if="task.in_flight_run_id"
                    type="button"
                    class="st-link-btn"
                    :disabled="store.isMutating"
                    @click="onCancelRun(task)"
                  >
                    cancel
                  </button>
                </span>
                <span
                  v-if="task.queued_run_count"
                  class="st-backlog-pill st-backlog-pill--queued"
                  :title="`${task.queued_run_count} occurrence(s) waiting behind the current turn; older occurrences are superseded while the Chat is busy`"
                >
                  {{ task.queued_run_count }} queued
                  <button
                    type="button"
                    class="st-link-btn"
                    :disabled="store.isMutating"
                    @click="onClearBacklog(task)"
                  >
                    clear
                  </button>
                </span>
              </div>
            </div>
            <div class="st-item-actions">
              <label
                class="st-toggle"
                :title="task.enabled ? 'Disable' : 'Enable'"
              >
                <input
                  :checked="task.enabled"
                  type="checkbox"
                  :aria-label="`${task.enabled ? 'Disable' : 'Enable'} scheduled task ${task.name}`"
                  @change="onToggle(task, ($event.target as HTMLInputElement).checked)"
                >
                <span class="st-toggle-track" />
              </label>
              <button
                type="button"
                class="ch-btn ch-btn--sm"
                title="Run now"
                :disabled="store.isMutating"
                @click="onRunNow(task)"
              >
                Run
              </button>
              <button
                type="button"
                class="ch-btn ch-btn--sm"
                title="Edit"
                @click="startEdit(task)"
              >
                Edit
              </button>
              <button
                type="button"
                class="ch-btn ch-btn--sm ch-btn--danger"
                title="Delete"
                :disabled="store.isMutating"
                @click="onDelete(task)"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ===================== EDIT VIEW ===================== -->
      <div
        v-else
        class="st-body st-form"
      >
        <div class="form-group">
          <label for="st-name">Name</label>
          <input
            id="st-name"
            v-model="draft.name"
            type="text"
            class="ch-input"
            placeholder="e.g. Daily dependency check"
          >
        </div>

        <div class="form-group">
          <label for="st-kind">Action kind</label>
          <select
            id="st-kind"
            v-model="draft.kind"
            class="ch-select"
            :disabled="!!editingId"
          >
            <option value="chat_turn">
              Chat message — continue an existing conversation
            </option>
            <optgroup label="Advanced">
              <option value="hub_task">
                Hub task — publish a task that runs &amp; auto-cleans
              </option>
              <option value="new_session">
                New session — create a session and send a message
              </option>
              <option value="tab_message">
                Terminal message — type into a raw terminal
              </option>
            </optgroup>
          </select>
          <p class="form-hint">
            {{ kindHint(draft.kind) }}
          </p>
        </div>

        <div class="form-group">
          <label>Schedule</label>
          <div class="st-schedule-types">
            <button
              v-for="opt in scheduleTypeOptions"
              :key="opt.value"
              type="button"
              :class="['ch-btn ch-btn--sm', { 'ch-btn--primary': draft.scheduleType === opt.value }]"
              @click="draft.scheduleType = opt.value"
            >
              {{ opt.label }}
            </button>
          </div>

          <input
            v-if="draft.scheduleType === 'run_at'"
            v-model="draft.run_at"
            type="datetime-local"
            class="ch-input st-schedule-input"
          >
          <input
            v-else-if="draft.scheduleType === 'cron'"
            v-model="draft.cron"
            type="text"
            class="ch-input st-schedule-input"
            placeholder="30 9 * * *"
            spellcheck="false"
          >
          <div
            v-else
            class="st-interval-row"
          >
            <input
              v-model.number="draft.interval_seconds"
              type="number"
              min="1"
              class="ch-input st-schedule-input"
            >
            <span class="st-interval-unit">seconds</span>
            <button
              v-for="chip in intervalChips"
              :key="chip.seconds"
              type="button"
              class="ch-btn ch-btn--sm"
              :class="{ 'ch-btn--primary': draft.interval_seconds === chip.seconds }"
              @click="draft.interval_seconds = chip.seconds"
            >
              {{ chip.label }}
            </button>
          </div>
          <p
            v-if="draft.scheduleType === 'cron'"
            class="form-hint"
          >
            5-field cron: <code>minute hour day-of-month month day-of-week</code> (0 = Sunday).
            e.g. <code>*/10 * * * *</code>, <code>0 9 * * 1-5</code>.
          </p>
        </div>

        <!-- kind-specific payload -->
        <div
          v-if="draft.kind === 'chat_turn' || draft.kind === 'tab_message'"
          class="form-group"
        >
          <label for="st-tab">{{ draft.kind === 'chat_turn' ? 'Target Chat' : 'Target terminal tab' }}</label>
          <select
            id="st-tab"
            v-model="draft.tab_id"
            class="ch-select"
          >
            <option
              value=""
              disabled
            >
              {{ targetTabOptions.length === 0 ? emptyTargetLabel : selectTargetLabel }}
            </option>
            <option
              v-if="staleTab"
              :value="staleTab.value"
              disabled
            >
              {{ staleTab.label }}
            </option>
            <option
              v-for="opt in targetTabOptions"
              :key="opt.value"
              :value="opt.value"
            >
              {{ opt.label }}<template v-if="opt.detail">
                · {{ opt.detail }}
              </template>
            </option>
          </select>
          <p class="form-hint">
            {{ targetTabHint }}
          </p>
        </div>

        <div
          v-if="draft.kind === 'new_session' || draft.kind === 'hub_task'"
          class="form-group"
        >
          <label for="st-workspace">Workspace</label>
          <select
            id="st-workspace"
            v-model="draft.workspace_id"
            class="ch-select"
          >
            <option
              value=""
              disabled
            >
              Select a workspace
            </option>
            <option
              v-for="ws in workspaceStore.workspaces"
              :key="ws.id"
              :value="ws.id"
            >
              {{ ws.name }}
            </option>
          </select>
        </div>

        <div
          v-if="draft.kind === 'new_session' || draft.kind === 'hub_task'"
          class="form-group"
        >
          <label for="st-agent">Agent type</label>
          <select
            id="st-agent"
            v-model="draft.agent_type"
            class="ch-select"
          >
            <option value="claude">
              claude
            </option>
            <option value="codex">
              codex
            </option>
            <option value="cursor">
              cursor
            </option>
            <option value="terminal">
              terminal
            </option>
          </select>
        </div>

        <div
          v-if="draft.kind === 'hub_task'"
          class="form-group"
        >
          <label for="st-task-title">Task title</label>
          <input
            id="st-task-title"
            v-model="draft.task_title"
            type="text"
            class="ch-input"
            placeholder="e.g. Scheduled nightly test run"
          >
        </div>

        <div class="form-group">
          <label for="st-message">
            {{ draft.kind === 'hub_task' ? 'Task prompt' : 'Message' }}
          </label>
          <textarea
            id="st-message"
            v-model="draft.message"
            class="ch-textarea"
            :placeholder="messagePlaceholder"
            spellcheck="false"
          />
        </div>
      </div>

      <div class="st-footer">
        <template v-if="mode === 'edit'">
          <button
            type="button"
            class="ch-btn"
            @click="cancelEdit"
          >
            Cancel
          </button>
          <button
            type="button"
            class="ch-btn ch-btn--primary"
            :disabled="!canSave || store.isMutating"
            @click="save"
          >
            {{ editingId ? 'Save changes' : 'Create task' }}
          </button>
        </template>
        <template v-else>
          <span class="st-footer-hint">
            Schedules fire automatically while the Hub backend is running.
          </span>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useScheduledTasksStore } from '@/stores/scheduledTasksStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { useTerminalStore } from '@/stores/terminalStore'
import type { AgentType, ScheduledTask, ScheduledTaskCreate, ScheduledTaskKind } from '@/types'

const props = defineProps<{
  visible: boolean
  createTargetTabId?: string | null
  createRequest?: number
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const store = useScheduledTasksStore()
const workspaceStore = useWorkspaceStore()
const terminalStore = useTerminalStore()

type Mode = 'list' | 'edit'
type ScheduleType = 'run_at' | 'cron' | 'interval'

const mode = ref<Mode>('list')
const editingId = ref<string | null>(null)
const formError = ref('')

const draft = reactive({
  name: '',
  kind: 'chat_turn' as ScheduledTaskKind,
  scheduleType: 'cron' as ScheduleType,
  run_at: '',
  cron: '',
  interval_seconds: 300,
  tab_id: '',
  workspace_id: '',
  agent_type: 'claude' as AgentType,
  message: '',
  task_title: '',
})

const scheduleTypeOptions: { value: ScheduleType; label: string }[] = [
  { value: 'cron', label: 'Cron' },
  { value: 'interval', label: 'Interval' },
  { value: 'run_at', label: 'One-off' },
]

const intervalChips = [
  { label: '1m', seconds: 60 },
  { label: '5m', seconds: 300 },
  { label: '15m', seconds: 900 },
  { label: '1h', seconds: 3600 },
  { label: '1d', seconds: 86400 },
]

// Plain terminal tabs (a working directory, no belonging workspace) are the
// valid targets for a tab_message task. Managed agent sessions are excluded —
// they belong to a workspace and have their own messaging path.
const tabOptions = computed(() =>
  terminalStore.manualTabs
    .filter(tab => tab.session_kind === 'terminal')
    .map(tab => ({
    value: tab.id,
    label: tab.name?.trim() || tab.id,
    detail: tab.cwd || '',
    })),
)

const chatOptions = computed(() =>
  terminalStore.chatTabs.map(tab => ({
    value: tab.id,
    label: tab.name?.trim() || tab.id,
    detail: [tab.agent_type, tab.cwd].filter(Boolean).join(' · '),
  })),
)

const targetTabOptions = computed(() =>
  draft.kind === 'chat_turn' ? chatOptions.value : tabOptions.value,
)
const emptyTargetLabel = computed(() =>
  draft.kind === 'chat_turn' ? 'No Chat sessions yet' : 'No plain terminal tabs yet',
)
const selectTargetLabel = computed(() =>
  draft.kind === 'chat_turn' ? 'Select a Chat session' : 'Select a terminal tab',
)

// When editing a task whose target tab has since been closed, keep the stale
// id visible (disabled) so the select doesn't render blank and the user can
// switch to a live tab. Never silently re-point by name.
const staleTab = computed<{ value: string; label: string } | null>(() => {
  const id = draft.tab_id.trim()
  if (!id) return null
  if (targetTabOptions.value.some(opt => opt.value === id)) return null
  return { value: id, label: `${id} (removed)` }
})

const targetTabHint = computed(() => {
  if (draft.kind === 'chat_turn') {
    return chatOptions.value.length === 0
      ? 'Create a Chat session first, then schedule a message in that conversation.'
      : 'The message waits if this Chat is busy, then runs and displays its answer in the same conversation.'
  }
  return tabOptions.value.length === 0
    ? 'No plain terminal tabs yet.'
    : 'The message is typed into this raw terminal when the schedule fires.'
})

let pollTimer: number | undefined

watch(
  () => props.visible,
  visible => {
    if (visible) {
      void store.fetchTasks()
      // Ensure workspaces are loaded for the workspace picker.
      if (workspaceStore.workspaces.length === 0) {
        void workspaceStore.fetchWorkspaces()
      }
      // Ensure terminal tabs are loaded for the tab_message target picker.
      if (terminalStore.tabs.length === 0) {
        void terminalStore.fetchTabs()
      }
      pollTimer = window.setInterval(() => void store.fetchTasks({ silent: true }), 10000)
    } else {
      if (pollTimer !== undefined) {
        window.clearInterval(pollTimer)
        pollTimer = undefined
      }
    }
  },
)

watch(
  [() => props.visible, () => props.createRequest, () => props.createTargetTabId],
  ([visible, request, targetTabId]) => {
    if (!visible || !request || !targetTabId) return
    startCreate(targetTabId)
  },
)

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})

function handleClose() {
  emit('close')
}

// ---------------------------------------------------------------------------
// List actions
// ---------------------------------------------------------------------------

function kindLabel(kind: ScheduledTaskKind): string {
  switch (kind) {
    case 'chat_turn':
      return 'Chat'
    case 'tab_message':
      return 'Message'
    case 'new_session':
      return 'New session'
    case 'hub_task':
      return 'Hub task'
  }
}

function kindHint(kind: ScheduledTaskKind): string {
  switch (kind) {
    case 'chat_turn':
      return 'Queue a message in an existing Chat. Its answer stays in that conversation.'
    case 'tab_message':
      return 'Type a message into one of your plain terminal tabs and submit it when the schedule fires — the agent self-scheduling primitive.'
    case 'new_session':
      return 'Create a new session in a workspace and send it a message (manual one-off execution).'
    case 'hub_task':
      return 'Publish a Hub-native task on a throwaway session; it auto-DONEs and the session is deleted — no agent / reviewer resources held.'
  }
}

function scheduleSummary(task: ScheduledTask): string {
  if (task.run_at) return `once · ${formatDateTime(task.run_at)}`
  if (task.cron) return `cron · ${task.cron}`
  if (task.interval_seconds) return `every ${formatInterval(task.interval_seconds)}`
  return '—'
}

function formatInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}m`
  return `${seconds}s`
}

function formatDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatRelative(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diffMs = d.getTime() - Date.now()
  const future = diffMs > 0
  const abs = Math.abs(diffMs)
  const minutes = Math.round(abs / 60000)
  if (minutes < 1) return future ? 'in <1m' : 'just now'
  if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return future ? `in ${hours}h` : `${hours}h ago`
  const days = Math.round(hours / 24)
  return future ? `in ${days}d` : `${days}d ago`
}

async function onToggle(task: ScheduledTask, enabled: boolean) {
  try {
    await store.setEnabled(task.id, enabled)
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to update task'
  }
}

async function onRunNow(task: ScheduledTask) {
  try {
    await store.runTask(task.id)
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to run task'
  }
}

async function onDelete(task: ScheduledTask) {
  if (!window.confirm(`Delete scheduled task "${task.name}"?`)) return
  try {
    await store.deleteTask(task.id)
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to delete task'
  }
}

// Show how long the current turn has been dispatched; a long duration alone is
// not an error (legit reviews/test runs), so this is informational, not an
// alert — the backend only reconciles once the provider guard is actually down.
function flightDuration(task: ScheduledTask): string {
  if (!task.in_flight_since) return ''
  const since = Date.parse(task.in_flight_since)
  if (!Number.isFinite(since)) return ''
  const minutes = Math.max(0, Math.round((Date.now() - since) / 60000))
  if (minutes < 1) return '<1m'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}h${rest}m` : `${hours}h`
}

async function onCancelRun(task: ScheduledTask) {
  if (!task.in_flight_run_id) return
  if (
    !window.confirm(
      'Cancel this scheduled run and release the queue?\n\n'
        + 'The Chat turn itself is not interrupted (use Stop in the Chat for that). '
        + 'The next queued occurrence will dispatch once the Chat is free.',
    )
  ) {
    return
  }
  try {
    await store.cancelRun(task.in_flight_run_id)
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to cancel run'
  }
}

async function onClearBacklog(task: ScheduledTask) {
  if (!task.queued_run_count) return
  if (
    !window.confirm(
      `Cancel ${task.queued_run_count} queued occurrence(s) of "${task.name}"?\n\n`
        + 'A run currently in flight (if any) keeps going.',
    )
  ) {
    return
  }
  try {
    await store.clearBacklog(task.id)
    await store.fetchTasks({ silent: true })
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to clear queued runs'
  }
}

// ---------------------------------------------------------------------------
// Create / edit
// ---------------------------------------------------------------------------

function resetDraft() {
  draft.name = ''
  draft.kind = 'chat_turn'
  draft.scheduleType = 'cron'
  draft.run_at = ''
  draft.cron = ''
  draft.interval_seconds = 300
  draft.tab_id = ''
  draft.workspace_id = ''
  draft.agent_type = 'claude'
  draft.message = ''
  draft.task_title = ''
  formError.value = ''
}

function startCreate(targetTabId: string = '') {
  resetDraft()
  if (targetTabId) draft.tab_id = targetTabId
  editingId.value = null
  mode.value = 'edit'
}

function startEdit(task: ScheduledTask) {
  resetDraft()
  editingId.value = task.id
  draft.name = task.name
  draft.kind = task.kind
  if (task.run_at) {
    draft.scheduleType = 'run_at'
    draft.run_at = task.run_at.slice(0, 16)
  } else if (task.cron) {
    draft.scheduleType = 'cron'
    draft.cron = task.cron
  } else if (task.interval_seconds) {
    draft.scheduleType = 'interval'
    draft.interval_seconds = task.interval_seconds
  }
  draft.tab_id = task.tab_id ?? ''
  draft.workspace_id = task.workspace_id ?? ''
  draft.agent_type = task.agent_type ?? 'claude'
  draft.message = task.message ?? ''
  draft.task_title = task.task_title ?? ''
  mode.value = 'edit'
}

function cancelEdit() {
  mode.value = 'list'
  editingId.value = null
  formError.value = ''
}

const messagePlaceholder = computed(() => {
  switch (draft.kind) {
    case 'tab_message':
      return 'Message to type into the terminal tab when the schedule fires'
    case 'chat_turn':
      return 'Message to send in this Chat when the schedule fires'
    case 'new_session':
      return 'Message to send to the new session when the schedule fires'
    case 'hub_task':
      return 'The task prompt the agent will execute'
    default:
      return ''
  }
})

const canSave = computed(() => {
  if (!draft.name.trim()) return false
  if (draft.scheduleType === 'run_at' && !draft.run_at) return false
  if (draft.scheduleType === 'cron' && !draft.cron.trim()) return false
  if (draft.scheduleType === 'interval' && (!draft.interval_seconds || draft.interval_seconds < 1)) {
    return false
  }
  if (!draft.message.trim()) return false
  if ((draft.kind === 'chat_turn' || draft.kind === 'tab_message') && !draft.tab_id.trim()) return false
  if (draft.kind === 'new_session' && !draft.workspace_id) return false
  if (draft.kind === 'hub_task') {
    if (!draft.workspace_id) return false
    if (!draft.task_title.trim()) return false
  }
  return true
})

function buildPayload(): ScheduledTaskCreate {
  const payload: ScheduledTaskCreate = {
    name: draft.name.trim(),
    kind: draft.kind,
    message: draft.message,
  }
  if (draft.scheduleType === 'run_at') payload.run_at = draft.run_at
  else if (draft.scheduleType === 'cron') payload.cron = draft.cron.trim()
  else payload.interval_seconds = draft.interval_seconds

  if (draft.kind === 'chat_turn' || draft.kind === 'tab_message') {
    payload.tab_id = draft.tab_id.trim()
  } else {
    payload.workspace_id = draft.workspace_id
    payload.agent_type = draft.agent_type
  }
  if (draft.kind === 'hub_task') {
    payload.task_title = draft.task_title.trim()
  }
  return payload
}

async function save() {
  formError.value = ''
  try {
    const payload = buildPayload()
    if (editingId.value) {
      // kind is immutable on the backend; strip it from the update payload.
      const { kind: _kind, ...update } = payload
      void _kind
      await store.updateTask(editingId.value, update)
    } else {
      await store.createTask(payload)
    }
    mode.value = 'list'
    editingId.value = null
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Failed to save task'
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: var(--ch-color-overlay-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 16px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  z-index: 1100;
}

.modal {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  padding: 20px;
  width: min(760px, 100%);
  max-width: 100%;
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.st-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-shrink: 0;
}

.st-header h3 {
  margin: 0;
  color: var(--ch-color-text);
  font-size: 18px;
  font-weight: 600;
}

.st-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.st-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.st-count {
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
}

.st-empty {
  padding: 32px 16px;
  text-align: center;
  color: var(--ch-color-text-muted);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.st-empty p {
  margin: 0;
}

.st-empty-hint {
  font-size: var(--ch-font-size-sm);
  max-width: 420px;
  line-height: 1.5;
}

.st-empty.st-error {
  color: var(--ch-color-warning, #fbbf24);
}

.st-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.st-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
}

.st-item.disabled .st-item-name,
.st-item.disabled .st-schedule {
  opacity: 0.55;
}

.st-item-main {
  min-width: 0;
  flex: 1;
}

.st-item-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.st-item-name {
  font-weight: 600;
  color: var(--ch-color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.st-kind {
  flex-shrink: 0;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.st-kind--tab_message {
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
}

.st-kind--chat_turn {
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
}

.st-kind--new_session {
  background: var(--ch-color-success-bg, rgba(74, 222, 128, 0.14));
  color: var(--ch-color-success, #4ade80);
}

.st-kind--hub_task {
  background: var(--ch-color-warning-bg, rgba(251, 191, 36, 0.14));
  color: var(--ch-color-warning, #fbbf24);
}

.st-item-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
}

.st-schedule {
  font-family: var(--ch-font-mono, monospace);
  font-size: var(--ch-font-size-xs);
}

.st-next strong {
  color: var(--ch-color-text);
  font-weight: 600;
}

.st-status {
  font-weight: 600;
  text-transform: uppercase;
  font-size: var(--ch-font-size-xs);
}

.st-status--ok,
.st-status--completed {
  color: var(--ch-color-success, #4ade80);
}

.st-status--queued,
.st-status--waiting,
.st-status--dispatching,
.st-status--running {
  color: var(--ch-color-accent);
}

.st-status--error,
.st-status--failed,
.st-status--skipped,
.st-status--uncertain,
.st-status--cancelled {
  color: var(--ch-color-warning, #fbbf24);
}

.st-error-text {
  cursor: help;
}

.st-backlog {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}

.st-backlog-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-accent);
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
}

.st-backlog-pill--queued {
  color: var(--ch-color-text-secondary, var(--ch-color-text));
  background: var(--ch-color-bg-elevated, rgba(255, 255, 255, 0.08));
}

.st-link-btn {
  border: 0;
  padding: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-decoration: underline;
  cursor: pointer;
}

.st-link-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.st-item-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.st-toggle {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  margin-right: 4px;
}

.st-toggle input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
}

.st-toggle-track {
  position: relative;
  width: 34px;
  height: 19px;
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  transition: background var(--ch-motion-fast, 120ms), border-color var(--ch-motion-fast, 120ms);
}

.st-toggle-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: var(--ch-color-text-muted);
  transition: transform var(--ch-motion-fast, 120ms), background var(--ch-motion-fast, 120ms);
}

.st-toggle input:checked + .st-toggle-track {
  background: var(--ch-color-accent-soft);
  border-color: var(--ch-color-accent);
}

.st-toggle input:checked + .st-toggle-track::after {
  transform: translateX(15px);
  background: var(--ch-color-accent);
}

.st-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-right: 4px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  color: var(--ch-color-text);
}

.form-hint {
  margin: 0;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
  line-height: 1.5;
}

.form-hint code {
  font-family: var(--ch-font-mono, monospace);
  background: var(--ch-color-surface-control);
  padding: 1px 4px;
  border-radius: 3px;
}

.st-schedule-types {
  display: flex;
  gap: 6px;
}

.st-schedule-input {
  max-width: 320px;
}

.st-interval-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.st-interval-row .st-schedule-input {
  max-width: 120px;
}

.st-interval-unit {
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
}

.st-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-warning-bg, rgba(251, 191, 36, 0.14));
  border: 1px solid var(--ch-color-warning, #fbbf24);
  color: var(--ch-color-warning, #fbbf24);
  font-size: var(--ch-font-size-sm);
  flex-shrink: 0;
}

.st-banner-text {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.st-banner-close {
  flex-shrink: 0;
  background: none;
  border: none;
  color: inherit;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}

.st-banner-close:hover {
  opacity: 0.8;
}

.st-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--ch-color-border);
  flex-shrink: 0;
}

.st-footer-hint {
  margin-right: auto;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}
</style>
