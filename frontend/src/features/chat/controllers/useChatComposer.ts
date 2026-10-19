import { type Ref, nextTick, ref, watch } from 'vue'
import { type TimelineTurn, type TimelineApproval } from '../timeline/types'
import { type QuestionIdentity, type DraftAttachment } from './types'
import { type QueuedChatDraft } from '@/stores/terminalStore'
import { useQuestionAnswers, approvalStateSignature } from '@/composables/useQuestionAnswers'
import { autoresizeComposerTextarea, resolveComposerEnterAction } from '@/utils/chatComposerInteraction'
import { formatAskQuestionResponse } from '@/utils/chatQuestionResponse'

interface Dependencies {
  attachments: Ref<DraftAttachment[]>
  closeModeMenu: (restoreFocus: boolean) => void
  closeModelMenu: (focusTrigger: boolean) => void
  composerError: Ref<string | null>
  draftMessage: Ref<string>
  draftQueue: Ref<QueuedChatDraft[]>
  enqueueDraft: () => void
  goalComposerReason: Ref<string | null>
  goalInputBlocked: Ref<boolean>
  goalSteerOnly: Ref<boolean>
  isManagedChat: Ref<boolean>
  isModeMenuOpen: Ref<boolean>
  isModelMenuOpen: Ref<boolean>
  isPaneActivated: Ref<boolean>
  isSending: Ref<boolean>
  sendQueuedDraft: (id: string) => Promise<void>
  submit: (delivery?: 'normal' | 'steer', messageOverride?: string, source?: 'user' | 'queue', questionIdentity?: QuestionIdentity | null, queuedDraft?: QueuedChatDraft) => Promise<boolean>
  turnInFlight: Ref<boolean>
  composerTurnInFlight: Ref<boolean>
}

export function useChatComposer(deps: Dependencies) {
  // Approval-card selection state (AskUserQuestion / AskQuestion /
  // request_user_input). Component-scoped so a stream batch rebuilding the
  // timeline turn never wipes a pending selection.
  const {
    questionAnswers,
    resolvedApprovalKeys,
    customAnswers,
    customAnswer,
    setCustomAnswer,
    isQuestionOptionSelected,
    toggleQuestionOption,
    isApprovalResolved,
    canSubmitQuestion,
    answersFor,
    markResolved,
  } = useQuestionAnswers()

  const isComposing = ref(false)

  // Mobile soft keyboards have no Shift/⌘/Ctrl keys, so on a mobile viewport the
  // composer hides the desktop-only key hints and treats Enter as a newline
  // (sending is only via the Send button). Reactive on resize so rotating or
  // resizing a device updates it live.
  const isMobileViewport = ref(false)

  function handleViewportResize() {
    isMobileViewport.value = window.innerWidth <= 768
  }

  const composerTextareaEl = ref<HTMLTextAreaElement | null>(null)

  function handleDocumentKeydown(event: KeyboardEvent) {
    if (!deps.isPaneActivated.value) return
    if (event.key === 'Escape' && deps.isModelMenuOpen.value) {
      event.preventDefault()
      deps.closeModelMenu(true)
      return
    }
    if (event.key === 'Escape' && deps.isModeMenuOpen.value) {
      event.preventDefault()
      deps.closeModeMenu(true)
      return
    }
  }

  function syncComposerTextareaHeight() {
    autoresizeComposerTextarea(composerTextareaEl.value)
  }

  function handleComposerEnter(event: KeyboardEvent) {
    if (deps.goalInputBlocked.value) {
      deps.composerError.value = deps.goalComposerReason.value
      return
    }
    const action = resolveComposerEnterAction({
      isComposing: isComposing.value,
      shiftKey: event.shiftKey,
      metaKey: event.metaKey,
      ctrlKey: event.ctrlKey,
      altKey: event.altKey,
      turnInFlight: deps.composerTurnInFlight.value || deps.goalSteerOnly.value,
      hasDraft: deps.draftMessage.value.trim().length > 0 || deps.attachments.value.length > 0,
      hasQueuedDraft: deps.draftQueue.value.length > 0,
      isMobile: isMobileViewport.value,
    })
    if (action === 'ignore' || action === 'newline') return
    event.preventDefault()
    if (deps.isManagedChat.value) {
      // Managed Chat has no browser-only queue: every explicit Enter/Ctrl+Enter
      // is a durable Hub mailbox intent, even while the provider owns a turn.
      void deps.submit('normal')
      return
    }
    if (action === 'steer-queued') {
      const first = deps.draftQueue.value[0]
      if (first) void deps.sendQueuedDraft(first.id)
      return
    }
    if (action === 'queue') {
      deps.enqueueDraft()
      return
    }
    void deps.submit(action === 'steer' ? 'steer' : 'normal')
  }

  /**
   * Per-turn memo signature for approval-card interaction state.
   *
   * Each turn is memoized with ``v-memo="[renderRevision, erroredAttachments,
   * signature]"``. The approval card's selected / resolved / send-disabled state
   * lives in component refs (``questionAnswers``, ``resolvedApprovalKeys``,
   * ``isSending``) that are NOT part of the timeline turn, so without this
   * signature a click toggles the reactive state but ``v-memo`` skips
   * re-rendering the turn — the chip never shows selected and the submit button
   * stays disabled. Folding this signature into the deps re-renders only the
   * turn owning the changed approval; turns without approvals return ``''`` and
   * stay memoized.
   */
  function turnApprovalSignature(turn: TimelineTurn): string {
    // Turns without approvals never depend on this state; keep them memoized.
    if (turn.approvals.length === 0) return ''
    let signature = ''
    for (const approval of turn.approvals) {
      signature += `${approval.key}=${approvalStateSignature(
        approval,
        questionAnswers.value,
        resolvedApprovalKeys.value,
        customAnswers.value,
      )};`
    }
    // isSending gates the option/submit disabled state inside the card, so a
    // send start/end must also invalidate the memo for approval-bearing turns.
    return deps.isSending.value ? `${signature}|sending` : signature
  }

  async function submitQuestionResponse(turn: TimelineTurn, approval: TimelineApproval) {
    if (!canSubmitQuestion(approval)) {
      deps.composerError.value = '请选择所有问题的选项后再提交。'
      return
    }
    if (!turn.turnId || !approval.callId) {
      deps.composerError.value = 'This provider question is missing its current request identity.'
      return
    }
    const delivery = deps.isManagedChat.value ? 'normal' : deps.turnInFlight.value ? 'steer' : 'normal'
    const sent = await deps.submit(
      delivery,
      formatAskQuestionResponse(answersFor(approval.key)),
      'user',
      { turnId: turn.turnId, callId: approval.callId },
    )
    if (sent) markResolved(approval.key)
  }

  function registerWatchers() {
    watch(deps.draftMessage, () => {
      void nextTick(() => syncComposerTextareaHeight())
    })
  }

  return {
    questionAnswers,
    resolvedApprovalKeys,
    customAnswers,
    customAnswer,
    setCustomAnswer,
    isQuestionOptionSelected,
    toggleQuestionOption,
    isApprovalResolved,
    canSubmitQuestion,
    answersFor,
    markResolved,
    isComposing,
    isMobileViewport,
    handleViewportResize,
    composerTextareaEl,
    handleDocumentKeydown,
    syncComposerTextareaHeight,
    handleComposerEnter,
    turnApprovalSignature,
    submitQuestionResponse,
    registerWatchers,
  }
}
