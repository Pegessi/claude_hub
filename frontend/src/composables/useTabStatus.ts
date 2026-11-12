import { computed } from 'vue'
import type { Ref } from 'vue'
import type { AgentRuntimeStatus, TerminalAgentStatus, TerminalTab } from '@/types'

const TAB_STATUS_LABELS: Record<AgentRuntimeStatus, string> = {
  idle: 'Idle',
  working: 'Working',
  attention: 'Needs attention',
  offline: 'Offline',
}

/**
 * Details that only restate the state. The backend reports them so a bare
 * payload still reads as a sentence, but "Working — Responding" says nothing a
 * user can act on, so the label stays "Working" and lets a concrete activity
 * (a running tool) carry the detail.
 */
const REDUNDANT_DETAILS = new Set(['responding', 'ready'])

/**
 * A card waiting on the user is not work in progress.
 *
 * The provider may still hold the turn open (its tool call is not finished, so
 * the status poll keeps reporting `working`), but nothing advances until the
 * user answers. The inbox already gives a pending question priority over the
 * `working` bucket for that reason; the sidebar and the TabBar must agree, or
 * the same session reads "Working" beside a card that asks for a decision.
 * `pending_question` is therefore presented as finished work that is waiting.
 *
 * It does not outrank a failed or dead provider, though. `_native_runtime_snapshot`
 * decides `offline` (hard failure, native error, transport error) *before*
 * `turn_in_flight`, and `attention` (a turn that exited non-zero) *after* it, so
 * `pending_question` rides on both (the card's epoch survives, and only
 * retirement clears it). Against a dead transport a native card cannot be
 * answered at all (`question_snapshot` needs `turn_in_flight`), and a follow-up
 * answer is refused while the provider still owns the turn (`409 question_busy`)
 * or once the turn ended unsuccessfully. So the failure keeps its colour and
 * label and merely adds "— a question is waiting": nothing is hidden, and no
 * answer is promised before it can be delivered.
 */
const AWAITING_ANSWER_LABEL = 'Waiting for your answer'
const QUESTION_WAITING_SUFFIX = 'a question is waiting'
const DEMOTED_STATUSES: ReadonlySet<AgentRuntimeStatus> = new Set(['working', 'idle'])

/**
 * Shared tab-status logic: index the backend-reported agent statuses by tab id,
 * resolve a tab's runtime status (with a fallback), and build a human label.
 * Used by both the TabBar and the session sidebar so the logic lives in one
 * place and cannot drift between the two.
 */
export function useTabStatus(agentStatuses: Ref<readonly TerminalAgentStatus[]>) {
  const tabStatusById = computed<Record<string, TerminalAgentStatus>>(() => {
    const map: Record<string, TerminalAgentStatus> = {}
    for (const s of agentStatuses.value) {
      map[s.tab_id] = s
    }
    return map
  })

  function reportedStatus(tab: TerminalTab): TerminalAgentStatus | undefined {
    return tabStatusById.value[tab.id]
  }

  function isAwaitingAnswer(tab: TerminalTab): boolean {
    const reported = reportedStatus(tab)
    return Boolean(reported?.pending_question && DEMOTED_STATUSES.has(reported.status))
  }

  /** A card is waiting even though the provider itself failed or went away. */
  function isQuestionWaitingBehindFailure(tab: TerminalTab): boolean {
    const reported = reportedStatus(tab)
    return Boolean(reported?.pending_question && !DEMOTED_STATUSES.has(reported.status))
  }

  function getTabStatus(tab: TerminalTab): AgentRuntimeStatus {
    const reported = tabStatusById.value[tab.id]
    // A blocked question outranks the provider's own turn flag: the answer is
    // what moves the session, so the row must not pulse as activity.
    if (isAwaitingAnswer(tab)) return 'idle'
    return reported?.status ?? (
      tab.session_kind === 'chat' ? 'offline' : tab.is_active ? 'idle' : 'offline'
    )
  }

  // Returns the status label, plus any backend `status_text` detail:
  // "Working", or "Working — <detail>".
  function getTabStatusLabel(tab: TerminalTab): string {
    if (isAwaitingAnswer(tab)) return AWAITING_ANSWER_LABEL
    if (isQuestionWaitingBehindFailure(tab)) {
      // Both facts, so neither the failure nor the card is hidden.
      return `${TAB_STATUS_LABELS[getTabStatus(tab)]} — ${QUESTION_WAITING_SUFFIX}`
    }
    const status = getTabStatus(tab)
    const statusLabel = TAB_STATUS_LABELS[status]
    const detail = reportedStatus(tab)?.status_text?.trim()
    if (!detail || detail.toLocaleLowerCase() === statusLabel.toLocaleLowerCase()) {
      return statusLabel
    }
    // A concrete activity ("Running pytest -q") is worth appending; a
    // restatement of the state is not.
    if (REDUNDANT_DETAILS.has(detail.toLocaleLowerCase())) return statusLabel
    return `${statusLabel} — ${detail}`
  }

  return { getTabStatus, getTabStatusLabel, isAwaitingAnswer }
}
