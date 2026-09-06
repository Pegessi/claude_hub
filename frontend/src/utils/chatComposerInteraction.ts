export type ComposerEnterAction = 'send' | 'queue' | 'steer' | 'newline' | 'ignore'

export interface ComposerEnterContext {
  isComposing: boolean
  shiftKey: boolean
  metaKey: boolean
  ctrlKey: boolean
  altKey: boolean
  turnInFlight: boolean
  hasDraft: boolean
  /**
   * Mobile soft keyboards have no Shift/⌘/Ctrl modifiers, so Enter can only
   * insert a newline; sending is always via the Send button. Optional so
   * existing desktop callers and tests are unchanged.
   */
  isMobile?: boolean
}

/** Resolve the default Enter key behavior for the structured composer. */
export function resolveComposerEnterAction(ctx: ComposerEnterContext): ComposerEnterAction {
  if (ctx.isComposing) return 'ignore'
  // Mobile: Enter always inserts a newline. There is no modifier key to
  // distinguish send/queue/steer, so sending is only possible via the Send
  // button. This stays after the isComposing check so IME input (e.g. CJK)
  // still takes precedence on mobile.
  if (ctx.isMobile) return 'newline'
  if (ctx.shiftKey || ctx.altKey) return 'newline'
  if ((ctx.metaKey || ctx.ctrlKey) && ctx.hasDraft) {
    return ctx.turnInFlight ? 'steer' : 'send'
  }
  if (ctx.turnInFlight) return 'queue'
  return 'send'
}

export const COMPOSER_TEXTAREA_MAX_HEIGHT_PX = 240

/** Grow a textarea up to ``maxHeightPx``, then scroll internally. */
export function autoresizeComposerTextarea(
  el: HTMLTextAreaElement | null | undefined,
  maxHeightPx: number = COMPOSER_TEXTAREA_MAX_HEIGHT_PX,
): void {
  if (!el) return
  el.style.height = 'auto'
  const next = Math.min(el.scrollHeight, maxHeightPx)
  el.style.height = `${next}px`
  el.style.overflowY = el.scrollHeight > maxHeightPx ? 'auto' : 'hidden'
}
