<template>
  <section
    class="task-execution-panel"
    aria-label="Task execution and progress"
  >
    <h3>Execution and progress</h3>
    <dl class="task-execution-facts">
      <div><dt>Source</dt><dd>{{ taskSourceLabel(task.source) }}</dd></div>
      <div><dt>Execution</dt><dd>{{ taskExecutionLabel(task) }}</dd></div>
      <div><dt>Epoch</dt><dd>{{ task.execution_epoch ?? 'legacy' }}</dd></div>
      <div><dt>Progress revision</dt><dd>{{ task.progress_revision ?? 'legacy' }}</dd></div>
      <div v-if="task.source?.tab_id">
        <dt>Source Chat</dt><dd><code>{{ task.source.tab_id }}</code></dd>
      </div>
      <div v-if="task.source?.agent_id">
        <dt>Source Agent</dt><dd><code>{{ task.source.agent_id }}</code></dd>
      </div>
    </dl>

    <div
      v-if="task.latest_progress"
      class="task-execution-block"
    >
      <strong>{{ progressStateLabel(task.latest_progress.state) }}</strong>
      <p>{{ task.latest_progress.summary }}</p>
      <small>{{ formatTime(task.latest_progress.reported_at) }}</small>
      <details v-if="task.latest_progress.validation || task.latest_progress.risks || task.latest_progress.artifact_refs.length">
        <summary>Validation, risks, and artifacts</summary>
        <p v-if="task.latest_progress.validation">
          <b>Validation:</b> {{ task.latest_progress.validation }}
        </p>
        <p v-if="task.latest_progress.risks">
          <b>Risks:</b> {{ task.latest_progress.risks }}
        </p>
        <ul v-if="task.latest_progress.artifact_refs.length">
          <li
            v-for="artifact in task.latest_progress.artifact_refs"
            :key="artifact"
          >
            <code>{{ artifact }}</code>
          </li>
        </ul>
      </details>
    </div>
    <p
      v-else
      class="task-execution-muted"
    >
      No progress has been reported.
    </p>

    <div class="task-execution-block">
      <strong>Runtime activity</strong>
      <p v-if="task.runtime_observation">
        {{ runtimeLabel }} · {{ formatTime(task.runtime_observation.observed_at) }}
        <span v-if="task.runtime_observation.detail"> — {{ task.runtime_observation.detail }}</span>
      </p>
      <p v-else>
        Unknown
      </p>
      <small>Runtime activity does not change the Task’s final state.</small>
    </div>

    <details
      v-if="task.execution_ref"
      class="task-execution-block"
    >
      <summary>Execution reference</summary>
      <dl class="task-execution-facts">
        <div><dt>Provider</dt><dd>{{ task.execution_ref.provider || 'none' }}</dd></div>
        <div><dt>Session</dt><dd>{{ task.execution_ref.session_id || 'none' }}</dd></div>
        <div><dt>Thread</dt><dd>{{ task.execution_ref.thread_id || 'none' }}</dd></div>
        <div><dt>Turn</dt><dd>{{ task.execution_ref.turn_id || 'none' }}</dd></div>
        <div><dt>Run epoch</dt><dd>{{ task.execution_ref.run_epoch ?? 'none' }}</dd></div>
      </dl>
    </details>

    <details
      v-if="task.source_work_id"
      class="task-execution-block"
    >
      <summary>Legacy Chat work</summary>
      <dl class="task-execution-facts">
        <div><dt>Work ID</dt><dd><code>{{ task.source_work_id }}</code></dd></div>
        <div><dt>Detached</dt><dd>{{ task.legacy_work_detached ? 'Yes' : 'No' }}</dd></div>
        <div v-if="task.chat_work_outcome">
          <dt>Outcome</dt><dd>{{ task.chat_work_outcome }}</dd>
        </div>
        <div v-if="task.chat_work_report_id">
          <dt>Report</dt><dd><code>{{ task.chat_work_report_id }}</code></dd>
        </div>
      </dl>
      <p v-if="task.chat_work_summary">
        {{ task.chat_work_summary }}
      </p>
    </details>

    <p
      v-if="recordOnly && !hasMutationVersion"
      class="task-execution-muted"
    >
      Legacy Task: progress and handoff are unavailable.
    </p>
    <p
      v-if="terminalRecord"
      class="task-execution-muted"
    >
      This record is {{ task.status === 'done' ? 'Done' : 'Failed' }} and is now read-only.
      Manual completion does not release the original executor.
    </p>

    <section
      v-if="progressRecovery && progressAttempt"
      class="task-execution-form"
    >
      <h4>Confirm prior progress update</h4>
      <p>{{ progressStateLabel(progressAttempt.request.state) }} · {{ progressAttempt.request.summary }}</p>
      <p class="task-execution-warning">
        This resends the exact saved call ID and does not create a different progress update.
      </p>
      <div class="task-execution-actions">
        <button
          type="button"
          :disabled="busy"
          @click="submitProgress"
        >
          Confirm prior update
        </button>
        <button
          type="button"
          :disabled="busy"
          @click="discardProgressAttempt"
        >
          Discard saved update
        </button>
      </div>
    </section>
    <form
      v-else-if="recordOnly && hasMutationVersion && !terminalRecord"
      class="task-execution-form"
      @submit.prevent="submitProgress"
    >
      <h4>Update progress</h4>
      <p
        v-if="!capabilities"
        class="task-execution-muted"
      >
        Task capabilities are still loading.
      </p>
      <label>State
        <select
          v-model="progressDraft.state"
          :disabled="busy || !manualStates.length"
          @change="discardProgressAttempt"
        >
          <option
            v-for="state in manualStates"
            :key="state"
            :value="state"
          >{{ progressStateLabel(state) }}</option>
        </select>
      </label>
      <label>Summary
        <textarea
          v-model="progressDraft.summary"
          required
          maxlength="2000"
          :disabled="busy"
          @input="discardProgressAttempt"
        />
      </label>
      <label>Validation
        <textarea
          v-model="progressDraft.validation"
          maxlength="4000"
          :disabled="busy"
          @input="discardProgressAttempt"
        />
      </label>
      <label>Risks
        <textarea
          v-model="progressDraft.risks"
          maxlength="2000"
          :disabled="busy"
          @input="discardProgressAttempt"
        />
      </label>
      <label>Artifact references
        <textarea
          v-model="progressDraft.artifactRefs"
          :disabled="busy"
          placeholder="One reference per line"
          @input="discardProgressAttempt"
        />
      </label>
      <p
        v-if="progressValidationError"
        class="task-execution-error"
        role="alert"
      >
        {{ progressValidationError }}
      </p>
      <div class="task-execution-actions">
        <button
          type="submit"
          :disabled="busy || !canSubmitManualProgress"
        >
          {{ progressAttempt ? 'Retry same update' : 'Save progress' }}
        </button>
        <button
          v-if="progressAttempt"
          type="button"
          :disabled="busy"
          @click="discardProgressAttempt"
        >
          Discard pending retry
        </button>
      </div>
      <small>Done or Failed updates this record only. It does not stop the source Agent or sub-agent, and it does not release execution for handoff.</small>
    </form>

    <section
      v-if="hasMutationVersion"
      class="task-execution-block"
    >
      <h4>Execution handoff</h4>
      <template v-if="handoffTarget">
        <p
          v-if="handoffRecovery"
          class="task-execution-warning"
        >
          A prior handoff needs confirmation. This reuses the exact saved request and does not create a new handoff.
        </p>
        <p v-else-if="handoffTarget === 'workspace' && !task.execution_released">
          Waiting for the initiator to report Released. There is no force takeover.
        </p>
        <p v-else-if="handoffTarget === 'workspace'">
          Move this released Task to Workspace control. It will return to Todo and will not start automatically.
        </p>
        <p v-else>
          Move this Task to initiator control. The server requires every worker, reviewer, and dispatch to be idle.
        </p>
        <div class="task-execution-actions">
          <button
            type="button"
            :disabled="busy || !canHandoff"
            @click="submitHandoff"
          >
            {{ handoffRecovery ? 'Confirm prior handoff' : handoffAttempt ? 'Retry same handoff' : handoffTarget === 'workspace' ? 'Hand off to Workspace' : 'Hand off to initiator' }}
          </button>
          <button
            v-if="handoffAttempt"
            type="button"
            :disabled="busy"
            @click="discardHandoffAttempt"
          >
            Discard pending retry
          </button>
        </div>
      </template>
      <p
        v-else
        class="task-execution-muted"
      >
        No other execution arrangement is supported.
      </p>
    </section>

    <details
      v-if="reporterKey"
      class="task-execution-block"
    >
      <summary>External reporter credential · epoch {{ reporterKeyEpoch }}</summary>
      <p
        v-if="reporterKeyEpoch !== task.execution_epoch"
        class="task-execution-warning"
      >
        This credential belongs to the confirmed receipt epoch. Refresh the Task before using it.
      </p>
      <p>This credential controls progress, completion, and release reporting. Share it only with the actual executor.</p>
      <code
        v-if="revealReporterKey"
        class="task-reporter-key"
      >{{ reporterKey }}</code>
      <div class="task-execution-actions">
        <button
          type="button"
          @click="revealReporterKey = !revealReporterKey"
        >
          {{ revealReporterKey ? 'Hide' : 'Show' }}
        </button>
        <button
          type="button"
          @click="copyReporterKey"
        >
          Copy
        </button>
      </div>
    </details>

    <p
      v-if="success"
      role="status"
      class="task-execution-success"
    >
      {{ success }}
    </p>
    <p
      v-if="error"
      role="alert"
      class="task-execution-error"
    >
      {{ error }}
    </p>
    <p
      v-if="refreshWarning"
      role="status"
      class="task-execution-warning"
    >
      {{ refreshWarning }}
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type {
  WorkspaceTask,
  WorkspaceTaskCapabilities,
  WorkspaceTaskHandoffRequest,
} from '@/types'
import {
  generateTaskCallId,
  generateTaskReporterKey,
  isWorkspaceControlledTask,
  loadTaskHandoffAttempt,
  loadTaskProgressAttempt,
  loadTaskReporterKey,
  manualProgressStates,
  progressStateLabel,
  removeTaskHandoffAttempt,
  removeTaskProgressAttempt,
  removeTaskReporterKey,
  saveTaskHandoffAttempt,
  saveTaskProgressAttempt,
  saveTaskReporterKey,
  taskExecutionLabel,
  taskSourceLabel,
  type ManualTaskProgressState,
  type StoredTaskHandoffAttempt,
  type StoredTaskProgressAttempt,
} from '@/utils/taskExecution'
import { writeClipboard } from '@/utils/clipboard'

