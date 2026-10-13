import { onScopeDispose, ref, watch } from 'vue'

type ProbeState = 'idle' | 'checking' | 'available' | 'unavailable'

interface PreflightResponse {
  available: boolean
  base_url: string
  message: string
}

export function useMasoPreflight(active: () => boolean, baseUrl: () => string | undefined) {
  const state = ref<ProbeState>('idle')
  const message = ref('')
  const endpoint = ref('')
  let generation = 0
  let controller: AbortController | null = null
  let timeout: ReturnType<typeof setTimeout> | null = null

  function invalidate() {
    generation += 1
    controller?.abort()
    controller = null
    if (timeout !== null) clearTimeout(timeout)
    timeout = null
  }

  async function check() {
    invalidate()
    if (!active()) {
      state.value = 'idle'
      message.value = ''
      endpoint.value = ''
      return
    }
    const current = generation
    const requestBase = baseUrl()
    const requestController = new AbortController()
    controller = requestController
    timeout = setTimeout(() => requestController.abort(), 5000)
    state.value = 'checking'
    message.value = 'Checking MASO service…'
    endpoint.value = ''
    try {
      const response = await fetch('/api/maso/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: requestBase || null }),
        signal: requestController.signal,
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to check MASO service. Retry the check.')
      }
      if (typeof data.available !== 'boolean' || typeof data.message !== 'string'
        || typeof data.base_url !== 'string') {
        throw new Error('Unable to check MASO service. Retry the check.')
      }
      const result = data as PreflightResponse
      if (current !== generation || !active() || baseUrl() !== requestBase) return
      state.value = result.available ? 'available' : 'unavailable'
      message.value = result.message
      endpoint.value = result.base_url
    } catch (error) {
      if (current !== generation || !active()) return
      state.value = 'unavailable'
      message.value = error instanceof Error && error.name !== 'AbortError'
        ? error.message : 'Unable to reach the MASO service check. Retry the check.'
    } finally {
      if (current === generation) {
        if (timeout !== null) clearTimeout(timeout)
        timeout = null
        controller = null
      }
    }
  }

  // Synchronous invalidation prevents an old successful probe enabling Create
  // after a preset/service change, including within the same browser event.
  watch([active, baseUrl], () => { void check() }, { immediate: true, flush: 'sync' })
  onScopeDispose(invalidate)
  return { state, message, endpoint, check }
}
