import { type Ref, type ComputedRef, computed, nextTick, ref, watch } from 'vue'
import { type ChatGoal, type AgentStreamEvent, type WorkspaceAttachmentCreate } from '@/types'
import { type TimelineTurn } from '../timeline/types'
import { type PendingTurn, type RetriableSend, type QuestionIdentity, type DraftAttachment } from './types'
import { type QueuedChatDraft, type useTerminalStore } from '@/stores/terminalStore'
import { toStreamSendAttachment } from '@/utils/agentStreamAttachments'
import { isChatModeLocked } from '@/utils/chatTurnLifecycle'
import { hasAuthoritativeTerminalFor } from '@/utils/agentStreamStopReset'

interface Dependencies {
  attachments: Ref<DraftAttachment[]>
  authoritativeTurns: ComputedRef<TimelineTurn[]>
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  draftQueue: Ref<QueuedChatDraft[]>
  enqueueDraft: () => void
  events: Ref<AgentStreamEvent[]>
  goal: Ref<ChatGoal | null>
  goalComposerReason: Ref<string | null>
  goalError: Ref<string | null>
  goalImageRecovery: Ref<boolean>
  goalInputBlocked: Ref<boolean>
  goalSteerOnly: Ref<boolean>
  hydrateGoal: () => Promise<void>
  isDraftQueuePaused: Ref<boolean>
  isGoalHydrating: Ref<boolean>
  isGoalMutating: Ref<boolean>
  isManagedChat: Ref<boolean>
  isPreparingAttachments: Ref<boolean>
  isUpdatingMode: Ref<boolean>
  isUpdatingModel: Ref<boolean>
  isUpdatingReasoningEffort: Ref<boolean>
  nudge: () => void
  requestLatestAnchor: (force?: boolean) => void
  streamBasePath: Ref<string>
  syncComposerTextareaHeight: () => void
  terminalStore: ReturnType<typeof useTerminalStore>
}