const props = defineProps<{
  workspaceId: string
  task: WorkspaceTask
  capabilities: WorkspaceTaskCapabilities | null
}>()

const store = useWorkspaceStore()
const busy = ref(false)
const error = ref<string | null>(null)
const success = ref<string | null>(null)
const refreshWarning = ref<string | null>(null)
const revealReporterKey = ref(false)
const reporterKey = ref<string | null>(loadTaskReporterKey(props.workspaceId, props.task))
const progressAttempt = ref<StoredTaskProgressAttempt | null>(null)
const progressRecovery = ref(false)
const confirmedHandoffCallIds = new Set<string>()
let activeOperationId = 0
const handoffAttempt = ref<StoredTaskHandoffAttempt | null>(null)
const handoffRecovery = ref(false)
let disposed = false
const reporterKeyEpoch = ref<number | null>(reporterKey.value ? props.task.execution_epoch ?? null : null)

function isCurrentTarget(workspaceId: string, taskId: string): boolean {
  return !disposed && props.workspaceId === workspaceId && props.task.id === taskId
}

function handoffReceiptKey(attempt: StoredTaskHandoffAttempt): string {
  return JSON.stringify([attempt.workspace_id, attempt.task_id, attempt.request.call_id])
}

const progressDraft = reactive({
  state: 'working' as ManualTaskProgressState,
  summary: '',
  validation: '',
  risks: '',
  artifactRefs: '',
})

