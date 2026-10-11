/**
 * Pure geometry + state helpers for the mobile left-edge session drawer.
 *
 * The DOM/gesture wiring lives in `useEdgeSwipeDrawer`; nothing in this file
 * touches `window`/`document`, so every branch is unit-testable under
 * node:test without a DOM.
 */

/** Width of the invisible left-edge gesture zone (px). Suggested 16-24. */
export const EDGE_BAND_PX = 20

/** Movement required before a gesture commits to horizontal vs vertical. */
export const EDGE_DIRECTION_SLOP_PX = 8

/** Fraction of the drawer width a drag must travel to snap open/closed. */
export const EDGE_COMMIT_RATIO = 1 / 3

/**
 * Fast-swipe velocity threshold in px/ms. A fling commits regardless of
 * distance (250px in 500ms ≈ 0.5 px/ms feels like a deliberate flick).
 */
export const EDGE_FLING_PX_PER_MS = 0.5

/** Main-surface swipes need more travel than the edge band so taps keep their jitter. */
export const SURFACE_DIRECTION_SLOP_PX = 12

/**
 * A main-surface swipe must be this many times more horizontal than vertical,
 * so diagonal reading scrolls stay scrolls.
 */
export const SURFACE_HORIZONTAL_BIAS = 1.5

export type EdgeIntent = 'horizontal' | 'vertical' | 'undecided'

export type SurfaceIntent = 'open' | 'ignore' | 'undecided'

/**
 * Did the gesture begin inside the left edge band?
 * `x < 0` (touches reported off-frame) is treated as the edge.
 */
export function isWithinEdgeBand(
  x: number,
  edgeBandPx: number = EDGE_BAND_PX,
): boolean {
  if (!Number.isFinite(x)) return false
  return x <= edgeBandPx
}

/**
 * Decide gesture orientation once the slop is crossed.
 * Horizontal wins ties so an edge fling is never eaten by the surface below;
 * vertical scrolls/selection still win clearly-diagonal drags.
 */
export function classifyIntent(
  dx: number,
  dy: number,
  slopPx: number = EDGE_DIRECTION_SLOP_PX,
): EdgeIntent {
  const absX = Math.abs(dx)
  const absY = Math.abs(dy)
  if (absX < slopPx && absY < slopPx) return 'undecided'
  if (absX >= absY) return 'horizontal'
  return 'vertical'
}

/**
 * Classify a swipe that began on the main content surface while the drawer is
 * closed. Only a clearly rightward swipe opens; vertical and leftward swipes
 * stay with the content.
 */
export function classifySurfaceIntent(dx: number, dy: number): SurfaceIntent {
  const absX = Math.abs(dx)
  const absY = Math.abs(dy)
  if (absX < SURFACE_DIRECTION_SLOP_PX && absY < SURFACE_DIRECTION_SLOP_PX) return 'undecided'
  return dx > 0 && absX >= absY * SURFACE_HORIZONTAL_BIAS ? 'open' : 'ignore'
}

/**
 * A rightward swipe over a horizontal scroller that is away from its start
 * scrolls that content back instead of opening the drawer.
 */
export function scrollerKeepsSurfaceSwipe(scrollLeft: number, overflowX: string): boolean {
  return scrollLeft > 0 && (overflowX === 'auto' || overflowX === 'scroll')
}

/**
 * Follow-finger translateX for the drawer panel, clamped to [-width, 0]:
 * closed + rightward drag eases it in; open + leftward drag eases it out.
 */
export function dragTranslatePx(
  startOpen: boolean,
  dx: number,
  drawerWidth: number,
): number {
  if (!(drawerWidth > 0)) return startOpen ? 0 : -Infinity
  const base = startOpen ? 0 : -drawerWidth
  return Math.min(0, Math.max(-drawerWidth, base + dx))
}

/** Backdrop opacity (0..1) tracking the panel's visible fraction. */
export function dragBackdropOpacity(
  translatePx: number,
  drawerWidth: number,
  maxOpacity: number = 1,
): number {
  if (!(drawerWidth > 0)) return 0
  const progress = Math.min(1, Math.max(0, (translatePx + drawerWidth) / drawerWidth))
  return Math.round(progress * maxOpacity * 100) / 100
}

export type DrawerSwipeOutcome = 'open' | 'close' | 'revert'

export interface ResolveEdgeSwipeInput {
  /** Drawer visibility when the touch began. */
  startOpen: boolean
  /** Total horizontal travel; right positive. */
  dx: number
  /** Latest horizontal velocity in px/ms; right positive. */
  velocityX: number
  drawerWidth: number
  thresholdPx?: number
  flingPxPerMs?: number
}

/**
 * Decide what happens on lift: open, close, or spring back to the start
 * state. Commit requires a distance past a third of the panel OR a fling;
 * fling direction must agree with the drag (right opens, left closes).
 */
export function resolveEdgeSwipe(input: ResolveEdgeSwipeInput): DrawerSwipeOutcome {
  const {
    startOpen,
    dx,
    velocityX,
    drawerWidth,
  } = input
  const thresholdPx = input.thresholdPx ?? drawerWidth * EDGE_COMMIT_RATIO
  const fling = input.flingPxPerMs ?? EDGE_FLING_PX_PER_MS

  // Closed + (dragged far right OR flicked right) => open.
  if (!startOpen && (dx >= thresholdPx || velocityX >= fling)) return 'open'
  // Open + (dragged far left OR flicked left) => close.
  if (startOpen && (dx <= -thresholdPx || velocityX <= -fling)) return 'close'
  return 'revert'
}

/** Simple open/closed reducer shared by the store and tests. */
export type DrawerOpenState = { open: boolean }
export type DrawerOpenAction =
  | { type: 'open' }
  | { type: 'close' }
  | { type: 'toggle' }
  /** Selecting a session always dismisses the drawer. */
  | { type: 'select' }

export function drawerOpenReducer(
  state: DrawerOpenState,
  action: DrawerOpenAction,
): DrawerOpenState {
  switch (action.type) {
    case 'open':
      return state.open ? state : { open: true }
    case 'close':
    case 'select':
      return state.open ? { open: false } : state
    case 'toggle':
      return { open: !state.open }
    default:
      return state
  }
}
