import { computed, onUnmounted, ref, type Ref } from 'vue'
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
  const error = ref<string | null>(null)
  let epoch = 0
  let hydrationController: AbortController | null = null

  async function hydrate(): Promise<void> {
    const requestEpoch = ++epoch
    hydrationController?.abort()
    const controller = new AbortController()
    hydrationController = controller
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
        return
      }
      if (!response.ok) throw new Error(await errorDetail(response))
      const body = await response.json() as ChatGoal | null
      if (requestEpoch === epoch) goal.value = body ?? null
    } catch (cause) {
      if (controller.signal.aborted || requestEpoch !== epoch) return
      error.value = cause instanceof Error ? cause.message : 'Failed to load Goal.'
    } finally {
      if (requestEpoch === epoch) isHydrating.value = false
    }
  }

  async function create(input: Omit<ChatGoalCreate, 'client_request_id'>): Promise<boolean> {
    return run(async () => {
      const response = await fetch(`/api/tabs/${tabId.value}/goal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ ...input, client_request_id: requestId() }),
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      return await response.json() as ChatGoal
    })
  }

  async function mutate(operation: GoalMutation): Promise<boolean> {
    const current = goal.value
    if (!current) return false
    return run(async () => {
      const response = await fetch(`/api/goals/${current.id}/${operation}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ client_request_id: requestId() }),
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      if (operation === 'clear' || response.status === 204) return null
      return await response.json() as ChatGoal
    })
  }

  async function run(action: () => Promise<ChatGoal | null>): Promise<boolean> {
    if (isMutating.value) return false
    const requestEpoch = ++epoch
    hydrationController?.abort()
    isHydrating.value = false
    isMutating.value = true
    error.value = null
    try {
      const nextGoal = await action()
      if (requestEpoch !== epoch) return false
      goal.value = nextGoal
      return true
    } catch (cause) {
      if (requestEpoch === epoch) {
        error.value = cause instanceof Error ? cause.message : 'Goal action failed.'
      }
      return false
    } finally {
      if (requestEpoch === epoch) isMutating.value = false
    }
  }

  function dispose() {
    epoch++
    hydrationController?.abort()
    hydrationController = null
  }

  onUnmounted(dispose)

  return {
    goal,
    error,
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