const recordOnly = computed(() => !isWorkspaceControlledTask(props.task))
const manualStates = computed(() => manualProgressStates(props.capabilities))
const hasMutationVersion = computed(() =>
  Number.isInteger(props.task.execution_epoch) &&
  (props.task.execution_epoch ?? 0) >= 1 &&
  Number.isInteger(props.task.progress_revision) &&
  (props.task.progress_revision ?? -1) >= 0,
)
const terminalRecord = computed(() =>
  recordOnly.value && ['done', 'failed'].includes(props.task.status),
)
const runtimeLabel = computed(() => {
  const status = props.task.runtime_observation?.status ?? 'unknown'
  return status.charAt(0).toUpperCase() + status.slice(1)
})
const artifactRefs = computed(() =>
  progressDraft.artifactRefs
    .split('\n')
    .map(value => value.trim())
    .filter(Boolean),
)
const progressValidationError = computed(() => {
  const summary = progressDraft.summary.trim()
  const validation = progressDraft.validation.trim()
  const risks = progressDraft.risks.trim()
  if (!summary) return 'Summary is required.'
  if (summary.length > 2_000) return 'Summary must be 2,000 characters or fewer.'
  if (validation.length > 4_000) return 'Validation must be 4,000 characters or fewer.'
  if (risks.length > 2_000) return 'Risks must be 2,000 characters or fewer.'
  if (artifactRefs.value.length > 32) return 'Use at most 32 artifact references.'
  if (artifactRefs.value.some(value => value.length > 2_048)) {
    return 'Each artifact reference must be 2,048 characters or fewer.'
  }
  return null
})
const canSubmitManualProgress = computed(() =>
  recordOnly.value &&
  hasMutationVersion.value &&
  !terminalRecord.value &&
  manualStates.value.length > 0 &&
  progressValidationError.value === null,
)
const handoffTarget = computed(() => {
  if (handoffAttempt.value) return handoffAttempt.value.request.execution_control
  const target = recordOnly.value ? 'workspace' : 'initiator'
  return props.capabilities?.supported_execution_controls.includes(target) ? target : null
})
const canHandoff = computed(() => {
  if (handoffAttempt.value) return true // Replay the saved request without creating another handoff.
  return Boolean(
    hasMutationVersion.value &&
    handoffTarget.value &&
    (handoffTarget.value !== 'workspace' || props.task.execution_released)
  )
})

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function clearMessages(): void {
  error.value = null
  success.value = null
  refreshWarning.value = null
}

