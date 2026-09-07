import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskRunResult,
  ScheduledTaskUpdate,
} from '@/types'

const API_BASE = '/api/scheduled-tasks'

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json()
    const detail = data?.detail
    if (Array.isArray(detail)) {
      // FastAPI 422: detail is an array of { loc, msg, type }. Flatten to a
      // readable string instead of "[object Object]".
      return (
        detail
          .map((item: { loc?: unknown; msg?: string }) => {
            const field = Array.isArray(item.loc)
              ? item.loc.filter(part => part !== 'body').join('.')
              : ''
            return field ? `${field}: ${item.msg}` : item.msg
          })
          .join('; ') || response.statusText
      )
    }
    return detail || response.statusText
  } catch {
    return response.statusText
  }
}

export const useScheduledTasksStore = defineStore('scheduled-tasks', () => {
  const tasks = ref<ScheduledTask[]>([])
  const isLoading = ref(false)
  const isMutating = ref(false)
  const error = ref<string | null>(null)

  const enabledCount = computed(() => tasks.value.filter(task => task.enabled).length)

  async function fetchTasks(options?: { silent?: boolean }) {
    // Don't let a background poll clobber an in-flight mutation's optimistic
    // local state; the mutation updates the list itself when it completes.
    if (options?.silent && isMutating.value) return
    if (!options?.silent) {
      isLoading.value = true
      error.value = null
    }
    try {
      const response = await fetch(API_BASE)
      if (!response.ok) throw new Error(await readError(response))
      tasks.value = await response.json()
    } catch (e) {
      if (!options?.silent) {
        error.value = e instanceof Error ? e.message : 'Failed to fetch scheduled tasks'
      }
    } finally {
      if (!options?.silent) isLoading.value = false
    }
  }

  async function createTask(payload: ScheduledTaskCreate): Promise<ScheduledTask> {
    isMutating.value = true
    try {
      const response = await fetch(API_BASE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      const task: ScheduledTask = await response.json()
      tasks.value.unshift(task)
      return task
    } finally {
      isMutating.value = false
    }
  }

  async function updateTask(taskId: string, payload: ScheduledTaskUpdate): Promise<ScheduledTask> {
    isMutating.value = true
    try {
      const response = await fetch(`${API_BASE}/${taskId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      const updated: ScheduledTask = await response.json()
      const index = tasks.value.findIndex(task => task.id === taskId)
      if (index >= 0) tasks.value[index] = updated
      return updated
    } finally {
      isMutating.value = false
    }
  }

  async function deleteTask(taskId: string): Promise<void> {
    isMutating.value = true
    try {
      const response = await fetch(`${API_BASE}/${taskId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readError(response))
      tasks.value = tasks.value.filter(task => task.id !== taskId)
    } finally {
      isMutating.value = false
    }
  }

  async function runTask(taskId: string): Promise<ScheduledTaskRunResult> {
    isMutating.value = true
    let result: ScheduledTaskRunResult
    try {
      const response = await fetch(`${API_BASE}/${taskId}/run`, { method: 'POST' })
      if (!response.ok) throw new Error(await readError(response))
      result = await response.json()
      // Reflect the fired run immediately for snappy feedback.
      const index = tasks.value.findIndex(task => task.id === taskId)
      if (index >= 0) {
        const current = tasks.value[index]
        tasks.value[index] = {
          ...current,
          last_run_at: result.last_run_at ?? current.last_run_at,
          last_status: result.last_status ?? current.last_status,
          last_error: result.last_error ?? current.last_error,
          run_count: current.run_count + 1,
        }
      }
    } finally {
      isMutating.value = false
    }
    // Re-fetch authoritative state: enabled / next_run_at change on fire and
    // aren't part of the run result. Safe now that the mutation window closed.
    await fetchTasks({ silent: true })
    return result
  }

  async function setEnabled(taskId: string, enabled: boolean): Promise<ScheduledTask> {
    return updateTask(taskId, { enabled })
  }

  return {
    tasks,
    isLoading,
    isMutating,
    error,
    enabledCount,
    fetchTasks,
    createTask,
    updateTask,
    deleteTask,
    runTask,
    setEnabled,
  }
})