export function useChatDelivery(deps: Dependencies) {
  const retrySendAttempt = ref<RetriableSend | null>(null)

  function sameRetryAttachment(left: DraftAttachment, right: DraftAttachment): boolean {
    return left.filename === right.filename &&
      left.mime_type === right.mime_type &&
      left.data_url === right.data_url &&
      left.preview_data_url === right.preview_data_url
  }

  function reusableSendId(
    message: string,
    draftAtts: DraftAttachment[],
    questionIdentity: QuestionIdentity | null,
  ): string | null {
    const attempt = retrySendAttempt.value
    if (!attempt || attempt.message !== message || attempt.attachments.length !== draftAtts.length) {
      return null
    }
    if (
      attempt.questionIdentity?.turnId !== questionIdentity?.turnId ||
      attempt.questionIdentity?.callId !== questionIdentity?.callId
    ) {
      return null
    }
    return attempt.attachments.every((attachment, index) =>
      sameRetryAttachment(attachment, draftAtts[index]))
      ? attempt.clientTurnId
      : null
  }

  const pendingDirectTurns = ref<PendingTurn[]>([])

  // After a successful manual Stop we optimistically append one terminal
  // ``cancelled`` edge for the stopped turn, so the composer unlocks and the
  // round renders as Stopped WITHOUT waiting for the possibly-wedged long-poll
  // to deliver the backend's persisted edge. The synthetic stream_sequence (-1)
  // never exists server-side; the reducer's turn_completed is idempotent, and
  // the optimism is dropped as soon as the real edge lands in ``events``.
  const optimisticCancelledTurnId = ref<string | null>(null)

  const pendingTurns = computed(() => {
    const observedTurnIds = new Set(deps.authoritativeTurns.value.map(turn => turn.turnId).filter(Boolean))
    return pendingDirectTurns.value.filter(turn => !observedTurnIds.has(turn.turnId))
  })

  const draftMessage = ref('')

  const composerError = ref<string | null>(null)

  const isSending = ref(false)

  const isCancelling = ref(false)

  const turnInFlight = computed(() => isChatModeLocked(
    pendingDirectTurns.value.length > 0,
    deps.authoritativeTurns.value,
  ))

  const modeInteractionLocked = computed(() => isSending.value || turnInFlight.value)

  // ── Manual Stop: unconditional recovery ─────────────────────────────────────
  // A user Stop is explicit intent and must work for ANY wedged active turn,
  // including one the conservative auto-watchdogs deliberately leave alone (a
  // long turn that keeps emitting thinking/tool records but never closes).
  // ``stopArmed`` is therefore slightly wider than ``turnInFlight``: a genuine
  // error already released the composer lock, but the turn is still unfinished in
  // the timeline — Stop stays mounted so the user can terminalize it. Stop in
  // the reconnecting/FAILED banners shares this same gate.
  const stopArmed = computed(() => {
    if (pendingDirectTurns.value.length > 0) return true
    const latest = deps.authoritativeTurns.value[deps.authoritativeTurns.value.length - 1]
    return Boolean(latest && !latest.completed)
  })

  const stopTargetTurnId = computed(() => {
    const latest = deps.authoritativeTurns.value[deps.authoritativeTurns.value.length - 1]
    if (latest && !latest.completed && latest.turnId) return latest.turnId
    const pending = pendingDirectTurns.value[pendingDirectTurns.value.length - 1]
    return pending?.turnId ?? null
  })

  const isSubmissionReady = computed(() => deps.connectionState.value === 'live' &&
    (!deps.goalImageRecovery.value || (!deps.isGoalHydrating.value && !deps.goalError.value)) &&
    !deps.isGoalMutating.value &&
    !deps.isPreparingAttachments.value &&
    !deps.isUpdatingMode.value &&
    !deps.isUpdatingModel.value &&
    !deps.isUpdatingReasoningEffort.value)

  const canSend = computed(() => isSubmissionReady.value &&
    !deps.goalInputBlocked.value &&
    (!deps.goalImageRecovery.value || deps.attachments.value.length > 0) &&
    (draftMessage.value.trim().length > 0 || deps.attachments.value.length > 0))

  function reconcileGoalRecovery() {
    const attempt = retrySendAttempt.value
    if (!attempt?.goalRecovery) return
    const delivered = deps.authoritativeTurns.value.some(turn => turn.turnId === attempt.clientTurnId)
      || deps.goal.value?.completed_turn_ids?.includes(attempt.clientTurnId)
      || (deps.goal.value?.current_turn_id === attempt.clientTurnId && deps.goal.value?.dispatch_state === 'dispatched')
    if (!delivered) return
    if (reusableSendId(draftMessage.value, deps.attachments.value, null) === attempt.clientTurnId) {
      draftMessage.value = ''
      deps.attachments.value = []
      composerError.value = null
    }
    retrySendAttempt.value = null
  }

  async function cancelActiveTurn() {
    // Idempotent: a second click while Stop is in flight is a no-op. Stop stays
    // armed slightly wider than the composer lock (an unfinished turn after a
    // terminal error), but is never allowed to race a send still in flight.
    if (isCancelling.value || isSending.value || !stopArmed.value) return
    const cancelledTurnId = stopTargetTurnId.value
    isCancelling.value = true
    composerError.value = null
    try {
      const res = await fetch(`${deps.streamBasePath.value}/cancel`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        // Hint the backend to terminalize THIS turn; it ignores the hint when it
        // no longer matches (Stop racing a natural completion stays a single
        // terminal edge). Omitted for a turn whose authoritative id has not
        // hydrated yet — the backend then terminalizes its active/orphan turn.
        body: cancelledTurnId ? JSON.stringify({ expected_turn_id: cancelledTurnId }) : undefined,
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          // ignore non-JSON error body
        }
        throw new Error(detail)
      }
      const result: { cancelled?: boolean; outcome?: string } = await res.json().catch(() => ({}))
      if (
        deps.isManagedChat.value &&
        result?.cancelled !== true &&
        result?.outcome !== 'already_terminal'
      ) {
        throw new Error('Stop did not match the current managed Chat turn.')
      }
      // Direct Chat preserves its legacy idempotent 200 contract. Managed Chat
      // uses the explicit outcome so a stale Stop can never unlock a newer turn,
      // while an already-terminal retry still repairs a wedged local view.
      if (cancelledTurnId) {
        optimisticCancelledTurnId.value = cancelledTurnId
      }
      deps.nudge()
      void deps.terminalStore.fetchAgentStatuses()
    } catch (err) {
      composerError.value = err instanceof Error ? err.message : 'Failed to stop the current turn.'
    } finally {
      isCancelling.value = false
    }
  }

  /**
   * Deliver composer input to the native provider transport via ``/stream/send``.
   *
   * StructuredPane is mounted only for top-level Chat tabs. Text and images are
   * staged and submitted together through the native transport's atomic
   * send_message boundary.
   */
  async function sendToStream(
    message: string,
    atts: WorkspaceAttachmentCreate[],
    clientTurnId: string,
    delivery: 'normal' | 'steer' = 'normal',
    timeoutMs: number | null = null,
    questionIdentity: QuestionIdentity | null = null,
  ) {
    const base = `${deps.streamBasePath.value}/send`
    const controller = new AbortController()
    const timer = timeoutMs === null ? null : setTimeout(() => controller.abort(), timeoutMs)
    let res: Response
    try {
      res = await fetch(base, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        signal: controller.signal,
        body: JSON.stringify({
          client_turn_id: clientTurnId,
          text: message,
          delivery,
          attachments: atts.map(toStreamSendAttachment),
          question_turn_id: questionIdentity?.turnId,
          question_call_id: questionIdentity?.callId,
        }),
      })
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        throw Object.assign(
          new Error('Send timed out. Delivery is uncertain; review the Chat before retrying.'),
          { cause: error },
        )
      }
      throw error
    } finally {
      if (timer !== null) clearTimeout(timer)
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore non-JSON error body
      }
      throw new Error(detail)
    }
  }

  async function submit(
    delivery: 'normal' | 'steer' = 'normal',
    messageOverride?: string,
    source: 'user' | 'queue' = 'user',
    questionIdentity: QuestionIdentity | null = null,
    queuedDraft?: QueuedChatDraft,
  ): Promise<boolean> {
    if (isSending.value) return false
    if (!messageOverride && deps.goalInputBlocked.value) {
      composerError.value = deps.goalComposerReason.value
      return false
    }
    // Buttons are disabled from ``canSend``, but keyboard and watcher-driven
    // entries call submit directly. Re-check the same availability boundary so
    // Enter cannot dispatch during hydration/attachment preparation, and queued
    // drafts cannot leave before recovery reaches the authoritative live state.
    if (queuedDraft ? !isSubmissionReady.value : !messageOverride && !canSend.value) return false
    // Approval answers and "Implement plan" use messageOverride and may bypass a
    // Goal's ordinary composer lock, but they still require a live transport.
    if (messageOverride && deps.connectionState.value !== 'live') return false
    const message = queuedDraft?.message ?? messageOverride ?? draftMessage.value
    const hasContent = message.trim().length > 0 || (queuedDraft?.attachments ?? deps.attachments.value).length > 0
    if (!hasContent) return false
    if (
      !deps.isManagedChat.value &&
      delivery === 'normal' &&
      (turnInFlight.value || deps.goalSteerOnly.value) &&
      !messageOverride
    ) {
      deps.enqueueDraft()
      return false
    }
    isSending.value = true
    composerError.value = null
    // A new turn supersedes any local Stop optimism for the previous one.
    optimisticCancelledTurnId.value = null
    // Snapshot the full draft attachments (including id, preview_url,
    // preview_data_url, size_bytes) so we can restore the composer exactly on
    // send failure — no reconstructed ids, no zeroed sizes.
    const draftAtts: DraftAttachment[] = queuedDraft ? [...queuedDraft.attachments] : messageOverride ? [] : [...deps.attachments.value]
    // The original data_url goes to the provider; preview_data_url is the
    // stored image (sendToStream flags it instead when it is the original).
    const atts: WorkspaceAttachmentCreate[] = draftAtts.map(
      ({ filename, mime_type, data_url, preview_data_url }) => ({
        filename,
        mime_type,
        data_url,
        preview_data_url,
      }),
    )
    // Optimistic bubble thumbnails come from the bounded preview.
    const pendingAtts = draftAtts.map(({ preview_url, mime_type }) => ({
      preview_url,
      mime_type,
    }))
    const recoveringGoal = deps.goalImageRecovery.value
    // Native provider turns do not deduplicate IDs. Only controller-backed
    // delivery may retain a failed attempt; an ordinary resend is a new intent.
    const goalControlledInput = !messageOverride && (recoveringGoal || deps.goal.value?.status === 'active' || deps.goal.value?.status === 'blocked')
    const retainRetryId = deps.isManagedChat.value || goalControlledInput
    const reusableId = retainRetryId ? reusableSendId(message, draftAtts, questionIdentity) : null
    const retainedClientTurnId = recoveringGoal && !retrySendAttempt.value?.goalRecovery ? null : (queuedDraft?.clientTurnId ?? queuedDraft?.id) ?? reusableId
    const clientTurnId = retainedClientTurnId ?? (typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`)
    if (queuedDraft) queuedDraft.clientTurnId = clientTurnId
    try {
      if (!messageOverride && !queuedDraft) {
        draftMessage.value = ''
        deps.attachments.value = []
      }
      // Show the user's turn immediately; the stream will replace it with the
      // authoritative transcript line once the provider echoes it back.
      //
      // A provider-native question answer (messageOverride) is NOT a new turn:
      // the transport routes it as the blocking question's JSON-RPC response and
      // publishes no turn_started for it, so an optimistic bubble would never
      // reconcile against an authoritative turnId — leaving it pinned forever and
      // holding turnInFlight (composer lock) until remount. The approval card is
      // already marked resolved by submitQuestionResponse as the visual ack.
      if (!messageOverride) {
        pendingDirectTurns.value = [
          ...pendingDirectTurns.value,
          {
            key: `pending-${clientTurnId}`,
            turnId: clientTurnId,
            userText: message,
            attachments: pendingAtts,
          },
        ]
      }
      deps.requestLatestAnchor(true)
      await sendToStream(
        message,
        atts,
        clientTurnId,
        delivery,
        source === 'queue' ? 15_000 : null,
        questionIdentity,
      )
      retrySendAttempt.value = null
      if (!deps.isManagedChat.value && (messageOverride || (deps.goal.value && delivery === 'steer'))) await deps.hydrateGoal()
      // Deliberate user input may resume a queue moved into the composer, but
      // cannot silently release another item with unconfirmed delivery.
      if (!messageOverride && source === 'user' && !deps.draftQueue.value.some(draft => draft.error)) deps.isDraftQueuePaused.value = false
      // The POST acknowledgement means provider dispatch has begun. Refresh the
      // backend-native tab status now rather than waiting for the 5s poll phase;
      // turn_started/completed/error boundaries above provide subsequent edges.
      void deps.terminalStore.fetchAgentStatuses()
      // Success: composer already cleared; nothing more to do.
      void nextTick(() => deps.syncComposerTextareaHeight())
      return true
    } catch (err) {
      pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => turn.turnId !== clientTurnId)
      if (!queuedDraft && retainRetryId) {
        retrySendAttempt.value = {
          goalRecovery: recoveringGoal,
          clientTurnId,
          message,
          attachments: [...draftAtts],
          questionIdentity: questionIdentity ? { ...questionIdentity } : null,
        }
      } else if (!queuedDraft) retrySendAttempt.value = null
      if (queuedDraft) {
        queuedDraft.error = err instanceof Error ? err.message : 'Failed to send message.'
        deps.isDraftQueuePaused.value = true
      } else if (!messageOverride) {
        draftMessage.value = message
        deps.attachments.value = draftAtts
      }
      composerError.value = err instanceof Error ? err.message : 'Failed to send message.'
      if (!deps.isManagedChat.value && deps.goal.value) {
        await deps.hydrateGoal()
        // Only a successful authoritative refresh permits a new explicit
        // recovery intent. A failed GET keeps the same attempt and blocks send.
        if (recoveringGoal && !deps.goalError.value && deps.goalImageRecovery.value) retrySendAttempt.value = null
      }
      return false
    } finally {
      isSending.value = false
    }
  }

  function registerPendingWatcher() {
    // Reconcile optimistic (pending) turns against authoritative turns as they
    // arrive. No text-reveal state is kept: assistant text is rendered directly
    // from the batched event stream.
    watch(
      deps.authoritativeTurns,
      (latest) => {
        const observed = new Set(latest.map(turn => turn.turnId).filter(Boolean))
        pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => !observed.has(turn.turnId))
      },
      { immediate: true },
    )
  }

  function registerGoalRecoveryWatcher() {
    watch([deps.goal, retrySendAttempt, deps.authoritativeTurns], reconcileGoalRecovery)
  }

  function registerTerminalWatcher() {
    // Drop the local Stop optimism as soon as the backend's own terminal edge for
    // that turn arrives (any status: the natural completion winning the race is
    // just as terminal). The reducer's turn_completed is idempotent, so until this
    // fires the synthetic edge and the real one never double-render.
    watch(deps.events, (latest) => {
      const optimisticId = optimisticCancelledTurnId.value
      if (!optimisticId) return
      if (hasAuthoritativeTerminalFor(latest, optimisticId)) {
        optimisticCancelledTurnId.value = null
      }
    })
  }

  return {
    retrySendAttempt,
    sameRetryAttachment,
    reusableSendId,
    pendingDirectTurns,
    optimisticCancelledTurnId,
    pendingTurns,
    draftMessage,
    composerError,
    isSending,
    isCancelling,
    turnInFlight,
    modeInteractionLocked,
    stopArmed,
    stopTargetTurnId,
    isSubmissionReady,
    canSend,
    reconcileGoalRecovery,
    cancelActiveTurn,
    sendToStream,
    submit,
    registerPendingWatcher,
    registerGoalRecoveryWatcher,
    registerTerminalWatcher,
  }
}
