import { type Ref, computed, ref, toRef, watch } from 'vue'
import { type StreamCapabilities, type AgentStreamEvent } from '@/types'
import { type ChatPaneProps, type DraftAttachment } from './types'
import { type useTerminalStore } from '@/stores/terminalStore'
import { toStreamSendAttachment } from '@/utils/agentStreamAttachments'
import { useChatGoal as useGoalResource } from '@/composables/useChatGoal'
import { goalBlocksPlan, goalPlanLockReason, isGoalTerminal } from '@/utils/chatGoalPolicy'
import { hasChatStatusRefreshBoundary } from '@/utils/chatTurnLifecycle'

interface Dependencies {
  attachments: Ref<DraftAttachment[]>
  capabilities: Ref<StreamCapabilities | null>
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  currentModeId: Ref<string | null>
  events: Ref<AgentStreamEvent[]>
  isManagedChat: Ref<boolean>
  isUpdatingMode: Ref<boolean>
  isUpdatingModel: Ref<boolean>
  isUpdatingReasoningEffort: Ref<boolean>
  modeInteractionLocked: Ref<boolean>
  props: ChatPaneProps
  terminalStore: ReturnType<typeof useTerminalStore>
  timelineDisposed: boolean
}

export function useChatGoal(deps: Dependencies) {
  const {
    goal,
    error: goalError,
    isHydrated: isGoalHydrated,
    isHydrating: isGoalHydrating,
    isMutating: isGoalMutating,
    hydrate: hydrateGoal,
    create: createGoal,
    updateObjective: updateGoalObjective,
    pause: pauseGoal,
    resume: resumeGoal,
    complete: completeGoal,
    clear: clearGoal,
  } = useGoalResource(toRef(deps.props, 'tabId'))

  const isGoalSetupOpen = ref(false)

  const isGoalEditing = ref(false)

  const goalPlanReason = computed(() => goalPlanLockReason(goal.value))

  const goalEditReason = computed(() => goal.value && (!isGoalTerminal(goal.value.status) || goalBlocksPlan(goal.value))
    ? 'Finish or clear the Goal before editing history'
    : null)

  const goalComposerLocked = computed(() => goalBlocksPlan(goal.value))

  const goalComposerReason = computed(() => goalComposerLocked.value
    ? 'Pause or complete the active Goal before sending messages'
    : null)

  // An active Goal accepts text steers as its next turn; Enter queues instead.
  const goalImageRecovery = computed(() => Boolean(goal.value?.status === 'paused' && goal.value.pending_image_count))

  const goalSteerOnly = computed(() => goal.value?.status === 'active' || goalImageRecovery.value)

  const goalInputBlocked = computed(() => goalComposerLocked.value && !goalSteerOnly.value)

  async function startGoal(input: { objective: string }) {
    if (isGoalEditing.value) {
      if (await updateGoalObjective(input.objective)) isGoalSetupOpen.value = false
      return
    }
    if (goalSetupDisabledReason.value) {
      goalError.value = goalSetupDisabledReason.value
      return
    }
    const imageSnapshot = [...deps.attachments.value]
    const saved = await createGoal({ ...input, attachments: imageSnapshot.map(toStreamSendAttachment) })
    if (saved) {
      deps.attachments.value = []
      isGoalSetupOpen.value = false
    } else if (goal.value) {
      // The server may have persisted the Goal before delivery failed. Keep
      // originals in the composer so an explicit image steer can recover it.
      isGoalSetupOpen.value = false
    }
  }

  const GOAL_REFRESH_DELAYS_MS = [0, 120, 300, 650, 1200] as const

  let goalRefreshEpoch = 0

  function goalSnapshotVersion(): string {
    const current = goal.value
    if (!current) return 'none'
    return [
      current.updated_at,
      current.status,
      current.turns_completed,
      current.checkpoint?.turn_id ?? '',
    ].join('\u0000')
  }

  function goalReflectsCompletedTurn(turnId: string | null, baselineVersion: string): boolean {
    const current = goal.value
    if (!current || current.status !== 'active') return true
    if (turnId && (
      current.completed_turn_ids?.includes(turnId) ||
      current.checkpoint?.turn_id === turnId
    )) return true
    return turnId ? false : goalSnapshotVersion() !== baselineVersion
  }

  function waitForGoalRefresh(delayMs: number): Promise<void> {
    return new Promise(resolve => window.setTimeout(resolve, delayMs))
  }

  /**
   * Goal observation runs asynchronously after the transcript commits a
   * turn_completed event. The first GET may therefore still return the prior
   * snapshot. Retry with a short bounded backoff until either the completed turn
   * is visible or the authoritative Goal version advances.
   */
  async function refreshGoalAfterTurn(turnId: string | null): Promise<void> {
    const refreshEpoch = ++goalRefreshEpoch
    const baselineVersion = goalSnapshotVersion()
    for (const delayMs of GOAL_REFRESH_DELAYS_MS) {
      if (delayMs > 0) await waitForGoalRefresh(delayMs)
      if (refreshEpoch !== goalRefreshEpoch || deps.timelineDisposed) return
      await hydrateGoal()
      if (refreshEpoch !== goalRefreshEpoch || deps.timelineDisposed) return
      if (goalReflectsCompletedTurn(turnId, baselineVersion)) return
    }
  }

  const goalSetupDisabledReason = computed(() => {
    if (goal.value) return 'Clear the current Goal before starting another'
    if (!deps.capabilities.value?.supports_goals) return 'Goals are unavailable for this chat'
    if (!isGoalHydrated.value || isGoalHydrating.value) return 'Loading Goal status…'
    if (goalError.value && !isGoalSetupOpen.value) return 'Reload Goal status before starting a Goal'
    if (isGoalMutating.value) return 'Updating Goal…'
    if (deps.connectionState.value !== 'live') return 'Wait for Chat to reconnect'
    if (deps.modeInteractionLocked.value || deps.isUpdatingMode.value || deps.isUpdatingModel.value || deps.isUpdatingReasoningEffort.value) return 'Wait for the current turn or settings change to finish'
    if (deps.currentModeId.value === 'plan') return 'Switch to Agent mode before setting a Goal'
    return null
  })

  function cancelGoalRefresh() { goalRefreshEpoch++ }

  function registerWatchers() {
    // Chat lifecycle edges are authoritative status boundaries. Refresh the tab
    // status once per committed boundary. Goal completion gets a bounded poll
    // because its observer updates the separate control plane asynchronously.
    watch(deps.events, (latest, previous) => {
      if (hasChatStatusRefreshBoundary(previous, latest)) {
        void deps.terminalStore.fetchAgentStatuses()
        const previousLength = previous.length <= latest.length ? previous.length : 0
        let completed = null as (typeof latest)[number] | null
        for (let index = latest.length - 1; index >= previousLength; index -= 1) {
          if (latest[index].type === 'turn_completed') {
            completed = latest[index]
            break
          }
        }
        if (!deps.isManagedChat.value && completed) void refreshGoalAfterTurn(completed.turn_id ?? null)
        else if (!deps.isManagedChat.value) void hydrateGoal()
      }
    })
  }

  return {
    goal,
    goalError,
    isGoalHydrated,
    isGoalHydrating,
    isGoalMutating,
    hydrateGoal,
    createGoal,
    updateGoalObjective,
    pauseGoal,
    resumeGoal,
    completeGoal,
    clearGoal,
    isGoalSetupOpen,
    isGoalEditing,
    goalPlanReason,
    goalEditReason,
    goalComposerLocked,
    goalComposerReason,
    goalImageRecovery,
    goalSteerOnly,
    goalInputBlocked,
    startGoal,
    GOAL_REFRESH_DELAYS_MS,
    goalSnapshotVersion,
    goalReflectsCompletedTurn,
    waitForGoalRefresh,
    refreshGoalAfterTurn,
    goalSetupDisabledReason,
    cancelGoalRefresh,
    registerWatchers,
  }
}
