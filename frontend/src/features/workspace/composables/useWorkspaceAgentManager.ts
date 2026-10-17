import {
  ref,
  reactive,
  computed,
  watch,
} from 'vue'
import {
  type ManagedSession,
  type WorkspaceSessionRole,
  type AgentType,
  type SessionKind,
  type ExecutionTarget,
} from '@/types'
import {
  defaultLaunchEnvPresetForAgent,
  parseLaunchEnv,
} from '@/composables/useLaunchEnvPresets'
import {
  providerSupportsManagedSurface,
} from '@/utils/managedChatSurface'
import {
  remoteInteractive,
} from '@/utils/remoteProfiles'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'
import type { useWorkspaceEditor } from './useWorkspaceEditor'

type WorkspaceAgentManagerInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'defaultPresetTextForAgent'
  | 'reviewerSessions'
  | 'workspaceAgents'
  | 'dispatcherAgent'
  | 'residentAgent'
  | 'isLoading'
  | 'isPending'
  | 'activeWorkspace'
  | 'getPresetText'
  | 'workspaceStore'
  | 'runPending'
  | 'sessionActionKey'
  | 'terminalStore'
>
  & Pick<ReturnType<typeof useWorkspaceEditor>,
  'remoteProfiles'
  | 'fetchRemoteProfiles'
>

