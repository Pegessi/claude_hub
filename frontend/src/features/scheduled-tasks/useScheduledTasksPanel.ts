import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useScheduledTasksStore } from '@/stores/scheduledTasksStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import type { AgentType, ScheduledTask, ScheduledTaskCreate, ScheduledTaskKind } from '@/types'

export interface ScheduledTasksPanelProps {
  visible: boolean
  createTargetTabId?: string | null
  createRequest?: number
}

export interface ScheduledTasksPanelEvents {
  close: () => void
}

export function useScheduledTasksPanel(
  props: Readonly<ScheduledTasksPanelProps>,
  events: ScheduledTasksPanelEvents,
) {
  const store = useScheduledTasksStore()
  const workspaceStore = useWorkspaceStore()
  const terminalStore = useTerminalStore()
  const { envPresets, ensureLoaded } = useLaunchEnvPresets()

  const envPresetOptions = computed(() => envPresets.value.filter(preset => preset.id !== 'none'))

  // Schedules created via CLI may store a preset name while the UI options use
  // ids; resolve either. The built-in 'none' preset is represented by the empty
  // "Agent default" option. A stored value matching neither is shown as a
  // disabled "(no longer available)" option rather than silently blanked.
  function resolveEnvPresetId(stored: string): string {
    if (!stored || stored === 'none') return ''
    const preset = envPresets.value.find(p => p.id === stored || p.name === stored)
    if (!preset) return stored
    return preset.id === 'none' ? '' : preset.id
  }

  const missingEnvPreset = computed(
    () => !!draft.env_preset && !envPresetOptions.value.some(p => p.id === draft.env_preset),
  )

  // Presets can finish loading after the edit modal opened (or the stored value
  // is a CLI-supplied name); re-map name -> id once the options arrive so a
  // valid preset is not stuck showing "(no longer available)".
  watch(envPresetOptions, () => {
    if (!draft.env_preset || mode.value !== 'edit') return
    const match = envPresetOptions.value.find(
      p => p.id === draft.env_preset || p.name === draft.env_preset,
    )
    if (match) draft.env_preset = match.id
  })

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
    env_preset: '',
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
        void ensureLoaded()
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
    events.close()
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


  function handleTaskEnabledChange(task: ScheduledTask, event: Event) {
    const input = event.currentTarget
    if (input instanceof HTMLInputElement) void onToggle(task, input.checked)
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
    draft.env_preset = ''
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
    draft.env_preset = resolveEnvPresetId(task.env_preset ?? '')
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
      if (editingId.value) {
        // Send an explicit null when "Agent default" is chosen while editing so
        // a previously configured preset can be cleared.
        payload.env_preset = draft.env_preset || null
      } else if (draft.env_preset) {
        payload.env_preset = draft.env_preset
      }
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

    return {
      store,
      workspaceStore,
      envPresetOptions,
      missingEnvPreset,
      mode,
      editingId,
      formError,
      draft,
      scheduleTypeOptions,
      intervalChips,
      targetTabOptions,
      emptyTargetLabel,
      selectTargetLabel,
      staleTab,
      targetTabHint,
      handleClose,
      kindLabel,
      kindHint,
      scheduleSummary,
      formatRelative,
      handleTaskEnabledChange,
      onRunNow,
      onDelete,
      flightDuration,
      onCancelRun,
      onClearBacklog,
      startCreate,
      startEdit,
      cancelEdit,
      messagePlaceholder,
      canSave,
      save
    }
}
