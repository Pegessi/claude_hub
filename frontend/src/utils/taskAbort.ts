export const DEFAULT_ABORT_REASON = 'Manual abort from workspace UI'

export function resolveAbortReason(promptValue: string | null): string | null {
  if (promptValue === null) {
    return null
  }
  return promptValue.trim() || DEFAULT_ABORT_REASON
}