export function useWorkspaceAgentManager(inputs: WorkspaceAgentManagerInputs) {
  const {
    defaultPresetTextForAgent,
    reviewerSessions,
    workspaceAgents,
    dispatcherAgent,
    residentAgent,
    isLoading,
    isPending,
    activeWorkspace,
    getPresetText,
    workspaceStore,
    runPending,
    sessionActionKey,
    terminalStore,
    remoteProfiles,
    fetchRemoteProfiles,
  } = inputs

  const showAgentOptionsModal = ref(false)

  const showAgentFileBrowser = ref(false)

  // Switch Env modal state (for hot-swapping env/solo on running Claude agents)
  const showSwitchEnvModal = ref(false)

  const showSwitchEnvManager = ref(false)

  const switchEnvAgent = ref<ManagedSession | null>(null)

  const switchEnvForm = reactive({
    env_preset: 'custom',
    env_text: '',
    solo_mode: false,
  })

  const agentOptionsForm = reactive({
    title: '',
    role: 'orchestrator' as WorkspaceSessionRole,
    agent_type: 'codex' as AgentType,
    session_kind: 'terminal' as SessionKind,
    target: 'local' as ExecutionTarget,
    cwd: '',
    solo_mode: true,
    remote_profile_id: '',
    remote_reconnect: true,
    env_preset: defaultLaunchEnvPresetForAgent('codex'),
    env_text: defaultPresetTextForAgent('codex'),
  })

  const agentManagerView = ref<'agents' | 'reviewers'>('agents')

  const agentManagerSessions = computed<ManagedSession[]>(() =>
    agentManagerView.value === 'reviewers'
      ? reviewerSessions.value
      : [
          ...workspaceAgents.value,
          ...(dispatcherAgent.value ? [dispatcherAgent.value] : []),
          ...(residentAgent.value ? [residentAgent.value] : []),
        ],
  )

  const agentManagerEmptyText = computed(() =>
    agentManagerView.value === 'reviewers'
      ? 'No reviewers in this workspace.'
      : 'No agents in this workspace.',
  )

  const selectedAgentRemoteProfile = computed(() =>
    remoteProfiles.value.find(profile => profile.id === agentOptionsForm.remote_profile_id) || null
  )

  const isAgentOptionsCreateDisabled = computed(
    () =>
      isLoading.value ||
      isPending('agent:create') ||
      !providerSupportsManagedSurface(
        agentOptionsForm.agent_type,
        agentOptionsForm.session_kind,
        agentOptionsForm.target,
      ) ||
      (agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id) ||
      (agentOptionsForm.target === 'remote' && !remoteInteractive(selectedAgentRemoteProfile.value))
  )

  function workspaceDefaultCwd(target: ExecutionTarget, remoteProfileId?: string): string {
    const workspace = activeWorkspace.value
    if (!workspace) return ''
    if (target === 'remote') {
      // When a specific profile id is supplied (e.g. the resident form's own
      // selection) resolve its default_cwd; otherwise fall back to the Add-Agent
      // form's selected profile.
      const profile = remoteProfileId
        ? remoteProfiles.value.find(p => p.id === remoteProfileId) || null
        : selectedAgentRemoteProfile.value
      return workspace.remote_cwd || profile?.default_cwd || ''
    }
    return workspace.path || ''
  }

  // ---- Switch Env (hot-swap on running Claude agents) ----

  function serializeAgentEnv(env: Record<string, string> | undefined | null): string {
    if (!env) return ''
    return Object.entries(env)
      .map(([k, v]) => `${k}=${v}`)
      .join('\n')
  }

  function applySwitchEnvPreset(presetId: string) {
    const text = getPresetText(presetId)
    if (text === null) return
    switchEnvForm.env_text = text
  }

  function openSwitchEnvModal(agent: ManagedSession) {
    switchEnvAgent.value = agent
    switchEnvForm.env_preset = 'custom'
    switchEnvForm.env_text = serializeAgentEnv(agent.env)
    switchEnvForm.solo_mode = agent.solo_mode ?? false
    showSwitchEnvModal.value = true
  }

  function closeSwitchEnvModal() {
    showSwitchEnvModal.value = false
    showSwitchEnvManager.value = false
    switchEnvAgent.value = null
  }

  function openSwitchEnvPresetManager() {
    showSwitchEnvManager.value = true
  }

  function closeSwitchEnvPresetManager() {
    showSwitchEnvManager.value = false
    applySwitchEnvPreset(switchEnvForm.env_preset)
  }

  async function handleSwitchEnv() {
    const agent = switchEnvAgent.value
    if (!agent) return
    const env = parseLaunchEnv(switchEnvForm.env_text)
    if (!env) {
      workspaceStore.pushNotification({
        type: 'error',
        message: 'Please provide at least one KEY=VALUE environment variable, or pick a preset.',
        autoDismissMs: 6000,
      })
      return
    }
    try {
      await runPending(sessionActionKey('switch-env', agent.id), async () => {
        await terminalStore.switchEnv(agent.tab_id, {
          env,
          solo_mode: switchEnvForm.solo_mode,
        })
      })
      workspaceStore.pushNotification({
        type: 'success',
        message: `Environment switched for "${agent.title}". Agent is resuming conversation.`,
        autoDismissMs: 4000,
      })
      closeSwitchEnvModal()
    } catch (e) {
      // terminalStore.switchEnv already pushes an error notification; log too.
      console.error('switch env failed', e)
    }
  }

  function resetAgentEnvForType(agentType: AgentType) {
    agentOptionsForm.env_preset = defaultLaunchEnvPresetForAgent(agentType)
    agentOptionsForm.env_text = defaultPresetTextForAgent(agentType)
  }

  function resetAgentOptionsForm() {
    const workspace = activeWorkspace.value
    agentOptionsForm.title = ''
    agentOptionsForm.role = 'orchestrator'
    agentOptionsForm.agent_type = 'codex'
    agentOptionsForm.session_kind = 'terminal'
    agentOptionsForm.target = workspace?.target ?? 'local'
    agentOptionsForm.solo_mode = true
    agentOptionsForm.remote_reconnect = workspace?.remote_reconnect ?? true
    agentOptionsForm.remote_profile_id =
      workspace?.remote_profile_id || remoteProfiles.value[0]?.id || ''
    resetAgentEnvForType(agentOptionsForm.agent_type)
    agentOptionsForm.cwd = workspaceDefaultCwd(agentOptionsForm.target)
  }

  function handleAgentSurfaceChange(sessionKind: SessionKind) {
    if (sessionKind === 'chat' && agentOptionsForm.target === 'remote') return
    agentOptionsForm.session_kind = sessionKind
    if (
      (sessionKind === 'chat' && agentOptionsForm.agent_type === 'terminal') ||
      (sessionKind === 'terminal' && agentOptionsForm.agent_type === 'maso')
    ) {
      agentOptionsForm.agent_type = 'codex'
      agentOptionsForm.solo_mode = true
      resetAgentEnvForType('codex')
    }
  }

  function handleAgentTargetChange(target: ExecutionTarget) {
    if (agentOptionsForm.target === target) return
    const previousDefault = workspaceDefaultCwd(agentOptionsForm.target)
    agentOptionsForm.target = target
    if (target === 'remote') handleAgentSurfaceChange('terminal')
    if (!agentOptionsForm.cwd.trim() || agentOptionsForm.cwd === previousDefault) {
      agentOptionsForm.cwd = workspaceDefaultCwd(target)
    }
    if (target === 'remote') {
      fetchRemoteProfiles()
    }
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
    const env = parseLaunchEnv(agentOptionsForm.env_text)
    await runPending('agent:create', async () => {
      await workspaceStore.ensureWorkspaceAgent({
        agent_type: agentOptionsForm.agent_type,
        session_kind: agentOptionsForm.session_kind,
        title: agentOptionsForm.title.trim() || null,
        role: agentOptionsForm.role,
        reuse_existing: false,
        target: agentOptionsForm.target,
        cwd: agentOptionsForm.target === 'local' ? cwd || null : null,
        remote_profile_id:
          agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_profile_id || null : null,
        remote_cwd: agentOptionsForm.target === 'remote' ? cwd || null : null,
        remote_reconnect:
          agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_reconnect : null,
        solo_mode:
          agentOptionsForm.agent_type === 'cursor' ||
          agentOptionsForm.agent_type === 'terminal'
            ? false
            : agentOptionsForm.solo_mode,
        env,
      })
      showAgentFileBrowser.value = false
      agentOptionsForm.title = ''
      resetAgentEnvForType(agentOptionsForm.agent_type)
      await terminalStore.fetchTabs({ force: true })
    })
  }

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
      if (
        agentOptionsForm.target === 'remote' &&
        (!agentOptionsForm.cwd || agentOptionsForm.cwd === '~')
      ) {
        agentOptionsForm.cwd = selectedAgentRemoteProfile.value?.default_cwd || '~'
      }
    }
  )

  return {
    openAgentOptionsModal,
    openSwitchEnvModal,
    showAgentOptionsModal,
    closeAgentOptionsModal,
    agentManagerView,
    agentManagerSessions,
    agentManagerEmptyText,
    handleCreateAdvancedAgent,
    agentOptionsForm,
    handleAgentSurfaceChange,
    handleAgentTargetChange,
    selectedAgentRemoteProfile,
    isAgentOptionsCreateDisabled,
    showAgentFileBrowser,
    switchEnvForm,
    showSwitchEnvManager,
    closeSwitchEnvPresetManager,
    showSwitchEnvModal,
    closeSwitchEnvModal,
    switchEnvAgent,
    handleSwitchEnv,
    applySwitchEnvPreset,
    openSwitchEnvPresetManager,
  }
}
