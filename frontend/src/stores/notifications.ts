import { computed, ref } from 'vue'
import type { NotificationType, StoreNotification } from '@/types'

export function createStoreNotifications(idPrefix: string, errorTimeoutMs: number) {
  const notifications = ref<StoreNotification[]>([])
  let idSequence = 0

  function pushNotification(partial: Omit<StoreNotification, 'id'>) {
    const id = `${idPrefix}-${Date.now().toString(36)}-${(idSequence++).toString(36)}`
    const notification: StoreNotification = { id, ...partial }
    notifications.value.push(notification)
    if (notification.autoDismissMs && notification.autoDismissMs > 0) {
      window.setTimeout(() => dismissNotification(id), notification.autoDismissMs)
    }
  }

  function dismissNotification(id: string) {
    const index = notifications.value.findIndex(notification => notification.id === id)
    if (index >= 0) notifications.value.splice(index, 1)
  }

  function notifyError(message: string) {
    pushNotification({ type: 'error', message, autoDismissMs: errorTimeoutMs })
  }

  const error = computed<string | null>(() =>
    notifications.value.find(
      notification => notification.type === ('error' as NotificationType),
    )?.message ?? null
  )

  return { notifications, pushNotification, dismissNotification, notifyError, error }
}
