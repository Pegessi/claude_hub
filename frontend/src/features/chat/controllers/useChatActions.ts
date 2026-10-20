import { type Ref, type ComputedRef, ref } from 'vue'
import { type TimelineTurn } from '../timeline/types'
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

  const editingTurnKey = ref<string | null>(null)

  const editDraft = ref('')

  const isEditSending = ref(false)

  const editError = ref<string | null>(null)

  function isEditingTurn(turn: TimelineTurn): boolean {
    return editingTurnKey.value === turn.key
  }

  function startEdit(turn: TimelineTurn) {
    if (isEditSending.value) return
    // Refuse to edit while a turn is running.  The backend guard is
    // authoritative (409); this just avoids a round-trip and keeps the button
    // and the action in sync.
    if (deps.turnInFlight.value) return
    if (deps.goalEditReason.value) {
      editError.value = deps.goalEditReason.value
      return
    }
    editingTurnKey.value = turn.key
    editDraft.value = turn.userText
    editError.value = null
  }

  function cancelEdit() {
    if (isEditSending.value) return
    editingTurnKey.value = null
    editDraft.value = ''
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
        body: JSON.stringify({
          text,
          client_turn_id: clientTurnId,
          turn_id: turn.turnId,
        }),
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
      // Success: clear editing state and rehydrate the stream from the
      // truncated event store.
      editingTurnKey.value = null
      editDraft.value = ''
      deps.resetStream()
      void deps.start(deps.streamSourceId.value, deps.streamSource.value)
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
    isEditingTurn,
    startEdit,
    cancelEdit,
    submitEdit,
  }
}
