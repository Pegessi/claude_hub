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
  // Delegated tasks that came back and wait for Dot's verdict.
  pending_reviews: number
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
  // A numbered checklist; older items hold one string.
  done_when?: string | string[]
  output?: string
  status?: string
  result_card?: string
  // A result came back and Dot has not judged it yet.
  review_due?: boolean
  review?: DotReview
  // The worker had to ask a question before finishing.
  asked?: boolean
  closed?: boolean
  // Set for an existing task the user asked Dot to watch (not delegated by Dot).
  watched?: boolean
  stall_hours?: number
  created: string
  updated: string
}

// change: the user changed what they want (no lesson, not in the first-pass rate).
export type DotReviewCause = 'brief' | 'env' | 'worker' | 'change'

export interface DotReview {
  verdict: 'ok' | 'redo'
  cause?: DotReviewCause
  note?: string
  lesson_id?: string
  at: string
}

// One rule for writing briefs, learned when a task came back wrong because of its brief.
export interface DotLesson {
  id: string
  rule: string
  // How many tasks missed this.
  votes: number
  // Strength as of the last vote or merge (older strength already decayed).
  weight?: number
  tasks: string[]
  created: string
  seen: string
  // Strength halved every 60 days without a new vote.
  score: number
  // Among the few strongest rules shown in every session.
  in_session: boolean
  // always: in every session; on match: listed when a brief shares its words; faded: history.
  reach: 'always' | 'on match' | 'faded'
  dropped?: boolean
  merged_into?: string
}

export interface DotReviewStats {
  reviewed: number
  first_pass: number
  rate: number | null
  // First-round causes of the reviews in the window.
  causes: Partial<Record<DotReviewCause, number>>
  pending: number
  // Delegated tasks that came back, and how many of them got a verdict.
  judged: number
  returned: number
  window: number
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
export function loadDotLessons(all = false): Promise<{ lessons: DotLesson[], stats: DotReviewStats }> {
  return requestJson(`/lessons${query({ all: all || undefined })}`)
}
export function addDotLesson(rule: string): Promise<DotLesson> {
  return requestJson('/lessons', jsonRequest('POST', { rule }))
}
export function updateDotLesson(lessonId: string, rule: string): Promise<DotLesson> {
  return requestJson(`/lessons/${encodeURIComponent(lessonId)}`, jsonRequest('PATCH', { rule }))
}
export function dropDotLesson(lessonId: string): Promise<DotLesson> {
  return requestJson(`/lessons/${encodeURIComponent(lessonId)}`, { method: 'DELETE' })
}

// "First pass 3/4 (75%)": tasks that were right at the first verdict, no question asked.
export function formatFirstPass(stats: DotReviewStats): string {
  if (!stats.reviewed) return 'No reviews yet'
  return `First pass ${stats.first_pass}/${stats.reviewed} (${Math.round((stats.rate ?? 0) * 100)}%)`
}

// "judged 4/5 returned": a rate is only honest when most returned tasks got a verdict.
export function formatReviewCoverage(stats: Pick<DotReviewStats, 'judged' | 'returned'>): string {
  return stats.returned ? `judged ${stats.judged}/${stats.returned} returned` : ''
}

// Same as the backend QUIET_REPLY: a Hub wake or tidy turn where nothing needed the user.
export const DOT_QUIET_REPLY = '[quiet]'

export function isQuietDotTurn(turn: { userText: string, assistantText: string, completed: boolean }): boolean {
  return turn.completed &&
    turn.userText.trimStart().startsWith('[Dot ') &&
    turn.assistantText.trim() === DOT_QUIET_REPLY
}
