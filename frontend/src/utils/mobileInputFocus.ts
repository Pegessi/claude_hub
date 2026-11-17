// Whether programmatic or automatic text-input focus should be suppressed to
// avoid popping up the mobile soft keyboard when opening a dialog or selector
// menu. Users on phones or coarse-pointer touch screens must explicitly tap a
// text input to bring up the virtual keyboard.
const MOBILE_VIEWPORT_BREAKPOINT_PX = 768

export const MODAL_OVERLAY_SELECTOR = [
  '.modal-overlay',
  '.workspace-modal-overlay',
  '.goal-dialog-backdrop',
  '.quota-modal-backdrop',
  '.task-detail-overlay',
  '.tam-overlay',
  '.subagent-sheet-backdrop',
  '.native-subagent-backdrop',
  '.inbox-sheet-backdrop',
  '.goal-sheet-backdrop',
  '.dot-overlay',
  '.archive-overlay',
].join(', ')

export const MODAL_SURFACE_SELECTOR = [
  '.modal',
  '.workspace-modal',
  '.goal-dialog',
  '.quota-modal',
  '.tam-modal',
  '.task-detail-panel',
  '.subagent-sheet',
  '.native-subagent-window',
  '.inbox-sheet',
  '.goal-status-details--sheet',
  '.dot-panel',
  '.archive-panel',
  'dialog[open]',
].join(', ')

export const EDITABLE_FIELD_SELECTOR = [
  'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="file"])',
  'textarea',
  'select',
  '[contenteditable]:not([contenteditable="false"])',
].join(', ')

const STICKY_MODAL_CHROME_SELECTOR = [
  '.modal-actions',
  '.file-browser-footer',
  '.tam-modal-actions',
  '.detail-header',
  '.st-footer',
  'header',
  'footer',
].join(', ')

export interface VerticalRect {
  top: number
  bottom: number
  height: number
}

export function shouldAutoFocusTextInput(win: Window | undefined = typeof window !== 'undefined' ? window : undefined): boolean {
  if (!win) return true
  if (typeof win.innerWidth === 'number' && win.innerWidth <= MOBILE_VIEWPORT_BREAKPOINT_PX) {
    return false
  }
  if (typeof win.matchMedia === 'function' && win.matchMedia('(any-pointer: coarse)').matches) {
    return false
  }
  return true
}

export function isMobileOrCoarseViewport(win: Window | undefined = typeof window !== 'undefined' ? window : undefined): boolean {
  return !shouldAutoFocusTextInput(win)
}

function isScrollableOverflow(overflowY: string): boolean {
  return overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay'
}

export function findModalScrollContainer(
  field: HTMLElement,
  modal: HTMLElement,
  win: Window = window,
): HTMLElement {
  let fallback: HTMLElement | null = null
  let current: HTMLElement | null = field.parentElement
  while (current) {
    const overflowY = win.getComputedStyle(current).overflowY
    if (isScrollableOverflow(overflowY)) {
      if (current.scrollHeight > current.clientHeight + 1) {
        return current
      }
      if (!fallback) {
        fallback = current
      }
    }
    if (current === modal) break
    current = current.parentElement
  }
  return fallback ?? modal
}

function collectModalScrollContainers(
  field: HTMLElement,
  modal: HTMLElement,
  win: Window = window,
): HTMLElement[] {
  const overflowing: HTMLElement[] = []
  const scrollables: HTMLElement[] = []
  let current: HTMLElement | null = field.parentElement
  while (current) {
    const overflowY = win.getComputedStyle(current).overflowY
    if (isScrollableOverflow(overflowY)) {
      scrollables.push(current)
      if (current.scrollHeight > current.clientHeight + 1) {
        overflowing.push(current)
      }
    }
    if (current === modal) break
    current = current.parentElement
  }
  if (!scrollables.includes(modal) && modal.scrollHeight > modal.clientHeight + 1) {
    overflowing.push(modal)
  }
  if (overflowing.length > 0) {
    return overflowing.reverse()
  }
  if (scrollables.length > 0) {
    return scrollables.reverse()
  }
  return [modal]
}

function resolveVisibleContainerBounds(
  scrollContainer: HTMLElement,
  modal: HTMLElement,
): { top: number; bottom: number } {
  const rawRect = scrollContainer.getBoundingClientRect()
  if (scrollContainer === modal) {
    return { top: rawRect.top, bottom: rawRect.bottom }
  }
  const modalRect = modal.getBoundingClientRect()
  if (modalRect.bottom <= modalRect.top) {
    return { top: rawRect.top, bottom: rawRect.bottom }
  }
  const clampedTop = Math.max(rawRect.top, modalRect.top)
  const clampedBottom = Math.min(rawRect.bottom, modalRect.bottom)
  if (clampedBottom > clampedTop) {
    return { top: clampedTop, bottom: clampedBottom }
  }
  return { top: modalRect.top, bottom: modalRect.bottom }
}

