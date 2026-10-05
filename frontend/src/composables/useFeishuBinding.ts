import { computed, ref, type Ref } from 'vue'

export interface FeishuBinding {
  owner_open_id: string
  sender_open_id: string
  app_id: string
  chat_id: string
  tab_id: string
  workspace_id: string | null
  created_at: string
}

export interface FeishuBindingCode {
  code: string
  expires_at: string
  event_url: string
}

export type FeishuBindingViewState =
  | 'unbound'
  | 'pending'
  | 'bound-current'
  | 'bound-other'
  | 'error'

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: string }
    if (body.detail) return body.detail
  } catch {
    // A proxy or framework may return a non-JSON error page.
  }
  return `HTTP ${response.status}`
}

export function useFeishuBinding(tabId: Ref<string>) {
  const binding = ref<FeishuBinding | null>(null)
  const pendingCode = ref<FeishuBindingCode | null>(null)
  const error = ref<string | null>(null)
  const needsLogin = ref(false)
  const isHydrated = ref(false)
  const isLoading = ref(false)
  const isMutating = ref(false)
  let requestEpoch = 0
  let controller: AbortController | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let pollingEnabled = false

  const viewState = computed<FeishuBindingViewState>(() => {
    if (pendingCode.value) return 'pending'
    if (binding.value?.tab_id === tabId.value) return 'bound-current'
    if (binding.value) return 'bound-other'
    if (error.value) return 'error'
    return 'unbound'
  })

  function clearPollTimer() {
    if (pollTimer !== null) clearTimeout(pollTimer)
    pollTimer = null
  }

  function requireLogin() {
    binding.value = null
    pendingCode.value = null
    needsLogin.value = true
    clearPollTimer()
  }

  function codeExpired(): boolean {
    const expiresAt = pendingCode.value ? Date.parse(pendingCode.value.expires_at) : Number.NaN
    return Number.isFinite(expiresAt) && expiresAt <= Date.now()
  }

  function schedulePoll() {
    clearPollTimer()
    if (!pollingEnabled || !pendingCode.value || codeExpired()) {
      if (pendingCode.value && codeExpired()) {
        pendingCode.value = null
        error.value = 'Binding code expired. Generate a new code to try again.'
      }
      return
    }
    pollTimer = setTimeout(async () => {
      await refresh()
      if (binding.value?.tab_id === tabId.value) pendingCode.value = null
      schedulePoll()
    }, 2_000)
  }

  async function refresh(): Promise<boolean> {
    if (isMutating.value) return false
    const epoch = ++requestEpoch
    controller?.abort()
    const requestController = new AbortController()
    controller = requestController
    isLoading.value = true
    error.value = null
    needsLogin.value = false
    try {
      const response = await fetch('/api/feishu/bot/binding', {
        credentials: 'same-origin',
        signal: requestController.signal,
      })
      if (epoch !== requestEpoch) return false
      if (response.status === 401) {
        requireLogin()
        throw new Error('Sign in with Feishu to manage this connection.')
      }
      if (!response.ok) throw new Error(await errorDetail(response))
      const body = await response.json() as { binding: FeishuBinding | null }
      if (epoch !== requestEpoch) return false
      binding.value = body.binding
      isHydrated.value = true
      if (binding.value?.tab_id === tabId.value) pendingCode.value = null
      return true
    } catch (cause) {
      if (epoch !== requestEpoch || requestController.signal.aborted) return false
      error.value = cause instanceof Error ? cause.message : 'Failed to read Feishu connection.'
      return false
    } finally {
      if (epoch === requestEpoch) isLoading.value = false
    }
  }

  async function generateCode(): Promise<boolean> {
    if (isMutating.value) return false
    const epoch = ++requestEpoch
    controller?.abort()
    const requestController = new AbortController()
    controller = requestController
    isMutating.value = true
    error.value = null
    needsLogin.value = false
    clearPollTimer()
    try {
      const response = await fetch('/api/feishu/bot/bind/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        signal: requestController.signal,
        body: JSON.stringify({ tab_id: tabId.value }),
      })
      if (epoch !== requestEpoch) return false
      if (response.status === 401) {
        requireLogin()
        throw new Error('Sign in with Feishu to manage this connection.')
      }
      if (!response.ok) throw new Error(await errorDetail(response))
      const nextPendingCode = await response.json() as FeishuBindingCode
      if (epoch !== requestEpoch || requestController.signal.aborted) return false
      pendingCode.value = nextPendingCode
      pollingEnabled = true
      schedulePoll()
      return true
    } catch (cause) {
      if (epoch !== requestEpoch || requestController.signal.aborted) return false
      error.value = cause instanceof Error ? cause.message : 'Failed to generate a binding code.'
      if (pollingEnabled && pendingCode.value) schedulePoll()
      return false
    } finally {
      if (epoch === requestEpoch) isMutating.value = false
    }
  }

  async function disconnect(): Promise<boolean> {
    if (isMutating.value) return false
    const epoch = ++requestEpoch
    controller?.abort()
    const requestController = new AbortController()
    controller = requestController
    isMutating.value = true
    error.value = null
    needsLogin.value = false
    clearPollTimer()
    try {
      const response = await fetch('/api/feishu/bot/binding', {
        method: 'DELETE',
        credentials: 'same-origin',
        signal: requestController.signal,
      })
      if (epoch !== requestEpoch) return false
      if (response.status === 401) {
        requireLogin()
        throw new Error('Sign in with Feishu to manage this connection.')
      }
      if (!response.ok) throw new Error(await errorDetail(response))
      binding.value = null
      pendingCode.value = null
      isHydrated.value = true
      return true
    } catch (cause) {
      if (epoch !== requestEpoch || requestController.signal.aborted) return false
      error.value = cause instanceof Error ? cause.message : 'Failed to disconnect Feishu.'
      if (pollingEnabled && pendingCode.value) schedulePoll()
      return false
    } finally {
      if (epoch === requestEpoch) isMutating.value = false
    }
  }

  function resume() {
    pollingEnabled = true
    if (pendingCode.value) schedulePoll()
  }

  function pause() {
    pollingEnabled = false
    clearPollTimer()
    requestEpoch++
    controller?.abort()
    controller = null
    isLoading.value = false
    isMutating.value = false
  }

  return {
    binding,
    pendingCode,
    error,
    needsLogin,
    isHydrated,
    isLoading,
    isMutating,
    viewState,
    refresh,
    generateCode,
    disconnect,
    resume,
    pause,
  }
}
