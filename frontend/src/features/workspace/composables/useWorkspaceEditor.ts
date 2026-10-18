import {
  ref,
  reactive,
  computed,
  watch,
} from 'vue'
import {
  type RemoteProfile,
  type ExecutionTarget,
  type ResidentPeriodicTask,
  type AgentType,
  type WorkspaceUpdate,
  type AgentReport,
  type ManagedSession,
} from '@/types'
import {
  defaultLaunchEnvPresetForAgent,
  parseLaunchEnv,
  serializeLaunchEnv,
} from '@/composables/useLaunchEnvPresets'
import {
  sanitizePeriodicTasks,
  residentUpdatePayload,
  enabledPeriodicTaskCount as projectEnabledPeriodicTaskCount,
  residentLastRunLabel as projectResidentLastRunLabel,
  residentNextRunLabel as projectResidentNextRunLabel,
  latestResidentReport as projectLatestResidentReport,
} from '@/features/workspace/presentation/resident'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceEditorInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'defaultPresetTextForAgent'
  | 'workspaceStore'
  | 'runPending'
  | 'selectedWorkspaceId'
  | 'activeWorkspace'
  | 'activeWorkspaceId'
  | 'elapsedClockMs'
>

export function useWorkspaceEditor(inputs: WorkspaceEditorInputs) {
  const {
    defaultPresetTextForAgent,
    workspaceStore,
    runPending,
    selectedWorkspaceId,
    activeWorkspace,
    activeWorkspaceId,
    elapsedClockMs,
  } = inputs

  const showWorkspaceModal = ref(false)

  const showResidentAgentModal = ref(false)

  const workspaceModalMode = ref<'create' | 'edit'>('create')

  const editingWorkspaceId = ref<string | null>(null)

  const remoteProfiles = ref<RemoteProfile[]>([])

  const remoteProfilesLoading = ref(false)

  const workspaceForm = reactive({
    name: 'Claude Hub',
    path: '',
    default_branch: 'main',
    session_prefix: 'chub',
    target: 'local' as ExecutionTarget,
    remote_profile_id: '',
    remote_cwd: '',
    remote_reconnect: true,
    resident_agent_enabled: false,
    resident_agent_paused: false,
    resident_agent_master_mode: false,
    resident_agent_interval_minutes: 60,
    resident_agent_directive: '',
    resident_agent_periodic_tasks: [] as ResidentPeriodicTask[],
    // Resident agent CLI/env config (UI-side; resolved to resident_agent_env at submit).
    resident_agent_type: 'claude' as AgentType,
    resident_agent_solo_mode: true,
    resident_env_preset: defaultLaunchEnvPresetForAgent('claude'),
    resident_env_text: defaultPresetTextForAgent('claude'),
    // Resident agent launch placement (mirrors the Add-Agent form).
    resident_agent_title: '',
    resident_agent_target: 'local' as ExecutionTarget,
    resident_agent_remote_profile_id: '',
    resident_agent_cwd: '',
    resident_agent_remote_reconnect: true,
  })

  const selectedRemoteProfile = computed(() =>
    remoteProfiles.value.find(profile => profile.id === workspaceForm.remote_profile_id) || null
  )

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
      // (F5) error is now a computed; push to the notification queue instead.
      workspaceStore.pushNotification({
        type: 'error',
        message: e instanceof Error ? e.message : 'Failed to load remote profiles',
        autoDismissMs: 8000,
      })
    } finally {
      remoteProfilesLoading.value = false
    }
  }

  async function handleSubmitWorkspace() {
    if (workspaceModalMode.value === 'edit') {
      return handleSaveWorkspace()
    }
    return handleCreateWorkspace()
  }

  async function handleCreateWorkspace() {
    const workspace = await runPending('workspace:create', () =>
      workspaceStore.createWorkspace({
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
        // NOTE: create-mode inlines the resident_agent_* fields rather than
        // spreading buildResidentPayload() because that helper returns a
        // WorkspaceUpdate, whose resident_agent_title / resident_agent_cwd are
        // `string | null`. WorkspaceCreate types those as `string | undefined`
        // (null not allowed), so the spread fails vue-tsc. For create, `undefined`
        // is the correct "omit" value — the backend applies its defaults — so the
        // `|| undefined` here is intentional and not a divergence bug.
        resident_agent_enabled: workspaceForm.resident_agent_enabled,
        resident_agent_paused: workspaceForm.resident_agent_paused,
        resident_agent_master_mode: workspaceForm.resident_agent_master_mode,
        resident_agent_interval_minutes: workspaceForm.resident_agent_interval_minutes,
        resident_agent_directive: workspaceForm.resident_agent_directive.trim() || undefined,
        resident_agent_periodic_tasks: sanitizePeriodicTasks(
          workspaceForm.resident_agent_periodic_tasks,
        ),
        resident_agent_type: workspaceForm.resident_agent_type,
        resident_agent_env: parseLaunchEnv(workspaceForm.resident_env_text) ?? {},
        resident_agent_solo_mode: workspaceForm.resident_agent_solo_mode,
        resident_agent_title: workspaceForm.resident_agent_title.trim() || undefined,
        resident_agent_target: workspaceForm.resident_agent_target,
        resident_agent_remote_profile_id:
          workspaceForm.resident_agent_target === 'remote'
            ? workspaceForm.resident_agent_remote_profile_id || null
            : null,
        resident_agent_cwd: workspaceForm.resident_agent_cwd.trim() || undefined,
        resident_agent_remote_reconnect: workspaceForm.resident_agent_remote_reconnect,
      })
    )
    if (workspace) {
      selectedWorkspaceId.value = workspace.id
      showWorkspaceModal.value = false
    }
  }

  // Build the resident_agent_* slice of a WorkspaceUpdate from the current form,
  // merged with any overrides. Shared by handleSaveWorkspace and the resident
  // lifecycle buttons (handleCreateResident) so the payload shape stays in one
  // place. Field shape matches the values WorkspaceUpdate accepts.
  function buildResidentPayload(overrides: Partial<WorkspaceUpdate> = {}): WorkspaceUpdate {
    return residentUpdatePayload(workspaceForm, parseLaunchEnv(workspaceForm.resident_env_text) ?? {}, overrides)
  }

  // --- Resident periodic-task editor helpers -----------------------------------
  //
  // Periodic tasks are the recurring checklist the resident runs EVERY wake-up
  // (distinct from the one-shot guiding directive). The modal edits a draft array
  // on workspaceForm; sanitize trims + drops empties before submit, mirroring the
  // backend's _normalize_periodic_tasks so what the user sees rendered matches
  // what the resident prompt embeds.

  function newPeriodicTaskId() {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID()
    }
    return `ptask-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }

  function addPeriodicTask() {
    workspaceForm.resident_agent_periodic_tasks.push({
      id: newPeriodicTaskId(),
      text: '',
      enabled: true,
    })
  }

  function removePeriodicTask(taskId: string) {
    const tasks = workspaceForm.resident_agent_periodic_tasks
    const index = tasks.findIndex((task) => task.id === taskId)
    if (index !== -1) tasks.splice(index, 1)
  }

  // Count of non-blank tasks currently enabled — drives the modal summary hint so
  // the user can see how many recurring items the resident will actually run.
  const enabledPeriodicTaskCount = computed(() =>
    projectEnabledPeriodicTaskCount(workspaceForm.resident_agent_periodic_tasks)
  )

  async function handleSaveWorkspace() {
    const workspaceId = editingWorkspaceId.value
    if (!workspaceId) return
    const workspace = await runPending('workspace:update', () =>
      workspaceStore.updateWorkspace(workspaceId, {
        name: workspaceForm.name.trim() || undefined,
        path: workspaceForm.path.trim() || undefined,
        default_branch: workspaceForm.default_branch.trim() || undefined,
        remote_cwd:
          workspaceForm.target === 'remote' ? workspaceForm.remote_cwd.trim() || null : undefined,
        remote_reconnect:
          workspaceForm.target === 'remote' ? workspaceForm.remote_reconnect : undefined,
        ...buildResidentPayload(),
      })
    )
    if (workspace) {
      showWorkspaceModal.value = false
    }
  }

  function resetWorkspaceForm() {
    workspaceForm.name = 'Claude Hub'
    workspaceForm.path = ''
    workspaceForm.default_branch = 'main'
    workspaceForm.session_prefix = 'chub'
    workspaceForm.target = 'local'
    workspaceForm.remote_profile_id = remoteProfiles.value[0]?.id || ''
    workspaceForm.remote_cwd = ''
    workspaceForm.remote_reconnect = true
    workspaceForm.resident_agent_enabled = false
    workspaceForm.resident_agent_paused = false
    workspaceForm.resident_agent_master_mode = false
    workspaceForm.resident_agent_interval_minutes = 60
    workspaceForm.resident_agent_directive = ''
    workspaceForm.resident_agent_periodic_tasks = []
    workspaceForm.resident_agent_type = 'claude'
    workspaceForm.resident_agent_solo_mode = true
    workspaceForm.resident_env_preset = defaultLaunchEnvPresetForAgent('claude')
    workspaceForm.resident_env_text = defaultPresetTextForAgent('claude')
    workspaceForm.resident_agent_title = ''
    workspaceForm.resident_agent_target = 'local'
    workspaceForm.resident_agent_remote_profile_id = remoteProfiles.value[0]?.id || ''
    workspaceForm.resident_agent_cwd = ''
    workspaceForm.resident_agent_remote_reconnect = true
  }

  function openWorkspaceModal() {
    resetWorkspaceForm()
    workspaceModalMode.value = 'create'
    editingWorkspaceId.value = null
    showWorkspaceModal.value = true
  }

  function openEditWorkspaceModal() {
    const workspace = activeWorkspace.value
    if (!workspace) return
    workspaceForm.name = workspace.name
    workspaceForm.path = workspace.path
    workspaceForm.default_branch = workspace.default_branch
    workspaceForm.session_prefix = workspace.session_prefix
    workspaceForm.target = workspace.target
    workspaceForm.remote_profile_id = workspace.remote_profile_id || ''
    workspaceForm.remote_cwd = workspace.remote_cwd || ''
    workspaceForm.remote_reconnect = workspace.remote_reconnect
    workspaceForm.resident_agent_enabled = workspace.resident_agent_enabled ?? false
    workspaceForm.resident_agent_paused = workspace.resident_agent_paused ?? false
    workspaceForm.resident_agent_master_mode = workspace.resident_agent_master_mode ?? false
    workspaceForm.resident_agent_interval_minutes = workspace.resident_agent_interval_minutes ?? 60
    workspaceForm.resident_agent_directive = workspace.resident_agent_directive || ''
    // Clone so the modal editor mutates a draft, not the store's workspace object.
    workspaceForm.resident_agent_periodic_tasks = (
      workspace.resident_agent_periodic_tasks ?? []
    ).map((task) => ({ ...task }))
    workspaceForm.resident_agent_type = workspace.resident_agent_type ?? 'claude'
    workspaceForm.resident_agent_solo_mode = workspace.resident_agent_solo_mode ?? true
    // Preset ids are localStorage-only; default the select to the agent-type
    // default and treat the serialized env text as the source of truth.
    workspaceForm.resident_env_preset = defaultLaunchEnvPresetForAgent(
      workspaceForm.resident_agent_type
    )
    workspaceForm.resident_env_text = serializeLaunchEnv(workspace.resident_agent_env)
    workspaceForm.resident_agent_title = workspace.resident_agent_title || ''
    workspaceForm.resident_agent_target = workspace.resident_agent_target ?? 'local'
    workspaceForm.resident_agent_remote_profile_id =
      workspace.resident_agent_remote_profile_id || ''
    workspaceForm.resident_agent_cwd = workspace.resident_agent_cwd || ''
    workspaceForm.resident_agent_remote_reconnect = workspace.resident_agent_remote_reconnect ?? true
    workspaceModalMode.value = 'edit'
    editingWorkspaceId.value = workspace.id
    showWorkspaceModal.value = true
    if (workspace.target === 'remote' || workspace.resident_agent_target === 'remote') {
      fetchRemoteProfiles()
    }
  }

  function closeWorkspaceModal() {
    showWorkspaceModal.value = false
    showResidentAgentModal.value = false
    workspaceModalMode.value = 'create'
    editingWorkspaceId.value = null
  }

  function openResidentAgentModal() {
    showResidentAgentModal.value = true
    // The Remote Server dropdown needs the profile list; load it lazily the same
    // way the Add-Agent modal does.
    if (remoteProfiles.value.length === 0) {
      fetchRemoteProfiles()
    }
  }

  function closeResidentAgentModal() {
    showResidentAgentModal.value = false
  }

  // --- Resident lifecycle buttons (Create / Pause / Delete) ---------------
  //
  // These act immediately via PATCH /api/workspaces/{id} in EDIT mode, where the
  // workspace already exists. In CREATE mode there is no id to PATCH yet, so the
  // three lifecycle buttons are disabled and the resident config is persisted by
  // the parent "Create workspace" button (handleCreateWorkspace already sends the
  // resident_agent_* fields). "Exists" is read from the SAVED workspace
  // (activeWorkspace), not the editable form flag.
  const isResidentCreateMode = computed(() => workspaceModalMode.value !== 'edit')

  const residentExists = computed(
    () => !isResidentCreateMode.value && (activeWorkspace.value?.resident_agent_enabled ?? false)
  )

  // Create / enable the resident: PATCH the full resident payload with
  // enabled:true. The next monitor tick spawns the resident session. Keep the
  // sub-modal open so the user sees the state flip (Create disables, Pause/Delete
  // enable). The override forces enabled:true regardless of the form value, so we
  // mirror the flag into the form ONLY after the PATCH succeeds (success-gated,
  // like handleToggleResidentPause / handleDeleteResident). This avoids leaving
  // the form at enabled:true after a failed PATCH, which a later parent "Save
  // workspace" would otherwise re-send and unintentionally create the resident.
  async function handleCreateResident() {
    if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
    if (residentExists.value) return
    const workspaceId = editingWorkspaceId.value
    const workspace = await runPending('resident:create', () =>
      workspaceStore.updateWorkspace(
        workspaceId,
        buildResidentPayload({ resident_agent_enabled: true })
      )
    )
    if (workspace) {
      workspaceForm.resident_agent_enabled = true
    }
  }

  // Toggle Pause/Resume: PATCH only resident_agent_paused. Backend keeps the
  // session + tab alive on pause (only stops auto-scheduling). Mirror the new
  // value into the form so the sub-modal checkbox/label stay in sync.
  async function handleToggleResidentPause() {
    if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
    if (!residentExists.value) return
    const workspaceId = editingWorkspaceId.value
    const next = !(activeWorkspace.value?.resident_agent_paused ?? false)
    const workspace = await runPending('resident:pause', () =>
      workspaceStore.updateWorkspace(workspaceId, { resident_agent_paused: next })
    )
    if (workspace) {
      workspaceForm.resident_agent_paused = next
    }
  }

  // Delete the resident agent ONLY (not the workspace): PATCH enabled:false. The
  // backend's disabling_resident path clears the session pointer, drops the
  // ManagedSession (the orphan-tab pruner removes the tab), and resets
  // last_run_at. The workspace itself is untouched.
  async function handleDeleteResident() {
    if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
    if (!residentExists.value) return
    const confirmed = window.confirm(
      'Remove the resident agent for this workspace? Its session will be stopped. '
        + 'The workspace itself is kept.'
    )
    if (!confirmed) return
    const workspaceId = editingWorkspaceId.value
    const workspace = await runPending('resident:delete', () =>
      workspaceStore.updateWorkspace(workspaceId, { resident_agent_enabled: false })
    )
    if (workspace) {
      workspaceForm.resident_agent_enabled = false
    }
  }

  // "Run now": force the resident to fire on the next monitor tick using the
  // SAVED directive + periodic tasks (no form save). Deliberate one-off — bypasses
  // pause but respects enabled, per the backend request_resident_run guard.
  async function handleRunResidentNow() {
    const workspaceId = editingWorkspaceId.value ?? activeWorkspaceId.value
    if (!workspaceId || !residentExists.value) return
    await runPending('resident:run', () => workspaceStore.runResidentNow(workspaceId))
  }

  // "Save & run now": persist the current form (directive + periodic tasks + all
  // resident config) THEN request an immediate run, so the freshly-saved directive
  // takes effect this cycle instead of waiting for the next interval. Plain "Save"
  // only applies on the next natural wake-up — this button makes the timing
  // explicit for the user, which was the core complaint.
  async function handleSaveResidentAndRunNow() {
    if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
    if (!residentExists.value) return
    const workspaceId = editingWorkspaceId.value
    const workspace = await runPending('resident:save-run', () =>
      workspaceStore.updateWorkspace(workspaceId, buildResidentPayload())
    )
    if (!workspace) return
    await workspaceStore.runResidentNow(workspaceId)
  }

  const residentSummaryLabel = computed(() => {
    if (!workspaceForm.resident_agent_enabled) return 'Off'
    const every = `every ${workspaceForm.resident_agent_interval_minutes || 60}m`
    const base = workspaceForm.resident_agent_paused ? `Paused · ${every}` : `On · ${every}`
    return workspaceForm.resident_agent_master_mode ? `${base} · Autopilot` : base
  })

  async function handleDeleteWorkspace() {
    const workspaceId = editingWorkspaceId.value
    if (!workspaceId) return
    const name = workspaceForm.name.trim() || 'this workspace'
    const confirmed = window.confirm(
      `Delete workspace "${name}"? This cannot be undone.`,
    )
    if (!confirmed) return
    try {
      await runPending('workspace:delete', () => workspaceStore.deleteWorkspace(workspaceId))
      closeWorkspaceModal()
    } catch {
      // Error already surfaced via store notification; keep the modal open.
    }
  }

  function handleResidentTargetChange(target: ExecutionTarget) {
    if (!workspaceForm.resident_agent_enabled) return
    if (workspaceForm.resident_agent_target === target) return
    workspaceForm.resident_agent_target = target
    if (target === 'remote') {
      if (!workspaceForm.resident_agent_remote_profile_id && remoteProfiles.value.length > 0) {
        workspaceForm.resident_agent_remote_profile_id = remoteProfiles.value[0].id
      }
      fetchRemoteProfiles()
    }
  }

  // Three-state resident lifecycle: Enable (exists + auto-works), Pause (session
  // stays for manual chat but won't auto-run), Delete (teardown + clear pointers).
  const isResidentPaused = computed(() => activeWorkspace.value?.resident_agent_paused ?? false)

  const isResidentMaster = computed(() => activeWorkspace.value?.resident_agent_master_mode ?? false)

  // Relative "last run" label for the resident card, mirroring the timeline's
  // "{duration} ago" style (see latestSelectedReportAgeLabel).
  const residentLastRunLabel = computed(() =>
    projectResidentLastRunLabel(activeWorkspace.value, elapsedClockMs.value)
  )

  // True while a run-now request is stamped but not yet consumed by a wake-up.
  // Drives the card's "queued" hint and disables the Run-now button so a user
  // can't stack duplicate requests before the monitor tick fires.
  const residentRunPending = computed(
    () => Boolean(activeWorkspace.value?.resident_agent_run_requested_at),
  )

  // Human "next run" hint for the resident card. Priority:
  //   - run-now requested      -> "next run queued"
  //   - paused                 -> "next run paused"
  //   - disabled / no schedule -> "" (caller hides the line)
  //   - future timestamp       -> "in {duration}" countdown
  //   - past-due timestamp     -> "due now" (overdue backstop will pick it up)
  const residentNextRunLabel = computed(() =>
    projectResidentNextRunLabel(activeWorkspace.value, elapsedClockMs.value)
  )

  // Newest report posted by the resident session, used to surface its latest
  // heartbeat on the resident card. Returns null when none exist.
  const latestResidentReport = computed<AgentReport | null>(() =>
    projectLatestResidentReport(activeWorkspace.value?.resident_agent_session_id, workspaceStore.reports)
  )

  function isResidentAgent(agent: ManagedSession) {
    return agent.role === 'resident'
  }

  async function toggleResidentPaused() {
    const workspaceId = activeWorkspaceId.value
    if (!workspaceId) return
    const next = !isResidentPaused.value
    await runPending('workspace:resident-pause', () =>
      workspaceStore.updateWorkspace(workspaceId, { resident_agent_paused: next })
    )
  }

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

  return {
    openWorkspaceModal,
    openEditWorkspaceModal,
    isResidentAgent,
    isResidentPaused,
    isResidentMaster,
    residentLastRunLabel,
    residentNextRunLabel,
    latestResidentReport,
    toggleResidentPaused,
    residentRunPending,
    handleRunResidentNow,
    showWorkspaceModal,
    closeWorkspaceModal,
    workspaceModalMode,
    handleSubmitWorkspace,
    workspaceForm,
    remoteProfilesLoading,
    remoteProfiles,
    selectedRemoteProfile,
    residentSummaryLabel,
    openResidentAgentModal,
    handleDeleteWorkspace,
    showResidentAgentModal,
    closeResidentAgentModal,
    enabledPeriodicTaskCount,
    removePeriodicTask,
    addPeriodicTask,
    handleResidentTargetChange,
    isResidentCreateMode,
    residentExists,
    handleDeleteResident,
    handleToggleResidentPause,
    handleCreateResident,
    handleSaveResidentAndRunNow,
    fetchRemoteProfiles,
  }
}
