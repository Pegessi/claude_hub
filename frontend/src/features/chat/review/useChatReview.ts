import { computed, ref, type Ref } from 'vue'
import { createComposerDraftId } from '@/utils/chatComposerInteraction'
import {
  formatReviewMessage,
  reviewNote,
  type ReviewAnchor,
  type ReviewComment,
} from './reviewComments'
import type { QueuedChatDraft } from '@/stores/terminalStore'

/** Only the two calls a tray needs; injected so a unit test can watch writes. */
export interface ReviewStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

interface Dependencies {
  /** Per-tab identity: comments and their stored copy are scoped to it. */
  tabKey: string
  /** The composer's durable queue: a review message is an ordinary message. */
  draftQueue: Ref<QueuedChatDraft[]>
  /** Defaults to sessionStorage; null disables persistence. */
  storage?: ReviewStorage | null
}

const STORAGE_PREFIX = 'chub-review:'

/** A short-lived local review list, not conversation history: sessionStorage
 *  only keeps it across a Chat remount and never leaves this browser. Blocked
 *  storage (private mode) must not break the tray. */
function browserStorage(): ReviewStorage | null {
  try {
    if (typeof window === 'undefined') return null
    const storage = (window as Window).sessionStorage
    return storage ?? null
  } catch {
    return null
  }
}

function storedComments(storage: ReviewStorage | null, key: string): ReviewComment[] {
  if (!storage) return []
  try {
    const raw = storage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    const list = Array.isArray(parsed) ? parsed : (parsed as { comments?: unknown })?.comments
    if (!Array.isArray(list)) return []
    // Anything unexpected (older shape, hand-edited value) is dropped rather
    // than rendered: the text is what the user wrote, the rest is plumbing.
    return list
      .filter((item): item is ReviewComment => Boolean(
        item && typeof item === 'object' &&
        typeof (item as ReviewComment).id === 'string' &&
        typeof (item as ReviewComment).text === 'string' &&
        typeof (item as ReviewComment).file === 'string' &&
        typeof (item as ReviewComment).key === 'string' &&
        typeof (item as ReviewComment).tool === 'string' &&
        typeof (item as ReviewComment).row === 'number',
      ))
      // Whether the row still exists is learned from the live timeline, never
      // from a stored verdict.
      .map(item => ({ ...item, stale: false }))
  } catch {
    return []
  }
}

function storeComments(storage: ReviewStorage | null, key: string, comments: readonly ReviewComment[]): void {
  if (!storage) return
  try {
    if (!comments.length) storage.setItem(key, '')
    else {
      // Whether the row still exists describes the live timeline, not the note.
      const persisted = comments.map((comment) => {
        const copy: Record<string, unknown> = { ...comment }
        delete copy.stale
        return copy
      })
      storage.setItem(key, JSON.stringify({ comments: persisted }))
    }
  } catch {
    // Quota or a blocked store: the in-memory list still works.
  }
}

export function useChatReview(deps: Dependencies) {
  const storage = deps.storage === undefined ? browserStorage() : deps.storage
  const storageKey = `${STORAGE_PREFIX}${deps.tabKey}`
  const comments = ref<ReviewComment[]>(storedComments(storage, storageKey))
  /** Which row the tray asked to reveal; a nonce lets a repeat click re-fire. */
  const focusTarget = ref<{ key: string; nonce: number } | null>(null)
  let focusNonce = 0

  const count = computed(() => comments.value.length)
  const isEmpty = computed(() => comments.value.length === 0)

  function persist(): void {
    storeComments(storage, storageKey, comments.value)
  }

  /** One comment per row: a second note on the same row edits the first, and
   *  widens its range when the reader selected one. */
  function addComment(anchor: ReviewAnchor, text: string): ReviewComment | null {
    const note = reviewNote(text)
    if (!note) return null
    const existing = comments.value.find(comment => comment.key === anchor.key)
    if (existing) {
      existing.text = note
      existing.stale = false
      existing.file = anchor.file
      existing.line = anchor.line
      existing.endLine = anchor.endLine
      existing.code = anchor.code
      persist()
      return existing
    }
    const comment: ReviewComment = { ...anchor, id: createComposerDraftId(), text: note, stale: false }
    comments.value = [...comments.value, comment]
    persist()
    return comment
  }

  function updateComment(id: string, text: string): void {
    const note = reviewNote(text)
    const comment = comments.value.find(item => item.id === id)
    if (!comment || !note || comment.text === note) return
    comment.text = note
    persist()
  }

  function removeComment(id: string): void {
    if (!comments.value.some(item => item.id === id)) return
    comments.value = comments.value.filter(item => item.id !== id)
    persist()
  }

  function clearComments(): void {
    if (!comments.value.length) return
    comments.value = []
    persist()
  }

  /**
   * Record whether a stored anchor can still be reached.
   *
   * A warm-cache history reload renumbers turn and part keys, so a saved
   * comment can outlive its row. The tray asserts the verdict when it fails to
   * reveal the row and clears it when the row is found again; it stays local
   * because it describes the current timeline, not the note.
   */
  function setAnchorStale(key: string, stale = true): void {
    const comment = comments.value.find(item => item.key === key)
    if (!comment || Boolean(comment.stale) === stale) return
    comment.stale = stale
  }

  function requestFocus(key: string): void {
    focusNonce += 1
    focusTarget.value = { key, nonce: focusNonce }
  }

  /**
   * Send the whole tray as ONE message.
   *
   * It enters the composer's draft queue rather than calling the transport
   * directly: a busy turn then queues it exactly like typed input, the queue
   * owns retry/idempotency, and the message stays visible and cancellable
   * until it is accepted.
   */
  function sendReview(): string {
    const message = formatReviewMessage(comments.value)
    if (!message) return ''
    deps.draftQueue.value.push({
      id: createComposerDraftId(),
      message,
      attachments: [],
    })
    clearComments()
    return message
  }

  return {
    comments,
    count,
    isEmpty,
    focusTarget,
    persist,
    addComment,
    updateComment,
    removeComment,
    clearComments,
    setAnchorStale,
    requestFocus,
    sendReview,
  }
}