function discardProgressAttempt():void{
  if(busy.value)return
  const attempt=progressAttempt.value
  if(attempt)removeTaskProgressAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
  progressAttempt.value=null
  progressRecovery.value=false
  error.value=null
}

function discardHandoffAttempt(): void {
  if (busy.value) return
  const attempt = handoffAttempt.value
  if (attempt) removeTaskHandoffAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
  handoffAttempt.value = null
  handoffRecovery.value = false
  error.value = null
}

async function refreshAfterSuccess(
  workspaceId:string,
  taskId:string,
  operation:number,
):Promise<void>{
  try{await store.fetchBoard(workspaceId)}catch{
    if(isCurrentTarget(workspaceId,taskId)&&operation===activeOperationId){
      refreshWarning.value='The update was saved, but the board could not be refreshed. Reload the board to see the latest state.'
    }
  }
}



function reconcileCurrentPending():void{
  const currentKey = loadTaskReporterKey(props.workspaceId, props.task)
  const currentEpoch = props.task.execution_epoch ?? null
  if (currentKey) {
    reporterKey.value = currentKey
    reporterKeyEpoch.value = currentEpoch
  } else if (!(reporterKey.value && reporterKeyEpoch.value !== null &&
      currentEpoch !== null && reporterKeyEpoch.value > currentEpoch)) {
    reporterKey.value = null
    reporterKeyEpoch.value = null
  }
  revealReporterKey.value = false

  const progress=loadTaskProgressAttempt(props.workspaceId,props.task.id)
  progressAttempt.value=progress
  progressRecovery.value=Boolean(progress)

  const handoff=loadTaskHandoffAttempt(props.workspaceId,props.task.id)
  if(handoff&&confirmedHandoffCallIds.has(handoffReceiptKey(handoff))){
    handoffAttempt.value=null
    handoffRecovery.value=false
  }else{
    handoffAttempt.value=handoff
    handoffRecovery.value=Boolean(handoff)
  }
}

function resetForTaskIdentity():void{
  activeOperationId += 1
  reporterKey.value = null
  reporterKeyEpoch.value = null
  busy.value=false
  progressDraft.state='working'
  progressDraft.summary=''
  progressDraft.validation=''
  progressDraft.risks=''
  progressDraft.artifactRefs=''
  error.value=null
  success.value=null
  refreshWarning.value=null
  reconcileCurrentPending()
}

