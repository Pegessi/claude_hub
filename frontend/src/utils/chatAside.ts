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
  /** Ordinal of the newest turn the fork should keep, or ``null`` when the
   *  conversation has none. */
  ordinal: number | null
  busy: boolean
}

export function asideUnavailableReason(gate: AsideGate): string | null {
  if (gate.isManagedChat) return 'Asking aside needs a local Chat'
  if (gate.busy) return 'Asking aside…'
  if (gate.ordinal === null) return 'Send a message first, then ask aside'
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
