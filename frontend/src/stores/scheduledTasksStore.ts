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
    return data.detail || response.statusText
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
    const response = await fetch(`${API_BASE}/${taskId}/run`, { method: 'POST' })
    if (!response.ok) throw new Error(await readError(response))
    const result: ScheduledTaskRunResult = await response.json()
    // Reflect the fired run on the local copy so the UI updates immediately.
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
