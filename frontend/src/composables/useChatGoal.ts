import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import type { ChatGoal, ChatGoalCreate } from '@/types'

type GoalMutation = 'pause' | 'resume' | 'complete' | 'clear'

function requestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `goal-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: string }
    if (body.detail) return body.detail
  } catch {
    // A proxy or framework may return a non-JSON error page.
  }
  return `HTTP ${response.status}`
}

export function useChatGoal(tabId: Ref<string>) {
  const goal = ref<ChatGoal | null>(null)
  const isHydrating = ref(false)
  const isMutating = ref(false)
  const isHydrated = ref(false)
  const error = ref<string | null>(null)
  let epoch = 0
  let mutationEpoch = 0
  let hydrationController: AbortController | null = null
  let mutationController: AbortController | null = null

  async function hydrate(): Promise<void> {
    if (isMutating.value) return
    const requestEpoch = ++epoch
    hydrationController?.abort()
    const controller = new AbortController()
    hydrationController = controller
    const timeout = setTimeout(() => controller.abort(), 15_000)
    isHydrating.value = true
    error.value = null
    try {
      const response = await fetch(`/api/tabs/${tabId.value}/goal/current`, {
        credentials: 'same-origin',
        signal: controller.signal,
      })
      if (requestEpoch !== epoch) return
      if (response.status === 204) {
        goal.value = null
        isHydrated.value = true
        return
      }
      if (!response.ok) throw new Error(await errorDetail(response))
      const body = await response.json() as ChatGoal | null
      if (requestEpoch === epoch) {
        goal.value = body ?? null
        isHydrated.value = true
      }
    } catch (cause) {
      if (requestEpoch !== epoch) return
      error.value = cause instanceof Error ? cause.message : 'Failed to load Goal.'
    } finally {
      clearTimeout(timeout)
      if (requestEpoch === epoch) isHydrating.value = false
    }
  }

  async function create(input: Omit<ChatGoalCreate, 'client_request_id'>): Promise<boolean> {
    return run(async signal => {
      const response = await fetch(`/api/tabs/${tabId.value}/goal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        signal,
        body: JSON.stringify({ ...input, client_request_id: requestId() }),
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      return await response.json() as ChatGoal
    })
  }

  async function mutate(operation: GoalMutation): Promise<boolean> {
    const current = goal.value
    if (!current) return false
    return run(async signal => {
      const response = await fetch(`/api/goals/${current.id}/${operation}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        signal,
        body: JSON.stringify({ client_request_id: requestId() }),
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      if (response.status === 204) return null
      const result = await response.json() as ChatGoal
      return result.status === 'cancelled' && result.dispatch_state === 'idle' ? null : result
    })
  }

  async function run(action: (signal: AbortSignal) => Promise<ChatGoal | null>): Promise<boolean> {
    if (isMutating.value) return false
    epoch++
    const requestMutationEpoch = ++mutationEpoch
    hydrationController?.abort()
    isHydrating.value = false
    isMutating.value = true
    error.value = null
    const controller = new AbortController()
    mutationController = controller
    const timeout = setTimeout(() => controller.abort(), 30_000)
    try {
      const nextGoal = await action(controller.signal)
      if (requestMutationEpoch !== mutationEpoch) return false
      goal.value = nextGoal
      isHydrated.value = true
      return true
    } catch (cause) {
      if (requestMutationEpoch === mutationEpoch) {
        const message = cause instanceof Error ? cause.message : 'Goal action failed.'
        // A lost response does not imply the server rejected the operation.
        // Reconcile before letting the UI offer a conflicting action.
        isMutating.value = false
        await hydrate()
        if (requestMutationEpoch === mutationEpoch) error.value = message
      }
      return false
    } finally {
      clearTimeout(timeout)
      if (requestMutationEpoch === mutationEpoch) isMutating.value = false
    }
  }

  function dispose() {
    epoch++
    mutationEpoch++
    hydrationController?.abort()
    mutationController?.abort()
    hydrationController = null
  }

  watch(tabId, () => {
    dispose()
    goal.value = null
    isHydrated.value = false
    isMutating.value = false
    void hydrate()
  }, { flush: 'sync' })

  onUnmounted(dispose)

  return {
    goal,
    error,
    isHydrated,
    isHydrating,
    isMutating,
    hasGoal: computed(() => goal.value !== null),
    hydrate,
    create,
    pause: () => mutate('pause'),
    resume: () => mutate('resume'),
    complete: () => mutate('complete'),
    clear: () => mutate('clear'),
    dispose,
  }
}
