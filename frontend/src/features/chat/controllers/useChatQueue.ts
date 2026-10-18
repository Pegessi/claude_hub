import { type Ref, type ComputedRef, computed, nextTick, toRef, watch } from 'vue'
import { type TimelineTurn } from '../timeline/types'
import { type ChatPaneProps, type RetriableSend, type QuestionIdentity, type DraftAttachment } from './types'
import { type QueuedChatDraft, type useTerminalStore } from '@/stores/terminalStore'
import { createComposerDraftId } from '@/utils/chatComposerInteraction'

interface Dependencies {
  attachments: Ref<DraftAttachment[]>
  authoritativeTurns: ComputedRef<TimelineTurn[]>
  canSend: Ref<boolean>
  composerError: Ref<string | null>
  composerTextareaEl: Ref<HTMLTextAreaElement | null>
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  draftMessage: Ref<string>
  goalComposerLocked: Ref<boolean>
  goalComposerReason: Ref<string | null>
  goalInputBlocked: Ref<boolean>
  goalSteerOnly: Ref<boolean>
  isPaneActivated: Ref<boolean>
  isPreparingAttachments: Ref<boolean>
  isSending: Ref<boolean>
  isSubmissionReady: Ref<boolean>
  props: ChatPaneProps
  retrySendAttempt: Ref<RetriableSend | null>
  submit: (delivery?: 'normal' | 'steer', messageOverride?: string, source?: 'user' | 'queue', questionIdentity?: QuestionIdentity | null, queuedDraft?: QueuedChatDraft) => Promise<boolean>
  syncComposerTextareaHeight: () => void
  terminalStore: ReturnType<typeof useTerminalStore>
  composerTurnInFlight: Ref<boolean>
}

