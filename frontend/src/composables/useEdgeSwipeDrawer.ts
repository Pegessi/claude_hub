import { ref, watch, onUnmounted, type Ref } from 'vue'
import {
  classifyIntent,
  dragBackdropOpacity,
  dragTranslatePx,
  resolveEdgeSwipe,
  EDGE_DIRECTION_SLOP_PX,
} from '@/utils/edgeSwipe'

/**
 * Binds the mobile drawer's touch gestures:
 *  - open: horizontal drag starting on the invisible left edge band (needed
 *    because the ttyd terminal runs in an <iframe>, whose touches never
 *    bubble to this document);
 *  - close: leftward drag starting on the backdrop or the panel header;
 *  - tap on the band is forwarded to the surface underneath so the narrow
 *    edge sliver does not become a dead zone;
 *  - vertical movement is never preventDefault-ed, so list scrolling and
 *    (outside the iframe sliver) terminal gestures are unaffected.
 *
 * Geometry decisions live in `utils/edgeSwipe`; this module only wires DOM
 * events to them.
 */

type Zone = 'open' | 'close'

interface ActiveDrag {
  touchId: number
  zone: Zone
  startX: number
  startY: number
  startTime: number
  startOpen: boolean
  /** Once true we own the gesture (horizontal intent past the slop). */
  captured: boolean
  lastX: number
  lastTime: number
}

interface UseEdgeSwipeDrawerOptions {
  /** Invisible left-edge strip that opens the drawer. */
  bandRef: Ref<HTMLElement | null>
  /** Drawer panel, measured for width + translate. */
  panelRef: Ref<HTMLElement | null>
  /** Dimmed layer: tap or left-drag closes. */
  backdropRef: Ref<HTMLElement | null>
  /** Header strip: left-drag closes without fighting list scroll/rows. */
  closeDragRef: Ref<HTMLElement | null>
  /** Only bind while true (mobile + terminal mode). */
  enabled: Ref<boolean>
  open: () => void
  close: () => void
  isOpen: () => boolean
}