async function submitProgress():Promise<void>{
  if(busy.value)return
  let attempt=progressAttempt.value
  const wasRecovery=progressRecovery.value
  if(!attempt){
    if(!canSubmitManualProgress.value)return
    attempt={
      workspace_id:props.workspaceId,
      task_id:props.task.id,
      request:{
        call_id:generateTaskCallId(),
        expected_execution_epoch:props.task.execution_epoch!,
        expected_progress_revision:props.task.progress_revision!,
        state:progressDraft.state,
        summary:progressDraft.summary.trim(),
        validation:progressDraft.validation.trim()||null,
        risks:progressDraft.risks.trim()||null,
        artifact_refs:artifactRefs.value,
      },
    }
    try{saveTaskProgressAttempt(attempt)}catch{
      error.value='This progress update could not be staged safely. No request was sent.'
      return
    }
    progressAttempt.value=attempt
    progressRecovery.value=false
  }

  const operation=++activeOperationId
  busy.value=true
  try{
    const result=await store.reportTaskProgressManually(attempt.workspace_id,attempt.task_id,attempt.request)
    removeTaskProgressAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
    await refreshAfterSuccess(attempt.workspace_id, attempt.task_id, operation)
    if(isCurrentTarget(attempt.workspace_id,attempt.task_id)&&operation===activeOperationId){
      progressAttempt.value=null
      progressRecovery.value=false
      progressDraft.summary=''
      progressDraft.validation=''
      progressDraft.risks=''
      progressDraft.artifactRefs=''
      success.value=result.replayed?'The existing progress update was confirmed.':'Progress saved.'
    }
  }catch{
    if(isCurrentTarget(attempt.workspace_id,attempt.task_id)&&operation===activeOperationId){
      error.value=wasRecovery
        ?'The prior progress update could not be confirmed. Refresh the Task, then confirm again or discard the saved request.'
        :'Progress was not confirmed. Retry the same update, or edit the form to create a new update.'
    }
  }finally{
    if(!disposed && operation===activeOperationId){
      busy.value=false
      reconcileCurrentPending()
    }
  }
}

async function submitHandoff(): Promise<void> {
  if (busy.value) return
  const existing = handoffAttempt.value
  const target = existing?.request.execution_control ?? handoffTarget.value
  if (!existing && (!hasMutationVersion.value || !target || !canHandoff.value)) return
  clearMessages()

  let attempt = existing
  if (!attempt) {
    if (!target) return
    const key = target === 'initiator' ? generateTaskReporterKey() : undefined
    const request: WorkspaceTaskHandoffRequest = {
      call_id: generateTaskCallId(),
      expected_execution_epoch: props.task.execution_epoch!,
      expected_progress_revision: props.task.progress_revision!,
      execution_control: target,
      ...(key ? { new_reporter_key: key } : {}),
    }
    attempt = {
      workspace_id: props.workspaceId,
      task_id: props.task.id,
      source: props.task.source ? { ...props.task.source } : null,
      request,
    }
    try {
      saveTaskHandoffAttempt(attempt)
    } catch {
      error.value = 'This handoff could not be staged safely for retry. No request was sent.'
      return
    }
    handoffAttempt.value = attempt
    handoffRecovery.value = false
  }

  const wasRecovery = handoffRecovery.value
  const operation = ++activeOperationId
  busy.value = true
  try {
    const result = await store.handoffTask(
      attempt.workspace_id,
      attempt.task_id,
      attempt.request,
    )
    const expectedEventCallId =
      `task-execution:${attempt.task_id}:${attempt.request.call_id}`
    const receiptMatches =
      result.event.call_id === expectedEventCallId &&
      result.event.task_id === attempt.task_id
    const targetReached =
      receiptMatches &&
      result.task.id === attempt.task_id &&
      result.task.workspace_id === attempt.workspace_id &&
      result.task.execution_control === attempt.request.execution_control &&
      result.task.execution_epoch ===
        attempt.request.expected_execution_epoch + 1

    if (!targetReached) {
      removeTaskHandoffAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
      await refreshAfterSuccess(attempt.workspace_id, attempt.task_id, operation)
      if (isCurrentTarget(attempt.workspace_id, attempt.task_id) &&
          operation === activeOperationId) {
        handoffAttempt.value = null
        handoffRecovery.value = false
        error.value = 'The handoff receipt or execution epoch did not match. The saved reporter credential was not used.'
      }
      return
    }

    confirmedHandoffCallIds.add(handoffReceiptKey(attempt))
    if (attempt.request.execution_control === 'initiator') {
      const key = attempt.request.new_reporter_key!
      const persisted = saveTaskReporterKey(
        attempt.workspace_id,
        result.task,
        key,
      )
      if (persisted) {
        removeTaskHandoffAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
      }
      if (isCurrentTarget(attempt.workspace_id, attempt.task_id) &&
          operation === activeOperationId) {
        if ((props.task.execution_epoch ?? 0) <= result.task.execution_epoch!) {
          reporterKey.value = key
          reporterKeyEpoch.value = result.task.execution_epoch!
        }
        if (!persisted) {
          refreshWarning.value = 'The handoff succeeded, but the reporter credential is only available in this browser session. Copy it before leaving.'
        }
      }
    } else {
      removeTaskReporterKey(attempt.workspace_id, {
        id: attempt.task_id,
        source: attempt.source,
        execution_epoch: attempt.request.expected_execution_epoch,
      })
      removeTaskHandoffAttempt(attempt.workspace_id, attempt.task_id, { expectedCallId: attempt.request.call_id })
      if (isCurrentTarget(attempt.workspace_id, attempt.task_id) &&
          operation === activeOperationId) {
        if ((reporterKeyEpoch.value ?? 0) <= result.task.execution_epoch!) {
          reporterKey.value = null
          reporterKeyEpoch.value = null
        }
      }
    }

    await refreshAfterSuccess(attempt.workspace_id, attempt.task_id, operation)
    if (isCurrentTarget(attempt.workspace_id, attempt.task_id) &&
        operation === activeOperationId) {
      handoffAttempt.value = null
      handoffRecovery.value = false
      success.value = result.replayed
        ? 'The existing handoff was confirmed.'
        : 'Execution responsibility updated. The Task was not started.'
    }
  } catch {
    if (isCurrentTarget(attempt.workspace_id, attempt.task_id) &&
        operation === activeOperationId) {
      error.value = wasRecovery
        ? 'The prior handoff could not be confirmed. No reporter credential was saved. Refresh the Task, then confirm again or discard the saved request.'
        : 'The handoff was not confirmed. Retry the same handoff, or discard it after checking the latest Task state.'
    }
  } finally {
    if (!disposed && operation === activeOperationId) {
      busy.value = false
      reconcileCurrentPending()
    }
  }
}

