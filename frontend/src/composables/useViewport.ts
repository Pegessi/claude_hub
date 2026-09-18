import { ref } from 'vue'

const MOBILE_BREAKPOINT_PX = 768

// Module-level singleton: every component shares one viewport state and one
// resize listener. Lazily initialized so importing the module never touches
// `window` (safe for build/SSR tooling).
const isMobile = ref(false)
let initialized = false

function sync() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT_PX
}

/** Reactive viewport flags. `isMobile` is true at width ≤ 768px. */
export function useViewport() {
  if (!initialized) {
    initialized = true
    sync()
    window.addEventListener('resize', sync)
  }
  return { isMobile }
}