export function useEdgeSwipeDrawer(options: UseEdgeSwipeDrawerOptions) {
  const {
    bandRef,
    panelRef,
    backdropRef,
    closeDragRef,
    enabled,
    open: openDrawer,
    close: closeDrawer,
    isOpen,
  } = options

  // True while the finger owns a horizontal drag; the template disables the
  // CSS transition and follows translatePx/backdropOpacity 1:1.
  const dragging = ref(false)
  const translatePx = ref(0)
  const backdropOpacity = ref(0)

  let drag: ActiveDrag | null = null

  function drawerWidth(): number {
    const width = panelRef.value?.getBoundingClientRect().width ?? 0
    return width > 0 ? width : 320
  }

  function beginDrag(event: TouchEvent, zone: Zone) {
    // Only track the first finger; multi-touch belongs to the surface below.
    if (drag || event.changedTouches.length !== 1) return
    const touch = event.changedTouches[0]
    drag = {
      touchId: touch.identifier,
      zone,
      startX: touch.clientX,
      startY: touch.clientY,
      startTime: event.timeStamp,
      startOpen: isOpen(),
      captured: false,
      lastX: touch.clientX,
      lastTime: event.timeStamp,
    }
  }

  function onTouchMove(event: TouchEvent) {
    const state = drag
    if (!state) return
    const touch = [...event.changedTouches].find(t => t.identifier === state.touchId)
    if (!touch) return

    if (!state.captured) {
      const intent = classifyIntent(
        touch.clientX - state.startX,
        touch.clientY - state.startY,
      )
      if (intent === 'undecided') return
      if (intent === 'vertical') {
        // Hand the gesture back: never intercept vertical scroll/selection.
        drag = null
        return
      }
      state.captured = true
      dragging.value = true
    }

    // Horizontal takeover: stop the browser scrolling / the iframe panning.
    event.preventDefault()
    state.lastX = touch.clientX
    state.lastTime = event.timeStamp
    const dx = touch.clientX - state.startX
    const width = drawerWidth()
    translatePx.value = dragTranslatePx(state.startOpen, dx, width)
    backdropOpacity.value = dragBackdropOpacity(translatePx.value, width)
  }

  function finishDrag(event: TouchEvent, cancelled: boolean) {
    const state = drag
    if (!state) return
    drag = null
    const touch = [...event.changedTouches].find(t => t.identifier === state.touchId)

    // No horizontal takeover: an untouched tap on the edge band gets
    // re-dispatched to the surface underneath (composer caret, links, …).
    if (!state.captured) {
      if (!cancelled && touch && state.zone === 'open') {
        forwardTap(touch.clientX, touch.clientY)
      }
      return
    }

    dragging.value = false
    if (cancelled || !touch) {
      // Spring back to whichever state the drag started in.
      if (state.startOpen) openDrawer()
      else closeDrawer()
      return
    }

    const dx = touch.clientX - state.startX
    const elapsed = Math.max(1, event.timeStamp - state.startTime)
    const velocityX = dx / elapsed
    const outcome = resolveEdgeSwipe({
      startOpen: state.startOpen,
      dx,
      velocityX,
      drawerWidth: drawerWidth(),
    })
    if (outcome === 'open') openDrawer()
    else if (outcome === 'close') closeDrawer()
    else if (state.startOpen) openDrawer()
    else closeDrawer()
  }

  /**
   * Re-dispatch a tap that landed on the invisible edge band to whatever is
   * underneath — including a same-origin ttyd iframe — so the 20px sliver
   * cannot swallow composer taps or terminal focus clicks.
   */
  function forwardTap(clientX: number, clientY: number) {
    const band = bandRef.value
    if (!band) return
    band.style.pointerEvents = 'none'
    try {
      let target: Element | null = document.elementFromPoint(clientX, clientY)
      let doc: Document = document
      let x = clientX
      let y = clientY
      if (target instanceof HTMLIFrameElement) {
        const frame = target
        try {
          const frameDoc = frame.contentDocument
          if (frameDoc) {
            const rect = frame.getBoundingClientRect()
            x = clientX - rect.left
            y = clientY - rect.top
            target = frameDoc.elementFromPoint(x, y)
            doc = frameDoc
          }
        } catch {
          // Cross-origin iframe: nothing to forward to; ignore the tap.
          target = null
        }
      }
      if (!target || !doc.defaultView) return
      const eventInit = {
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        view: doc.defaultView,
      }
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        const Ctor = type.startsWith('pointer') && doc.defaultView.PointerEvent
          ? doc.defaultView.PointerEvent
          : doc.defaultView.MouseEvent
        target.dispatchEvent(new Ctor(type, eventInit))
      }
    } finally {
      band.style.pointerEvents = ''
    }
  }

  // ---- DOM binding -------------------------------------------------------

  const cleanups: Array<() => void> = []

  function bindZone(el: HTMLElement, zone: Zone) {
    const onStart = (event: TouchEvent) => beginDrag(event, zone)
    const onMove = (event: TouchEvent) => onTouchMove(event)
    const onEnd = (event: TouchEvent) => finishDrag(event, false)
    const onCancel = (event: TouchEvent) => finishDrag(event, true)
    // touchmove must be non-passive so horizontal takeover can preventDefault.
    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchmove', onMove, { passive: false })
    el.addEventListener('touchend', onEnd)
    el.addEventListener('touchcancel', onCancel)
    cleanups.push(() => {
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchmove', onMove)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onCancel)
    })
  }

  function unbindAll() {
    while (cleanups.length) cleanups.pop()!()
  }

  function rebind() {
    unbindAll()
    if (!enabled.value) return
    if (bandRef.value) bindZone(bandRef.value, 'open')
    if (backdropRef.value) bindZone(backdropRef.value, 'close')
    if (closeDragRef.value) bindZone(closeDragRef.value, 'close')
  }

  watch([enabled, bandRef, backdropRef, closeDragRef], rebind, { immediate: true })

  onUnmounted(() => {
    unbindAll()
    drag = null
  })

  // Exposed for tests / debugging; EDGE_DIRECTION_SLOP_PX documents the slop.
  void EDGE_DIRECTION_SLOP_PX

  return {
    dragging,
    translatePx,
    backdropOpacity,
  }
}
