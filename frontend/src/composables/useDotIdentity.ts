import { ref } from 'vue'
import { loadDotIdentity, type DotIdentity } from '@/utils/dotApi'

// Module-level singleton: the launcher(s) and the panel share one identity.
const identity = ref<DotIdentity>({ name: 'Dot', avatar: '', avatar_image: null })
let loaded = false

function setIdentity(next: DotIdentity) {
  identity.value = next
  loaded = true
}

async function refreshIdentity(force = false) {
  if (loaded && !force) return
  try {
    setIdentity(await loadDotIdentity())
  } catch {
    // Keep the default name; Dot works without it.
  }
}

/** Dot's name and avatar, shared by every Dot component. */
export function useDotIdentity() {
  return { identity, setIdentity, refreshIdentity }
}
