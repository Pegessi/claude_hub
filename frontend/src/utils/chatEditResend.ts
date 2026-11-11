/** Rules for editing one already-sent user message and resending it.
 *
 *  Resend is a history rewrite: the backend truncates the Hub event store and
 *  the provider transcript at that turn and reruns from the edited text (see
 *  ``services/agent_stream/runtime/edit_resend.py``). Every rule here exists to
 *  keep the offered form honest about what that rewrite can actually do:
 *
 *  - A turn the Hub wrote (a Goal continuation, a restart continuation, a
 *    workspace or schedule turn) sent a *different* text to the provider than
 *    the summary the reader sees, so it cannot be replayed as the reader's own
 *    message. Such turns must never offer the form.
 *  - A turn the reader wrote is stored redacted: over 4000 characters are cut
 *    with a truncation marker and secret-looking tokens are masked. The stored
 *    copy is then not the message that was sent, so the reader has to be told
 *    before they edit a partial text — otherwise a successful resend would
 *    quietly send a mangled message.
 *  - The fork cuts at the edited turn, so the resend removes every later turn.
 *    The count comes back from the backend and is reported, because a silent
 *    history deletion reads as a bug.
 */

import { toStreamSendAttachment } from '@/utils/agentStreamAttachments'

/** Origins whose provider text was written by the Hub, not by the reader.
 *
 *  Mirrors the backend's refusal list in ``edit_resend``; a missing origin is a
 *  legacy reader turn and stays editable. */
const HUB_AUTHORED_ORIGINS = new Set([
  'goal',
  'workspace',
  'restart_continuation',
  'question_answer',
  'scheduled_task',
])

export interface EditOffer {
  /** Provider/session support for rewriting past turns. */
  supportsEditResend: boolean
  /** Raw ``metadata.origin`` of the turn, or ``null`` for legacy turns. */
  providerOrigin: string | null
  hasTurnId: boolean
}

/** Whether the inline editor may be offered at all.
 *
 *  Hidden rather than disabled where the backend would refuse: a disabled
 *  control that can never enable is worse than no control. */
export function canOfferEdit(offer: EditOffer): boolean {
  if (!offer.supportsEditResend || !offer.hasTurnId) return false
  return !(offer.providerOrigin !== null && HUB_AUTHORED_ORIGINS.has(offer.providerOrigin))
}

export interface EditGate {
  supportsEditResend: boolean
  providerOrigin: string | null
  hasTurnId: boolean
  /** A turn is running, so the backend would refuse to rewrite history (409). */
  unfinished: boolean
  /** The Goal owns this conversation until it settles. */
  goalReason: string | null
  /** An image is still being read/generated for the editor. */
  preparingAttachments: boolean
}

/** Why the editor cannot open right now, or ``null`` when it can. */
export function editUnavailableReason(gate: EditGate): string | null {
  if (!gate.supportsEditResend) return 'Editing past turns is unavailable in this Chat'
  if (!gate.hasTurnId) return 'This turn has no id to edit'
  if (gate.providerOrigin !== null && HUB_AUTHORED_ORIGINS.has(gate.providerOrigin)) {
    return 'This turn was written by the Goal/Hub, not by you'
  }
  if (gate.unfinished) return 'A turn is currently running'
  if (gate.goalReason) return gate.goalReason
  if (gate.preparingAttachments) return 'Wait for the image to finish preparing'
  return null
}

/** What to tell the reader when the stored copy of their message is partial.
 *
 *  ``stored_text_incomplete`` is set by the backend when redaction changed the
 *  summary it persisted (truncation at 4000 characters or a masked
 *  secret-looking token). The provider still has the full text, so the resend
 *  itself works; only the prefill is partial. */
export function storedCopyWarning(incomplete: boolean): string | null {
  if (!incomplete) return null
  return 'The Hub stores only a partial copy of this message (it was shortened or '
    + 'masked as a secret). Resending sends exactly the text below — restore anything '
    + 'missing before you send.'
}

/** Report what the rewrite removed, so a truncation is never silent. */
export function removedTurnsNotice(removedTurnCount: number | null | undefined): string | null {
  if (typeof removedTurnCount !== 'number' || !Number.isFinite(removedTurnCount)) return null
  if (removedTurnCount <= 0) return null
  return removedTurnCount === 1
    ? 'Message resent · 1 later turn was removed'
    : `Message resent · ${removedTurnCount} later turns were removed`
}

/** Body for ``POST /stream/edit-resend``.
 *
 *  ``attachments`` carries only images the reader added while editing; the
 *  backend re-sends the original turn's images by itself. The wire form is
 *  ``toStreamSendAttachment``, not a copy of the draft: when the preview *is*
 *  the original it must travel as ``preview_is_original`` instead of
 *  repeating the whole base64 payload, or every pasted image is uploaded twice
 *  and counted twice against the request cap — a set the editor accepted can
 *  then be rejected with HTTP 400. */
/** The editor's staged image, in the shape the composer drafts already use. */
interface EditResendAttachmentInput {
  filename: string
  mime_type: string
  data_url: string
  preview_data_url?: string
}

export function editResendBody(input: {
  text: string
  clientTurnId: string
  turnId: string
  attachments: readonly EditResendAttachmentInput[]
}) {
  return {
    text: input.text,
    client_turn_id: input.clientTurnId,
    turn_id: input.turnId,
    attachments: input.attachments.map(toStreamSendAttachment),
  }
}
