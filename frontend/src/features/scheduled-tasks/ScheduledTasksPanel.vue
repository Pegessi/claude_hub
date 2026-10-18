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
                  @change="handleTaskEnabledChange(task, $event)"
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
          v-if="draft.kind === 'new_session' || draft.kind === 'hub_task'"
          class="form-group"
        >
          <label for="st-env-preset">Env preset</label>
          <select
            id="st-env-preset"
            v-model="draft.env_preset"
            class="ch-select"
          >
            <option value="">
              Agent default
            </option>
            <option
              v-for="preset in envPresetOptions"
              :key="preset.id"
              :value="preset.id"
            >
              {{ preset.name }}
            </option>
            <option
              v-if="missingEnvPreset"
              :value="draft.env_preset"
              disabled
            >
              {{ draft.env_preset }} (no longer available)
            </option>
          </select>
          <p class="form-hint">
            Launch environment for the worker session created on each fire.
          </p>
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
import { useScheduledTasksPanel, type ScheduledTasksPanelProps } from './useScheduledTasksPanel'

const props = defineProps<ScheduledTasksPanelProps>()
const emit = defineEmits<{ close: [] }>()
const {
  store, workspaceStore, envPresetOptions, missingEnvPreset, mode, editingId, formError,
  draft, scheduleTypeOptions, intervalChips, targetTabOptions, emptyTargetLabel,
  selectTargetLabel, staleTab, targetTabHint, handleClose, kindLabel, kindHint,
  scheduleSummary, formatRelative, handleTaskEnabledChange, onRunNow, onDelete,
  flightDuration, onCancelRun, onClearBacklog, startCreate, startEdit, cancelEdit,
  messagePlaceholder, canSave, save,
} = useScheduledTasksPanel(props, { close: () => emit('close') })
</script>

<style scoped src="./scheduled-tasks-panel.css"></style>
