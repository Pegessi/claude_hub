// Whether programmatic or automatic text-input focus should be suppressed to
// avoid popping up the mobile soft keyboard when opening a dialog or selector
// menu. Users on phones or coarse-pointer touch screens must explicitly tap a
// text input to bring up the virtual keyboard.
const MOBILE_VIEWPORT_BREAKPOINT_PX = 768

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