async function copyReporterKey(): Promise<void> {
  if (!reporterKey.value) return
  try {
    await writeClipboard(reporterKey.value)
    success.value = 'Reporter credential copied.'
  } catch {
    error.value = 'Could not copy the reporter credential. Show it and copy it manually.'
  }
}

watch(
  [() => props.workspaceId, () => props.task.id],
  resetForTaskIdentity,
  { immediate: true },
)
watch(
  [() => props.task.execution_epoch, () => props.task.progress_revision, () => props.task.execution_control],
  () => { if (!busy.value) reconcileCurrentPending() },
)

watch(
  manualStates,
  states => {
    if (states.length && !states.includes(progressDraft.state)) {
      progressDraft.state = states[0]
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  disposed = true
  activeOperationId += 1
})
</script>

<style scoped>
.task-execution-panel,
.task-execution-block,
.task-execution-form {
  display: grid;
  gap: 10px;
}
.task-execution-panel {
  padding: 14px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
}
h3,
h4,
p { margin: 0; }
.task-execution-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}
.task-execution-facts div { min-width: 0; }
.task-execution-facts dt,
small,
.task-execution-muted { color: var(--ch-color-text-muted); }
.task-execution-facts dd {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
}
.task-execution-block {
  padding-top: 10px;
  border-top: 1px solid var(--ch-color-border-muted);
}
.task-execution-form label {
  display: grid;
  gap: 4px;
}
.task-execution-form textarea {
  min-height: 72px;
  resize: vertical;
}
.task-execution-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.task-reporter-key {
  padding: 8px;
  overflow-wrap: anywhere;
  background: var(--ch-color-surface-control);
}
.task-execution-success { color: var(--ch-color-success); }
.task-execution-error { color: var(--ch-color-danger); }
.task-execution-warning { color: var(--ch-color-warning); }
@media (max-width: 640px) {
  .task-execution-facts { grid-template-columns: 1fr; }
  .task-execution-actions button { min-height: 44px; }
}
</style>
