export interface RestartOperation {
  id: string
  instance_id: string
  status: 'preparing' | 'restarting' | 'succeeded' | 'failed'
  message: string
}

export interface RestartStatus {
  instance_id: string
  available: boolean
  reason: string
  operation: RestartOperation | null
}

export interface PendingRestart {
  id: string
  instanceId: string
}

export const RESTART_STORAGE_KEY = 'claude_hub_pending_restart'

export function createRestartId(): string {
  // getRandomValues is also available on LAN HTTP; randomUUID is HTTPS-only.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function restartOutcome(status: RestartStatus, pending: PendingRestart) {
  if (status.operation?.id !== pending.id) return 'waiting'
  if (status.operation.status === 'failed') return 'failed'
  if (status.operation.status === 'succeeded' && status.instance_id !== pending.instanceId) return 'succeeded'
  return 'waiting'
}

export async function fetchRestartStatus(body?: { instance_id: string; request_id: string }): Promise<RestartStatus> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 4000)
  try {
    const response = await fetch('/api/system/restart', {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json', 'X-Claude-Hub-Restart': '1' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: 'no-store',
    })
    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      throw new RestartRequestError(error.detail || `Request failed (${response.status})`)
    }
    return await response.json()
  } finally {
    window.clearTimeout(timeout)
  }
}

export class RestartRequestError extends Error {}
