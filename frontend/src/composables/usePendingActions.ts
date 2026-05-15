import { ref } from 'vue'

export function usePendingActions() {
  const pendingKeys = ref(new Set<string>())

  function isPending(key: string) {
    return pendingKeys.value.has(key)
  }

  async function runPending<T>(key: string, action: () => Promise<T>): Promise<T | undefined> {
    if (isPending(key)) return undefined
    const nextKeys = new Set(pendingKeys.value)
    nextKeys.add(key)
    pendingKeys.value = nextKeys
    try {
      return await action()
    } finally {
      const remainingKeys = new Set(pendingKeys.value)
      remainingKeys.delete(key)
      pendingKeys.value = remainingKeys
    }
  }

  return {
    isPending,
    runPending,
  }
}
