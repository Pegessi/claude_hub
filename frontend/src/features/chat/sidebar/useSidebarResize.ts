import { computed, onUnmounted, ref, type Ref } from 'vue'

export const SIDEBAR_DEFAULT_WIDTH = 240
export const SIDEBAR_MIN_WIDTH = 200
export const SIDEBAR_MAX_WIDTH = 480
export const SIDEBAR_WIDTH_STORAGE_KEY = 'claude_hub_chat_sidebar_width'


/** Own width persistence and all pointer-resize listeners for one sidebar. */
export function useSidebarResize(sidebarCollapsed: Readonly<Ref<boolean>>) {
  function clampSidebarWidth(width: number): number {
    return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)))
  }

  function loadSidebarWidth(): number {
    try {
      const saved = Number(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY))
      return Number.isFinite(saved) && saved > 0
        ? clampSidebarWidth(saved)
        : SIDEBAR_DEFAULT_WIDTH
    } catch {
      return SIDEBAR_DEFAULT_WIDTH
    }
  }

  const sidebarWidth = ref(loadSidebarWidth())
  const isResizing = ref(false)
  let resizeStartX = 0
  let resizeStartWidth = SIDEBAR_DEFAULT_WIDTH
  const sidebarStyle = computed(() => sidebarCollapsed.value
    ? undefined
    : { width: `${sidebarWidth.value}px`, minWidth: `${sidebarWidth.value}px` })

  function persistSidebarWidth() {
    try {
      localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth.value))
    } catch {
      // Width persistence is optional in privacy-restricted browsers.
    }
  }

  function resizeSidebar(event: PointerEvent) {
    sidebarWidth.value = clampSidebarWidth(
      resizeStartWidth + event.clientX - resizeStartX,
    )
  }

  function stopResize() {
    if (!isResizing.value) return
    isResizing.value = false
    document.body.classList.remove('chat-sidebar-resizing')
    window.removeEventListener('pointermove', resizeSidebar)
    window.removeEventListener('pointerup', stopResize)
    window.removeEventListener('pointercancel', stopResize)
    persistSidebarWidth()
  }

  function startResize(event: PointerEvent) {
    if (event.button !== 0) return
    event.preventDefault()
    resizeStartX = event.clientX
    resizeStartWidth = sidebarWidth.value
    isResizing.value = true
    document.body.classList.add('chat-sidebar-resizing')
    window.addEventListener('pointermove', resizeSidebar)
    window.addEventListener('pointerup', stopResize)
    window.addEventListener('pointercancel', stopResize)
  }

  function resetSidebarWidth() {
    sidebarWidth.value = SIDEBAR_DEFAULT_WIDTH
    persistSidebarWidth()
  }

  function resizeWithKeyboard(event: KeyboardEvent) {
    let width = sidebarWidth.value
    if (event.key === 'ArrowLeft') width -= 16
    else if (event.key === 'ArrowRight') width += 16
    else if (event.key === 'Home') width = SIDEBAR_MIN_WIDTH
    else if (event.key === 'End') width = SIDEBAR_MAX_WIDTH
    else return
    event.preventDefault()
    sidebarWidth.value = clampSidebarWidth(width)
    persistSidebarWidth()
  }

  onUnmounted(stopResize)
  return { sidebarWidth, sidebarStyle, isResizing, startResize, resetSidebarWidth, resizeWithKeyboard }
}
