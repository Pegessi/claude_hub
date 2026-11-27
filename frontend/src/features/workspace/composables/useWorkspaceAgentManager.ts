import { ref, reactive } from 'vue'
import type { ManagedSession } from '@/types'
import { parseLaunchEnv } from '@/composables/useLaunchEnvPresets'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceAgentManagerInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'getPresetText' | 'workspaceStore' | 'runPending' | 'sessionActionKey' | 'terminalStore'
>

/** Environment controls for existing task and Resident sessions, not a seat pool. */
export function useWorkspaceAgentManager(inputs: WorkspaceAgentManagerInputs) {
  const { getPresetText, workspaceStore, runPending, sessionActionKey, terminalStore } = inputs

  // Switch Env modal state (for hot-swapping env/solo on running Claude agents)
  const showSwitchEnvModal = ref(false)

  const showSwitchEnvManager = ref(false)

  const switchEnvAgent = ref<ManagedSession | null>(null)

  const switchEnvForm = reactive({
    env_preset: 'custom',
    env_text: '',
    solo_mode: false,
  })

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

  return {
    openSwitchEnvModal,
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
