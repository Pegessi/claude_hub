export interface PendingMessageReceipt {
  message_id: string
  state: 'queued' | 'waiting' | 'dispatching' | 'uncertain'
  waiting_reason: string | null
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
