// Thin client for /api/dot (Dot, the persistent personal agent; see docs/DOT.md).

export type DotCardKind = 'user' | 'work' | 'knowledge' | 'concept'

export interface DotIdentity {
  name: string
  // Short emoji or text; '' means the default dot mark.
  avatar: string
  // Home-relative path under files/, served by /api/dot/files.
  avatar_image: string | null
}

export interface DotStatus {
  set_up: boolean
  identity: DotIdentity
  home: string
  tab_id: string | null
  tab_exists: boolean
  session: { conversation_id: string | null, turns: number, started_at: string | null, max_turns: number }
  cards: Partial<Record<DotCardKind, number>>
  focus: number
  open_work: number
  unread_events: number
  last_tidy_at: string | null
}

export interface DotCard {
  id: string
  kind: DotCardKind
  title: string
  summary: string
  tags: string[]
  importance: number
  links: string[]
  files: string[]
  source: string
  created: string
  updated: string
  archived: boolean
  body?: string
}

export interface DotSearchHit {
  id: string
  kind: DotCardKind
  title: string
  summary: string
  importance: number
  updated: string
  score: number
}

export interface DotFocusItem {
  id: string
  title: string
  why: string
  priority: number
  status: 'active' | 'done' | 'dropped'
  created: string
  updated: string
}

export interface DotWorkItem {
  task_id: string
  workspace_id?: string
  title?: string
  goal?: string
  done_when?: string
  status?: string
  result_card?: string
  closed?: boolean
  // Set for an existing task the user asked Dot to watch (not delegated by Dot).
  watched?: boolean
  stall_hours?: number
  created: string
  updated: string
}

export interface DotInboxEvent {
  id: string
  at: string
  kind: string
  text: string
  read: boolean
  task_id?: string
  card_id?: string
}

export interface DotHealth {
  ok: boolean
  checks: { name: string, ok: boolean, detail: string }[]
  cards: number
}

const ROOT = '/api/dot'

export class DotRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, { ...init, credentials: 'same-origin' })
  if (!response.ok) {
    let detail = `Dot request failed (${response.status})`
    try {
      const body = await response.json() as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch { /* keep the generic message */ }
    throw new DotRequestError(response.status, detail)
  }
  return await response.json() as T
}

function jsonRequest(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function loadDotStatus(): Promise<DotStatus> {
  return requestJson('/status')
}
// Idempotent: creates (or restores) the memory home, Chat tab and schedules.
// `env` is the provider launch env for Dot's Chat tab, like a normal new Chat.
export function setupDot(env?: Record<string, string>): Promise<DotStatus> {
  return requestJson('/setup', jsonRequest('POST', { env: env ?? null }))
}
export function loadDotHealth(): Promise<DotHealth> {
  return requestJson('/health')
}
export function recallDot(q: string, kind?: DotCardKind): Promise<DotSearchHit[]> {
  return requestJson(`/recall${query({ q, kind, limit: 30 })}`)
}
export function loadDotCards(kind?: DotCardKind): Promise<DotCard[]> {
  return requestJson(`/cards${query({ kind })}`)
}
export function loadDotCard(cardId: string): Promise<DotCard> {
  return requestJson(`/cards/${encodeURIComponent(cardId)}`)
}
export function archiveDotCard(cardId: string): Promise<{ id: string, archived: boolean }> {
  return requestJson(`/cards/${encodeURIComponent(cardId)}`, { method: 'DELETE' })
}
export function loadDotFocus(): Promise<DotFocusItem[]> {
  return requestJson('/focus')
}
export function addDotFocus(input: { title: string, why?: string, priority?: number }): Promise<DotFocusItem> {
  return requestJson('/focus', jsonRequest('POST', input))
}
export function updateDotFocus(
  focusId: string,
  changes: Partial<Pick<DotFocusItem, 'title' | 'why' | 'priority' | 'status'>>,
): Promise<DotFocusItem> {
  return requestJson(`/focus/${encodeURIComponent(focusId)}`, jsonRequest('PATCH', changes))
}
export function loadDotWork(): Promise<DotWorkItem[]> {
  return requestJson('/work')
}
export function loadDotInbox(): Promise<DotInboxEvent[]> {
  return requestJson('/inbox')
}
export function ackDotInbox(ids?: string[]): Promise<{ acked: number }> {
  return requestJson('/inbox/ack', jsonRequest('POST', { ids: ids ?? null }))
}
export function loadDotIdentity(): Promise<DotIdentity> {
  return requestJson('/identity')
}
export function updateDotIdentity(changes: { name?: string, avatar?: string }): Promise<DotIdentity> {
  return requestJson('/identity', jsonRequest('PATCH', changes))
}
// PNG, JPEG, GIF or WebP, at most 2 MB (checked again by the server).
export async function uploadDotAvatar(file: Blob): Promise<DotIdentity> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ''
  for (let start = 0; start < bytes.length; start += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000))
  }
  return requestJson('/identity/avatar', jsonRequest('POST', { data_base64: btoa(binary) }))
}
export function clearDotAvatar(): Promise<DotIdentity> {
  return requestJson('/identity/avatar', { method: 'DELETE' })
}
export function dotFileUrl(path: string): string {
  return `${ROOT}/${path.split('/').map(encodeURIComponent).join('/')}`
}
export function watchDotTask(taskId: string, stallHours?: number): Promise<DotWorkItem> {
  return requestJson(
    `/work/${encodeURIComponent(taskId)}/watch`,
    jsonRequest('POST', { stall_hours: stallHours ?? null }),
  )
}
export function unwatchDotTask(taskId: string): Promise<DotWorkItem> {
  return requestJson(`/work/${encodeURIComponent(taskId)}/unwatch`, { method: 'POST' })
}