export function computeModalFieldScrollDelta(
  fieldRect: VerticalRect,
  containerRect: { top: number; bottom: number },
  stickyRegions: VerticalRect[] = [],
  padding = 12,
): number {
  let visibleTop = containerRect.top
  let visibleBottom = containerRect.bottom
  const containerMidY = (containerRect.top + containerRect.bottom) / 2

  for (const region of stickyRegions) {
    if (region.height <= 0) continue
    const midY = (region.top + region.bottom) / 2
    if (midY >= containerMidY) {
      if (region.top < visibleBottom && region.bottom >= containerRect.bottom - 32) {
        visibleBottom = Math.min(visibleBottom, region.top)
      }
    } else if (region.bottom > visibleTop && region.top <= containerRect.top + 32) {
      visibleTop = Math.max(visibleTop, region.bottom)
    }
  }

  const availableHeight = visibleBottom - visibleTop
  if (availableHeight <= 0) return 0

  if (fieldRect.height > availableHeight) {
    return fieldRect.top - (visibleTop + 4)
  }

  const margin = Math.min(padding, Math.max(0, (availableHeight - fieldRect.height) / 2))
  if (fieldRect.top < visibleTop + margin) {
    return fieldRect.top - (visibleTop + margin)
  }
  if (fieldRect.bottom > visibleBottom - margin) {
    return fieldRect.bottom - (visibleBottom - margin)
  }
  return 0
}

export function syncModalViewportScroll(
  doc: Document | undefined = typeof document !== 'undefined' ? document : undefined,
  win: Window | undefined = typeof window !== 'undefined' ? window : undefined,
): boolean {
  if (!doc || !win) return false

  const overlays = doc.querySelectorAll<HTMLElement>(MODAL_OVERLAY_SELECTOR)
  for (const overlay of overlays) {
    if (overlay.scrollTop !== 0) overlay.scrollTop = 0
    if (overlay.scrollLeft !== 0) overlay.scrollLeft = 0
  }

  const modals = doc.querySelectorAll<HTMLElement>(MODAL_SURFACE_SELECTOR)
  if ((modals.length > 0 || overlays.length > 0) && isMobileOrCoarseViewport(win)) {
    if (win.scrollY !== 0 || win.scrollX !== 0) {
      win.scrollTo(0, 0)
    }
    if (doc.documentElement && (doc.documentElement.scrollTop !== 0 || doc.documentElement.scrollLeft !== 0)) {
      doc.documentElement.scrollTop = 0
      doc.documentElement.scrollLeft = 0
    }
    if (doc.body && (doc.body.scrollTop !== 0 || doc.body.scrollLeft !== 0)) {
      doc.body.scrollTop = 0
      doc.body.scrollLeft = 0
    }
  }

  const active = doc.activeElement
  if (!(active instanceof HTMLElement) || !active.matches(EDITABLE_FIELD_SELECTOR)) {
    return false
  }

  const modal = active.closest<HTMLElement>(MODAL_SURFACE_SELECTOR)
  if (!modal) return false

  const scrollContainers = collectModalScrollContainers(active, modal, win)
  let didScrollAny = false

  for (const scrollContainer of scrollContainers) {
    const stickyCandidates = scrollContainer.querySelectorAll<HTMLElement>(STICKY_MODAL_CHROME_SELECTOR)
    const stickyRegions: VerticalRect[] = []
    for (const stickyEl of stickyCandidates) {
      if (stickyEl.contains(active)) continue
      const position = win.getComputedStyle(stickyEl).position
      if (position !== 'sticky' && position !== 'fixed') continue
      const rect = stickyEl.getBoundingClientRect()
      if (rect.height > 0) {
        stickyRegions.push({ top: rect.top, bottom: rect.bottom, height: rect.height })
      }
    }

    const fieldRect = active.getBoundingClientRect()
    const containerBounds = resolveVisibleContainerBounds(scrollContainer, modal)
    const delta = computeModalFieldScrollDelta(
      { top: fieldRect.top, bottom: fieldRect.bottom, height: fieldRect.height },
      containerBounds,
      stickyRegions,
    )

    if (Math.abs(delta) < 1) continue

    const maxScrollTop = Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight)
    const nextScrollTop = Math.max(0, Math.min(maxScrollTop, Math.round(scrollContainer.scrollTop + delta)))
    if (nextScrollTop === scrollContainer.scrollTop) continue

    scrollContainer.scrollTop = nextScrollTop
    didScrollAny = true
  }

  return didScrollAny
}