export function useChatQueue(deps: Dependencies) {
  const queuedChatState = deps.terminalStore.getQueuedChatState(deps.props.paneId, deps.props.tabId)

  const draftQueue = toRef(queuedChatState, 'drafts')

  const isEditingQueuedDraft = toRef(queuedChatState, 'editing')

  const isQueueSendInProgress = toRef(queuedChatState, 'sendInProgress')

  // A queued POST failure is delivery-uncertain: retain its visible card and
  // pause later messages until authoritative history confirms acceptance or the
  // user reviews/removes/edits the uncertain intent.
  const isDraftQueuePaused = toRef(queuedChatState, 'paused')

  const queueAutoActivationBlock = toRef(queuedChatState, 'autoActivationBlock')

  const isWaitingForQueuedChatActivation = computed(() =>
    deps.terminalStore.hasQueuedChatActivationRequest(deps.props.paneId, deps.props.tabId)
    && !deps.terminalStore.isQueuedChatActivationOwner(deps.props.paneId, deps.props.tabId)
  )

  function enqueueDraft() {
    if (deps.goalInputBlocked.value) {
      deps.composerError.value = deps.goalComposerReason.value
      return
    }
    if (!deps.canSend.value) return
    draftQueue.value.push({
      id: createComposerDraftId(),
      message: deps.draftMessage.value,
      attachments: [...deps.attachments.value],
    })
    deps.draftMessage.value = ''
    deps.attachments.value = []
    deps.composerError.value = null
    void nextTick(() => deps.syncComposerTextareaHeight())
  }

  function setQueuedDraftEdit(id: string | null, message: string) {
    queuedChatState.editingDraftId = id
    queuedChatState.editingMessage = message
    isEditingQueuedDraft.value = Boolean(id)
  }

  function reconcileQueuedDeliveries() {
    const observed = new Set(deps.authoritativeTurns.value.map(turn => turn.turnId))
    const accepted = draftQueue.value.filter(draft => draft.error && draft.id !== queuedChatState.editingDraftId && observed.has(draft.clientTurnId ?? draft.id))
    if (!accepted.length) return
    if (accepted.some(draft => draft.error === deps.composerError.value)) deps.composerError.value = null
    const acceptedIds = new Set(accepted.map(draft => draft.id))
    draftQueue.value = draftQueue.value.filter(draft => !acceptedIds.has(draft.id))
    if (!draftQueue.value.some(draft => draft.error) && !deps.draftMessage.value.trim() && !deps.attachments.value.length) isDraftQueuePaused.value = false
  }

  function updateQueuedDraft(id: string, message: string, nextAttachments: DraftAttachment[]) {
    if (deps.isSending.value || isQueueSendInProgress.value || deps.isPreparingAttachments.value) return
    const draft = draftQueue.value.find(item => item.id === id)
    if (!draft) return
    // An edit is a new intent; unchanged retries keep the original delivery ID.
    if (message === draft.message && nextAttachments.length === draft.attachments.length && nextAttachments.every((att, i) => att.id === draft.attachments[i]?.id)) return
    draft.clientTurnId = createComposerDraftId()
    draft.message = message
    draft.attachments = [...nextAttachments]
    draft.error = undefined
  }

  function cancelQueuedDraft(id: string) {
    if (deps.isSending.value || isQueueSendInProgress.value || deps.isPreparingAttachments.value) return
    draftQueue.value = draftQueue.value.filter(item => item.id !== id)
  }

  function editQueuedDraft(id: string) {
    if (deps.isSending.value || isQueueSendInProgress.value || deps.isPreparingAttachments.value) return
    const draft = draftQueue.value.find(item => item.id === id)
    if (!draft) return
    // Preserve an existing composer draft instead of overwriting it.
    const existing = deps.draftMessage.value.trim() || deps.attachments.value.length
      ? { id: createComposerDraftId(), message: deps.draftMessage.value, attachments: [...deps.attachments.value] }
      : null
    draftQueue.value = draftQueue.value.filter(item => item.id !== id)
    if (existing) draftQueue.value.push(existing)
    isDraftQueuePaused.value = true
    deps.draftMessage.value = draft.message
    deps.attachments.value = [...draft.attachments]
    // Moving is an explicit new intent, including when the old delivery is
    // unconfirmed. Never disguise it as a native same-ID retry.
    deps.retrySendAttempt.value = null
    deps.composerError.value = draft.error ? 'Review the conversation before sending this as a new message.' : null
    void nextTick(() => { deps.syncComposerTextareaHeight(); deps.composerTextareaEl.value?.focus() })
  }

  async function sendQueuedDraft(id: string) {
    if (deps.isSending.value || isQueueSendInProgress.value || !deps.isSubmissionReady.value || deps.goalInputBlocked.value || isEditingQueuedDraft.value) return
    const draft = draftQueue.value.find(item => item.id === id)
    if (!draft) return
    if (draft.error) {
      reconcileQueuedDeliveries()
      return
    }
    isQueueSendInProgress.value = true
    try {
      const sent = await deps.submit(deps.composerTurnInFlight.value || deps.goalSteerOnly.value ? 'steer' : 'normal', undefined, 'queue', null, draft)
      if (sent) {
        draftQueue.value = draftQueue.value.filter(item => item.id !== id)
        isDraftQueuePaused.value = draftQueue.value.some(item => Boolean(item.error))
      }
    } finally { isQueueSendInProgress.value = false }
  }

  async function flushDraftQueue() {
    while (
      draftQueue.value.length > 0
      && deps.isSubmissionReady.value
      && !isDraftQueuePaused.value
      && !deps.composerTurnInFlight.value
      && !deps.isSending.value
      && !isQueueSendInProgress.value
      && !isEditingQueuedDraft.value
      && !deps.goalComposerLocked.value
    ) {
      const activationRequestId = deps.terminalStore.queuedChatActivationRequestId(
        deps.props.paneId,
        deps.props.tabId,
      )
      isQueueSendInProgress.value = true
      let sent = false
      try {
        const next = draftQueue.value[0]
        if (!next) break
        sent = await deps.submit('normal', undefined, 'queue', null, next)
        if (sent) draftQueue.value = draftQueue.value.filter(item => item.id !== next.id)
      } finally {
        isQueueSendInProgress.value = false
        // The next ready hidden Chat may activate only after this one has
        // completed its foreground send attempt (including POST acknowledgement
        // or the existing delivery-uncertain pause path). Settle by the globally
        // unique tab ID because the Chat may have moved panes during the POST.
        deps.terminalStore.settleQueuedChatActivation(deps.props.tabId, activationRequestId)
      }
      if (!sent) break
    }
  }

  function requestDraftQueueFlush() {
    if (!deps.isPaneActivated.value || isEditingQueuedDraft.value) {
      deps.terminalStore.releaseQueuedChatActivation(deps.props.paneId, deps.props.tabId)
      return
    }
    if (
      draftQueue.value.length > 0
      && (deps.connectionState.value === 'failed' || deps.goalComposerLocked.value)
    ) {
      queueAutoActivationBlock.value = deps.connectionState.value === 'failed'
        ? 'stream-failed'
        : 'goal-locked'
      deps.terminalStore.releaseQueuedChatActivation(deps.props.paneId, deps.props.tabId)
      return
    }
    if (
      deps.isSubmissionReady.value
      && deps.isPaneActivated.value
      && !isWaitingForQueuedChatActivation.value
      && !isEditingQueuedDraft.value
      && !isDraftQueuePaused.value
      && !deps.composerTurnInFlight.value
      && draftQueue.value.length > 0
      && !deps.isSending.value
      && !deps.goalComposerLocked.value
    ) {
      queueAutoActivationBlock.value = null
      void flushDraftQueue()
    }
  }

  function registerReconciliationWatcher() {
    watch([deps.authoritativeTurns, () => draftQueue.value.map(draft => draft.error), isEditingQueuedDraft], reconcileQueuedDeliveries)
  }

  function registerAutoFlushWatcher() {
    watch(
      [
        deps.composerTurnInFlight,
        () => draftQueue.value.length,
        deps.isSending,
        deps.goalComposerLocked,
        deps.isSubmissionReady,
        isDraftQueuePaused,
        isEditingQueuedDraft,
        queueAutoActivationBlock,
        deps.isPaneActivated,
        isWaitingForQueuedChatActivation,
      ],
      requestDraftQueueFlush,
    )
  }

  return {
    queuedChatState,
    draftQueue,
    isEditingQueuedDraft,
    isQueueSendInProgress,
    isDraftQueuePaused,
    queueAutoActivationBlock,
    isWaitingForQueuedChatActivation,
    enqueueDraft,
    setQueuedDraftEdit,
    reconcileQueuedDeliveries,
    updateQueuedDraft,
    cancelQueuedDraft,
    editQueuedDraft,
    sendQueuedDraft,
    flushDraftQueue,
    requestDraftQueueFlush,
    registerReconciliationWatcher,
    registerAutoFlushWatcher,
  }
}
