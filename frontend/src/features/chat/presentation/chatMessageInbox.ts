export interface PendingMessageReceipt {
  message_id: string
  state: 'queued' | 'waiting' | 'dispatching' | 'uncertain'
  waiting_reason: string | null
}

export function messageInboxReadError(status: number | undefined, timedOut: boolean): string {
  if (timedOut) return 'The message status request timed out after 10 seconds.'
  if (status === 401) return 'Sign-in is required (HTTP 401). Refresh the page to check your session.'
  if (status === 403) return 'Access was denied (HTTP 403). Check your sign-in and permissions.'
  if (status !== undefined) {
    if (status >= 200 && status < 300) return 'Hub returned an unreadable message status response.'
    return `The message status request failed (HTTP ${status}).`
  }
  return 'Could not reach Hub. Check your network connection.'
}

/** Receipts are operational status, never synthetic transcript turns. */
export function pendingMessageReceipts(value: unknown): PendingMessageReceipt[] {
  if (!value || typeof value !== 'object' || !('messages' in value) || !Array.isArray(value.messages)) return []
  return value.messages.flatMap((row: unknown): PendingMessageReceipt[] => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return []
    const receipt = row as Record<string, unknown>
    const state = receipt.state
    if (typeof receipt.message_id !== 'string' || !receipt.message_id ||
      (state !== 'queued' && state !== 'waiting' && state !== 'dispatching' && state !== 'uncertain')) return []
    return [{ message_id: receipt.message_id, state,
      waiting_reason: typeof receipt.waiting_reason === 'string' ? receipt.waiting_reason : null }]
  }).slice(0, 100)
}
