/** "Ask aside": fork the current Chat and put a question in the fork, leaving
 *  the conversation on screen untouched.
 *
 *  Why a fork and not a second message: a turn that is already running cannot
 *  take a second question — the composer queues or steers it, both of which
 *  change the answer the main thread is producing. A fork is the only path that
 *  asks something without touching the turn in flight, and the fork already
 *  carries the copied transcript plus the seeded context the backend prepends to
 *  its first provider turn (see ``services/agent_stream/fork_seed.py``), so the
 *  aside question is answered with the same prior context.
 *
 *  The backend fork endpoint takes only ``ordinal`` — it cannot accept the
 *  question — so the aside is delivered as the fork's first ordinary turn
 *  through the same ``/stream/send`` contract the composer uses. Everything
 *  here is pure request building; the caller owns the effects.
 */

/** One aside question delivered to a forked tab. */
export interface AsideSendRequest {
  path: string
  body: {
    client_turn_id: string
    text: string
    delivery: 'normal'
    attachments: []
  }
}

/** ``/api/workspaces/tabs/<id>/stream/send`` for the forked tab.
 *
 *  ``delivery: 'normal'`` on purpose: the fork is a fresh session with no turn
 *  in flight, and a steer there would be a lie about what is being cancelled. */
export function buildAsideSendRequest(
  forkedTabId: string,
  text: string,
  clientTurnId: string,
): AsideSendRequest {
  return {
    path: `/api/workspaces/tabs/${encodeURIComponent(forkedTabId)}/stream/send`,
    body: {
      client_turn_id: clientTurnId,
      text: text.trim(),
      delivery: 'normal',
      attachments: [],
    },
  }
}

/** Stable turn identity for the aside turn, matching the composer's fallback. */
export function asideClientTurnId(): string {
  return typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
}

/** Why the aside action is unavailable, or ``null`` when it can run.
 *
 *  Kept pure so the composer's enablement rule is testable without mounting it:
 *  every reason here is a state a real user hits (a Managed Chat cannot be
 *  forked, an empty composer has no question to ask, images cannot be carried
 *  into the fork, an empty Chat has no turn to cut at). */
export interface AsideGate {
  isManagedChat: boolean
  question: string
  attachmentCount: number
  /** Ordinal of the last completed turn to cut at, or ``null`` when there is
   *  none yet. */
  ordinal: number | null
  /** Whether the conversation has turns at all, completed or not. An in-flight
   *  first turn is "not yet", not "nothing to ask about". */
  hasTurns: boolean
  busy: boolean
}

export function asideUnavailableReason(gate: AsideGate): string | null {
  if (gate.isManagedChat) return 'Asking aside needs a local Chat'
  if (gate.busy) return 'Asking aside…'
  if (gate.ordinal === null) {
    return gate.hasTurns
      ? 'Ask aside after this turn finishes'
      : 'Send a message first, then ask aside'
  }
  if (gate.attachmentCount > 0) return 'Ask aside sends text only — remove attachments first'
  if (!gate.question.trim()) return 'Type a question to ask aside'
  return null
}

/** Whether the aside control should be shown at all.
 *
 *  Hidden in Managed Chat: the backend refuses to fork managed tabs, so a
 *  control there could only fail. */
export function canOfferAside(isManagedChat: boolean): boolean {
  return !isManagedChat
}

/** Ordinal the fork should cut at: the last **completed** turn.
 *
 *  ``fork_tab`` copies the events of turns ``0..ordinal`` as-is. Cutting at a
 *  turn that is still running copies its ``turn_started`` and
 *  ``tool_call_started`` events without the completions that would follow, and
 *  the fork's provider session is a fresh one that will never finish them — so
 *  the fork opens on tool cards that spin forever and a question card nobody
 *  can answer. Stopping one turn earlier costs nothing: the aside question gets
 *  the same copied context, and an in-flight turn's answer is not something the
 *  fork could have used anyway.
 *
 *  ``null`` when no turn has completed yet: there is nothing coherent to cut
 *  at, and the caller reports why instead of creating a half turn. */
export function asideForkOrdinal(turns: readonly { completed: boolean }[]): number | null {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (turns[index].completed) return index
  }
  return null
}

/** Where the reader has to be sent when an aside fails, and what to tell them.
 *
 *  Forking switches the pane to the new tab (``terminalStore.forkTab``). If the
 *  follow-up send then fails — a 409/503 while the forked session is still
 *  warming up — the reader is looking at an empty fork, while the question they
 *  typed still lives in the composer of the Chat they came from. The failure
 *  therefore moves the pane *back* before the message can mean anything.
 *
 *  The move respects an explicit switch: if the reader has already chosen
 *  another tab in the meantime, yanking them back would override that choice,
 *  so nothing is restored — and then the message cannot say "here" either. It
 *  has to name the Chat that still holds the question, because the reader is no
 *  longer standing in it. */
export interface AsideFailure {
  /** Tab to make active again, or ``null`` when the pane should stay put. */
  restoreTabId: string | null
  message: string
}

export interface AsideFailureInput {
  sourceTabId: string
  /** Display name of the Chat the question came from; empty when unknown. */
  sourceTabName: string
  forkedTabId: string | null
  activeTabId: string | null
  detail: string
}

export function asideFailure(input: AsideFailureInput): AsideFailure {
  const { sourceTabId, forkedTabId, activeTabId, detail } = input
  const source = input.sourceTabName.trim() || 'the Chat you asked this in'
  if (forkedTabId === null) {
    // Nothing moved: the reader is still in the composer that holds the
    // question, so there is nothing to explain beyond the failure itself.
    return { restoreTabId: null, message: `Ask aside failed: ${detail}` }
  }
  if (activeTabId === forkedTabId) {
    return {
      restoreTabId: sourceTabId,
      message: `Ask aside failed: ${detail}. Back in ${source} — your question is still in the composer.`,
    }
  }
  return {
    restoreTabId: null,
    message: `Ask aside failed: ${detail}. Your question is still in the composer of ${source}.`,
  }
}
