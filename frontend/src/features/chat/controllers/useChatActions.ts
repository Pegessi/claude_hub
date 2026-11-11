import { type Ref, type ComputedRef, ref } from 'vue'
import { type TimelineTurn } from '../timeline/types'
import { type DraftAttachment } from './types'
import {
  canOfferEdit, editResendBody, editUnavailableReason, removedTurnsNotice,
} from '@/utils/chatEditResend'
import { type ChatPaneProps, type QuestionIdentity } from './types'
import { type QueuedChatDraft, type useTerminalStore } from '@/stores/terminalStore'
import { type useAgentStream } from '@/composables/useAgentStream'
import { type useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import { getCompletedPlanText } from '../timeline/turnProcess'
import { messageSourceAvatarUrl } from '../timeline/eventValues'
import { trustedFeishuAvatarUrl } from '@/utils/feishuIdentity'
import { writeClipboard } from '@/utils/clipboard'
import { buildTurnCopyText } from '@/utils/chatTurnCopy'
import { plainAgentDirectives } from '@/utils/agentDirectives'

interface Dependencies {
  composerError: Ref<string | null>
  currentModeId: Ref<string | null>
  feishuBotPoolStore: ReturnType<typeof useFeishuBotPoolStore>
  goalComposerLocked: Ref<boolean>
  goalComposerReason: Ref<string | null>
  goalEditReason: Ref<string | null>
  isManagedChat: Ref<boolean>
  modeChangeError: Ref<string | null>
  props: ChatPaneProps
  resetStream: () => void
  setMode: ReturnType<typeof useAgentStream>['setMode']
  start: ReturnType<typeof useAgentStream>['start']
  streamBasePath: Ref<string>
  streamSource: ComputedRef<'managed-session' | 'terminal-tab'>
  streamSourceId: Ref<string>
  submit: (delivery?: 'normal' | 'steer', messageOverride?: string, source?: 'user' | 'queue', questionIdentity?: QuestionIdentity | null, queuedDraft?: QueuedChatDraft) => Promise<boolean>
  terminalStore: ReturnType<typeof useTerminalStore>
  turnInFlight: Ref<boolean>
  turns: ComputedRef<TimelineTurn[]>
  /** Provider/session support for rewriting past turns; the pane derives it
   *  from the session's agent type. */
  supportsEditResend: Ref<boolean>
  /** Images the reader added while editing. The backend re-sends the original
   *  turn's own images, so this array carries additions only. */
  editAttachments: Ref<DraftAttachment[]>
  editAttachmentError: Ref<string | null>
  isPreparingEditAttachments: Ref<boolean>
  handleEditPaste: (event: ClipboardEvent) => void
  removeEditAttachment: (att: DraftAttachment) => void
  /** Drop the editor's staged images, aborting any batch still being read. */
  resetEditAttachments: () => void
  /** Local Stop optimism for a previous turn; resending history retires it. */
  optimisticCancelledTurnId: Ref<string | null>
}

export function useChatActions(deps: Dependencies) {
  function resolvedFeishuSourceAvatarUrl(turn: TimelineTurn): string | null {
    const matchingBot = deps.feishuBotPoolStore.bots.find(
      bot => bot.app_id === turn.feishu?.appId,
    )
    const currentBotAvatar = trustedFeishuAvatarUrl(matchingBot?.feishu_avatar_url)
    if (currentBotAvatar) return currentBotAvatar
    const eventAvatar = trustedFeishuAvatarUrl(messageSourceAvatarUrl(turn))
    if (eventAvatar) return eventAvatar
    if (turn.feishu?.appId) return null
    return trustedFeishuAvatarUrl(
      deps.feishuBotPoolStore.activeBotForTab(deps.props.tabId)?.feishu_avatar_url,
    )
  }

  function feishuSourceIdentityKey(turn: TimelineTurn): string {
    return [
      turn.key,
      turn.feishu?.appId ?? 'unknown-app',
      resolvedFeishuSourceAvatarUrl(turn) ?? 'feishu-brand',
    ].join('\n')
  }

  // ── Fork from turn ──────────────────────────────────────────────────────────
  // Preserve the full transcript ordinal when rendering a bounded history window.
  const forkingOrdinal = ref<number | null>(null)

  const implementingPlanKey = ref<string | null>(null)

  function completedPlanText(turn: TimelineTurn): string | null {
    if (turn.key !== deps.turns.value[deps.turns.value.length - 1]?.key) return null
    return getCompletedPlanText(turn)
  }

  async function implementPlan(turn: TimelineTurn) {
    if (deps.isManagedChat.value) return
    const plan = completedPlanText(turn)
    if (!plan || implementingPlanKey.value || deps.turnInFlight.value) return
    if (deps.goalComposerLocked.value) {
      deps.modeChangeError.value = deps.goalComposerReason.value
      return
    }
    implementingPlanKey.value = turn.key
    deps.modeChangeError.value = null
    try {
      if (deps.currentModeId.value !== 'default') await deps.setMode('default')
      const sent = await deps.submit('normal', 'Implement the approved plan now.')
      if (!sent) throw new Error(deps.composerError.value || 'Could not start implementation.')
    } catch (err) {
      deps.modeChangeError.value = err instanceof Error ? err.message : 'Failed to start implementation.'
    } finally {
      implementingPlanKey.value = null
    }
  }

  async function forkFromTurn(ordinal: number) {
    if (deps.isManagedChat.value || forkingOrdinal.value !== null) return
    forkingOrdinal.value = ordinal
    try {
      await deps.terminalStore.forkTab(deps.props.tabId, ordinal)
    } finally {
      forkingOrdinal.value = null
    }
  }

  // ── Copy whole turn ─────────────────────────────────────────────────────────
  // One control per turn, sharing the turn-actions row with "Fork from here".
  // Feedback (Copied / Copy failed) is keyed per turn and auto-dismissed.
  const copyFeedback = ref<{ turnKey: string; state: 'copied' | 'error' } | null>(null)

  let copyFeedbackTimer: ReturnType<typeof window.setTimeout> | null = null

  function turnCopyText(turn: TimelineTurn): string {
    return buildTurnCopyText(turn, plainAgentDirectives)
  }

  function clearCopyFeedback() {
    if (copyFeedbackTimer !== null) {
      window.clearTimeout(copyFeedbackTimer)
      copyFeedbackTimer = null
    }
    copyFeedback.value = null
  }

  function copyFeedbackState(turnKey: string): 'copied' | 'error' | null {
    return copyFeedback.value?.turnKey === turnKey ? copyFeedback.value.state : null
  }

  function copyFeedbackLabel(turnKey: string): string | null {
    const state = copyFeedbackState(turnKey)
    if (state === 'copied') return 'Copied'
    if (state === 'error') return 'Copy failed'
    return null
  }

  function copyTurnButtonLabel(turnKey: string): string {
    return copyFeedbackLabel(turnKey) ?? 'Copy conversation'
  }

  async function copyTurn(turn: TimelineTurn) {
    clearCopyFeedback()
    try {
      await writeClipboard(buildTurnCopyText(turn, plainAgentDirectives))
      copyFeedback.value = { turnKey: turn.key, state: 'copied' }
    } catch {
      copyFeedback.value = { turnKey: turn.key, state: 'error' }
    }
    copyFeedbackTimer = window.setTimeout(clearCopyFeedback, 1800)
  }

  // ── Edit-resend ──────────────────────────────────────────────────────────────
  // A user can edit one of their sent messages; after resending, the
  // conversation reruns from that point (truncating subsequent turns).
  //
  // This is a history rewrite, not a second send: the rules that decide when it
  // may be offered live in ``utils/chatEditResend`` so they are testable.

  const editingTurnKey = ref<string | null>(null)

  const editDraft = ref('')

  const isEditSending = ref(false)

  const editError = ref<string | null>(null)

  function isEditingTurn(turn: TimelineTurn): boolean {
    return editingTurnKey.value === turn.key
  }

  /** Whether the reader's editor may open for this turn, and why not. */
  function editGate(turn: TimelineTurn) {
    return {
      supportsEditResend: deps.supportsEditResend.value,
      providerOrigin: turn.providerOrigin,
      hasTurnId: Boolean(turn.turnId),
      unfinished: deps.turnInFlight.value,
      goalReason: deps.goalEditReason.value,
      preparingAttachments: deps.isPreparingEditAttachments.value,
    }
  }

  function editReason(turn: TimelineTurn): string | null {
    return editUnavailableReason(editGate(turn))
  }

  function startEdit(turn: TimelineTurn) {
    if (isEditSending.value) return
    // Refuse locally what the backend refuses authoritatively (409): a Hub
    // authored turn, a running turn, or a live Goal. The refusal is stated in
    // the editor instead of being swallowed, so a click that opens nothing
    // explains itself.
    if (!canOfferEdit(editGate(turn))) return
    const reason = editReason(turn)
    if (reason) {
      // Do not open an editor that cannot send. The hover button is already
      // disabled with this text, so this path is the keyboard/other-caller
      // backup; a notification keeps the refusal from being silent.
      editError.value = reason
      deps.terminalStore.pushNotification({
        type: 'warning',
        message: reason,
        autoDismissMs: 5000,
      })
      return
    }
    editingTurnKey.value = turn.key
    editDraft.value = turn.userText
    clearEditAttachments()
    editError.value = null
  }

  function clearEditAttachments() {
    // The controller bumps its own preparation epoch so a paste still being
    // read cannot append into the next edit's draft.
    deps.resetEditAttachments()
    deps.editAttachmentError.value = null
  }

  function cancelEdit() {
    if (isEditSending.value) return
    editingTurnKey.value = null
    editDraft.value = ''
    clearEditAttachments()
    editError.value = null
  }

  async function submitEdit(turn: TimelineTurn) {
    if (isEditSending.value) return
    const text = editDraft.value
    if (!text.trim()) {
      editError.value = 'Message cannot be empty'
      return
    }
    if (!turn.turnId) {
      editError.value = 'Cannot edit this turn (missing turn id)'
      return
    }
    if (deps.isPreparingEditAttachments.value) {
      editError.value = 'Wait for the image to finish preparing'
      return
    }
    isEditSending.value = true
    editError.value = null
    const clientTurnId = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
    try {
      const res = await fetch(`${deps.streamBasePath.value}/edit-resend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(editResendBody({
          text,
          clientTurnId,
          turnId: turn.turnId,
          attachments: deps.editAttachments.value,
        })),
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
      const result: { removed_turn_count?: number } | null = await res
        .json()
        .catch(() => null)
      // Success: clear editing state and rehydrate the stream from the
      // truncated event store.
      editingTurnKey.value = null
      editDraft.value = ''
      clearEditAttachments()
      // The rewrite removed the old turns, so a Stop optimism about a turn that
      // no longer exists would otherwise mint a phantom "Stopped" row on top of
      // the refreshed timeline.
      deps.optimisticCancelledTurnId.value = null
      deps.resetStream()
      void deps.start(deps.streamSourceId.value, deps.streamSource.value)
      // A rewrite deletes every later turn; saying so keeps that deletion from
      // reading as a rendering bug.
      const notice = removedTurnsNotice(result?.removed_turn_count)
      if (notice) {
        deps.terminalStore.pushNotification({
          type: 'success',
          message: notice,
          autoDismissMs: 6000,
        })
      }
    } catch (err) {
      editError.value = err instanceof Error ? err.message : 'Failed to resend message'
    } finally {
      isEditSending.value = false
    }
  }

  return {
    resolvedFeishuSourceAvatarUrl,
    feishuSourceIdentityKey,
    forkingOrdinal,
    implementingPlanKey,
    completedPlanText,
    implementPlan,
    forkFromTurn,
    copyFeedback,
    turnCopyText,
    clearCopyFeedback,
    copyFeedbackState,
    copyFeedbackLabel,
    copyTurnButtonLabel,
    copyTurn,
    editingTurnKey,
    editDraft,
    isEditSending,
    editError,
    editReason,
    /** The editor's own draft images: pasted in, removable before send. */
    editAttachments: deps.editAttachments,
    editAttachmentError: deps.editAttachmentError,
    isPreparingEditAttachments: deps.isPreparingEditAttachments,
    handleEditPaste: deps.handleEditPaste,
    removeEditAttachment: deps.removeEditAttachment,
    isEditingTurn,
    startEdit,
    cancelEdit,
    submitEdit,
  }
}
