import { type Ref, type ComputedRef, computed, nextTick, ref, watch } from 'vue'
import { type StreamCapabilities, type TerminalTab, StreamModelOption } from '@/types'
import { type useAgentStream } from '@/composables/useAgentStream'
import { type useTerminalStore } from '@/stores/terminalStore'
import { getAvailableChatModes, getCurrentChatModeId } from '@/utils/chatModePolicy'
import { LEGACY_TRAEX_REASONING_EFFORT_ENV, defaultModelReasoningEffortLabel, filterChatModels, isChatModelActive, isChatModelEffortActive, presentChatModel } from '@/features/chat/presentation/chatModelPresentation'

interface Dependencies {
  capabilities: Ref<StreamCapabilities | null>
  currentTab: ComputedRef<TerminalTab | null>
  goalPlanReason: Ref<string | null>
  modeInteractionLocked: Ref<boolean>
  preparationEpoch: Ref<number>
  refreshCapabilities: () => Promise<void>
  setMode: ReturnType<typeof useAgentStream>['setMode']
  terminalStore: ReturnType<typeof useTerminalStore>
}

export function useChatSettings(deps: Dependencies) {
  const isAddMenuOpen = ref(false)

  const isUpdatingMode = ref(false)

  const modeChangeError = ref<string | null>(null)

  const isModeMenuOpen = ref(false)

  const modeOptions = computed(() => getAvailableChatModes(deps.capabilities.value))

  const currentModeId = computed(() => getCurrentChatModeId(deps.capabilities.value))

  const currentModeOption = computed(() => modeOptions.value.find(option => option.id === currentModeId.value))

  const currentModeLabel = computed(() => currentModeOption.value?.label ?? 'Mode')

  const modelPresentation = computed(() => presentChatModel(deps.currentTab.value, deps.capabilities.value))

  const modelEnvVar = computed(() => modelPresentation.value.modelEnvVar)

  const currentModel = computed(() => modelPresentation.value.currentModel)

  const modelOptions = computed(() => modelPresentation.value.modelOptions)

  const currentModelOption = computed(() => modelPresentation.value.currentModelOption)

  const reasoningEffortEnvVar = computed(() => modelPresentation.value.reasoningEffortEnvVar)

  const combinedModelLabel = computed(() => modelPresentation.value.combinedModelLabel)

  function isModelActive(model: StreamModelOption) {
    return isChatModelActive(model, modelPresentation.value)
  }

  function isModelEffortActive(model: StreamModelOption, effort: string) {
    return isChatModelEffortActive(model, effort, modelPresentation.value)
  }

  function defaultReasoningEffortLabel(model: StreamModelOption) {
    return defaultModelReasoningEffortLabel(model, deps.capabilities.value?.current_reasoning_effort)
  }

  // Search filter for the picker. Matches id or label, case-insensitive.
  const modelSearch = ref('')

  const filteredModelOptions = computed(() => filterChatModels(modelOptions.value, modelSearch.value))

  const isModelPickerAvailable = computed(() => modelEnvVar.value !== null)

  const isModelMenuOpen = ref(false)

  const isUpdatingModel = ref(false)

  const isUpdatingReasoningEffort = ref(false)

  const selectedMenuModelId = ref('')

  const selectedMenuModel = computed(() =>
    filteredModelOptions.value.find(model => model.id === selectedMenuModelId.value)
    ?? filteredModelOptions.value.find(model => model.id === currentModelOption.value?.id)
    ?? filteredModelOptions.value[0]
    ?? null,
  )

  function chooseMenuModel(model: StreamModelOption) {
    selectedMenuModelId.value = model.id
    if (model.supported_reasoning_efforts.length === 0) {
      void selectModelAndEffort(model, '')
    }
  }

  async function selectModelAndEffort(model: StreamModelOption, effort: string) {
    closeModelMenu(true)
    const key = modelEnvVar.value
    if (!key) return
    const tab = deps.currentTab.value
    if (!tab) return
    const epoch = deps.preparationEpoch.value
    isUpdatingModel.value = true
    isUpdatingReasoningEffort.value = true
    try {
      const env = { ...(tab.env ?? {}) }
      const selectedEffort = model.supported_reasoning_efforts.find(option => option.id === effort)
      const providerModelId = selectedEffort?.provider_model_id ?? model.provider_model_id ?? model.id
      if (providerModelId) env[key] = providerModelId
      else delete env[key]
      const effortKey = reasoningEffortEnvVar.value
      if (effortKey) {
        if (effort) env[effortKey] = effort
        else delete env[effortKey]
      }
      if (tab.agent_type === 'traex') delete env[LEGACY_TRAEX_REASONING_EFFORT_ENV]
      // Errors surface via the store's notifyError toast; swallow so the
      // rejection is not unhandled.
      await deps.terminalStore.switchEnv(tab.id, { env })
      await deps.refreshCapabilities()
    } catch {
      // Swallow: error feedback comes from terminalStore.switchEnv's toast.
    } finally {
      if (deps.preparationEpoch.value === epoch) {
        isUpdatingModel.value = false
        isUpdatingReasoningEffort.value = false
      }
    }
  }

  async function selectModel(model: string) {
    const option = modelOptions.value.find(item => item.id === model)
    if (option) {
      await selectModelAndEffort(option, '')
      return
    }
    const custom: StreamModelOption = { id: model, label: model, supported_reasoning_efforts: [] }
    await selectModelAndEffort(custom, '')
  }

  function closeModelMenu(focusTrigger: boolean) {
    isModelMenuOpen.value = false
    modelSearch.value = ''
    if (focusTrigger) modelTriggerEl.value?.focus()
  }

  function toggleModelMenu() {
    if (
      deps.modeInteractionLocked.value
      || isUpdatingModel.value
      || isUpdatingReasoningEffort.value
    ) return
    if (isModelMenuOpen.value) {
      closeModelMenu(false)
      return
    }
    isModelMenuOpen.value = true
    selectedMenuModelId.value = currentModelOption.value?.id ?? filteredModelOptions.value[0]?.id ?? ''
    void nextTick(() => {
      modelSearchEl.value?.focus()
    })
  }

  function handleModelOutsidePointer(e: PointerEvent) {
    if (!isModelMenuOpen.value) return
    const el = modelPickerEl.value
    if (el && !el.contains(e.target as Node)) closeModelMenu(false)
  }

  const modelPickerEl = ref<HTMLElement | null>(null)

  const modelTriggerEl = ref<HTMLButtonElement | null>(null)

  const modelInputEl = ref<HTMLInputElement | null>(null)

  const modelSearchEl = ref<HTMLInputElement | null>(null)

  const modePickerEl = ref<HTMLElement | null>(null)

  const modeTriggerEl = ref<HTMLButtonElement | null>(null)

  const modeMenuEl = ref<HTMLElement | null>(null)

  function handleModeOutsidePointer(event: PointerEvent) {
    if (!isModeMenuOpen.value || modePickerEl.value?.contains(event.target as Node)) return
    closeModeMenu(false)
  }

  function closeModeMenu(restoreFocus: boolean) {
    if (!isModeMenuOpen.value) return
    isModeMenuOpen.value = false
    if (restoreFocus) void nextTick(() => modeTriggerEl.value?.focus())
  }

  function toggleModeMenu() {
    if (deps.modeInteractionLocked.value || isUpdatingMode.value) return
    if (isModeMenuOpen.value) {
      closeModeMenu(false)
      return
    }
    isModeMenuOpen.value = true
    void nextTick(() => {
      const currentItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[aria-checked="true"]')
      const firstItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[role="menuitemradio"]')
      const focusTarget = currentItem ?? firstItem
      focusTarget?.focus()
    })
  }

  async function selectMode(modeId: string) {
    closeModeMenu(true)
    await changeMode(modeId)
  }

  async function changeMode(modeId: string) {
    if (deps.modeInteractionLocked.value || isUpdatingMode.value || currentModeId.value === modeId) return
    if (modeId === 'plan' && deps.goalPlanReason.value) {
      modeChangeError.value = deps.goalPlanReason.value
      return
    }
    const epoch = deps.preparationEpoch.value
    isUpdatingMode.value = true
    modeChangeError.value = null
    try {
      await deps.setMode(modeId)
    } catch (err) {
      if (deps.preparationEpoch.value !== epoch) return
      modeChangeError.value = err instanceof Error ? err.message : 'Failed to update Chat mode.'
    } finally {
      if (deps.preparationEpoch.value === epoch) isUpdatingMode.value = false
    }
  }

  function registerAvailabilityWatcher() {
    // Reset picker state if it unmounts while open (e.g. the agent type changes),
    // so a remount does not reopen with a stale menu/query.
    watch(isModelPickerAvailable, available => {
      if (!available) {
        isModelMenuOpen.value = false
        modelSearch.value = ''
      }
    })
  }

  function registerMenuWatchers() {
    watch(isAddMenuOpen, open => {
      if (open) { closeModeMenu(false); closeModelMenu(false) }
    })

    watch([isModeMenuOpen, isModelMenuOpen], ([mode, model]) => {
      if (mode || model) isAddMenuOpen.value = false
    })

    watch([deps.modeInteractionLocked, isUpdatingMode], ([locked, updating]) => {
      if (locked || updating) closeModeMenu(false)
    })

    watch([deps.modeInteractionLocked, isUpdatingModel], ([locked, updating]) => {
      if (locked || updating) closeModelMenu(false)
    })
  }

  return {
    isAddMenuOpen,
    isUpdatingMode,
    modeChangeError,
    isModeMenuOpen,
    modeOptions,
    currentModeId,
    currentModeOption,
    currentModeLabel,
    modelPresentation,
    modelEnvVar,
    currentModel,
    modelOptions,
    currentModelOption,
    reasoningEffortEnvVar,
    combinedModelLabel,
    isModelActive,
    isModelEffortActive,
    defaultReasoningEffortLabel,
    modelSearch,
    filteredModelOptions,
    isModelPickerAvailable,
    isModelMenuOpen,
    isUpdatingModel,
    isUpdatingReasoningEffort,
    selectedMenuModelId,
    selectedMenuModel,
    chooseMenuModel,
    selectModelAndEffort,
    selectModel,
    closeModelMenu,
    toggleModelMenu,
    handleModelOutsidePointer,
    modelPickerEl,
    modelTriggerEl,
    modelInputEl,
    modelSearchEl,
    modePickerEl,
    modeTriggerEl,
    modeMenuEl,
    handleModeOutsidePointer,
    closeModeMenu,
    toggleModeMenu,
    selectMode,
    changeMode,
    registerAvailabilityWatcher,
    registerMenuWatchers,
  }
}
