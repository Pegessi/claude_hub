export type FeishuBotSource = 'stored' | 'environment' | 'invalid_environment'
export type FeishuPairingOwnerKind = 'oauth' | 'local'
export type FeishuBotConnectionStatus = 'connected' | 'connecting' | 'failed' | 'stopped'

export interface FeishuActiveBinding {
  pairing_id: string
  state: 'active'
  tab_id: string
  workspace_id: string | null
  created_at: string
  is_mine: boolean
  owner_kind: FeishuPairingOwnerKind
  chat_id: string | null
}

export interface FeishuPairingClaim {
  pairing_id: string
  state: 'claimed'
  bot_id: string
  app_id: string
  tab_id: string
  workspace_id: string | null
  chat_id: string
  created_at: string
  expires_at: string
}

export interface FeishuBotSummary {
  bot_id: string
  name: string
  app_id: string
  source: FeishuBotSource
  enabled: boolean
  credentials_editable: boolean
  deletable: boolean
  revision: number
  generation: number
  configured: boolean
  app_secret_configured: boolean
  connection_status: FeishuBotConnectionStatus
  updated_at: string | null
  binding: FeishuActiveBinding | null
  my_claims: FeishuPairingClaim[]
}

export interface FeishuBotPoolResponse {
  pool_revision: number
  bots: FeishuBotSummary[]
  pool_editable: boolean
  deprecated_env: string[]
  focus_bot_id: string | null
}

export interface FeishuPairStartResponse {
  bot_id: string
  revision: number
  code: string
  expires_at: string
}

export interface FeishuBotCreateInput {
  name: string
  app_id: string
  app_secret: string
}
export interface FeishuBotSecretsInput {
  app_secret: string
  expected_revision: number
}
export interface FeishuBotPatchInput {
  name?: string
  enabled?: boolean
  expected_revision: number
}
export interface FeishuPairStartInput {
  tab_id: string
  workspace_id?: string | null
  expected_revision: number
}
export interface FeishuPairActivateInput {
  pairing_id: string
  confirm_word: string
  expected_revision: number
}

const ROOT = '/api/feishu/bot/bots'
const ERROR_CODES = new Set([
  'bot_revision_conflict', 'bot_pool_unavailable', 'bot_operation_busy',
  'hub_access_required', 'app_id_already_in_pool', 'app_id_immutable',
  'bot_already_bound', 'chat_already_bound', 'pairing_not_claimed',
  'pairing_confirmation_mismatch', 'pairing_expired', 'pairing_not_owned',
  'bot_disabled', 'bot_not_found', 'pair_code_rate_limited',
  'routing_cleanup_failed',
  'pool_capacity_reached', 'chat_tab_workspace_changed',
  'chat_tab_not_found', 'bot_credentials_read_only',
])

export class FeishuBotRequestError extends Error {
  readonly status: number
  readonly code: string | null
  constructor(status: number, code: string | null, message: string) {
    super(message)
    this.name = 'FeishuBotRequestError'
    this.status = status
    this.code = code
  }
}

function fixedMessage(status: number, code: string | null): string {
  if (status === 400 || status === 422) return 'The Bot request was rejected. Check the fields and try again.'
  if (status === 401) return 'Sign in to use the Feishu Bot pool.'
  if (status === 403) {
    if (code === 'pairing_not_owned') return 'This pairing claim belongs to another authorized operator.'
    return 'You do not have permission to perform this Bot operation.'
  }
  if (status === 404) {
    if (code === 'chat_tab_not_found') return 'This Chat no longer exists or is archived. Choose an active local Chat before pairing.'
    return 'This Bot no longer exists. Reload the Bot pool.'
  }
  if (status === 410) return 'The Hub Bot API has changed. Refresh this page or update the UI.'
  if (status === 429) return 'Too many pairing codes were requested. Wait and try again.'
  if (status === 502) return 'Feishu could not validate these credentials. Check them and try again.'
  if (status === 500) {
    if (code === 'routing_cleanup_failed') return 'The Bot may have changed, but routing cleanup failed. Reload the pool before another action.'
    return 'The Bot operation may have changed the pool. Reload before another action.'
  }
  if (status === 503) {
    if (code === 'bot_operation_busy') return 'Another Bot operation is in progress. Wait and try again.'
    return 'The Bot pool is unavailable or damaged. Check the Hub configuration.'
  }
  if (status === 409) {
    if (code === 'pool_capacity_reached') return 'Bot or pairing capacity has been reached. Remove an unused Bot or wait for pending pairing attempts to expire.'
    if (code === 'chat_tab_workspace_changed') return 'This Chat changed workspace. Let the current pairing attempt expire, then generate a new code for this Chat.'
    if (code === 'bot_credentials_read_only') return 'This Bot uses environment credentials. Change them in the Hub environment instead.'
    if (code === 'bot_revision_conflict') return 'This Bot changed. Reload it before trying again.'
    if (code === 'app_id_already_in_pool') return 'A Bot with this App ID already exists.'
    if (code === 'app_id_immutable') return 'A Bot App ID cannot be changed. Delete it and create another Bot.'
    if (code === 'bot_already_bound') return 'This Bot is already in use by another Chat.'
    if (code === 'chat_already_bound') return 'This Chat is already connected to another Bot.'
    if (code === 'pairing_not_claimed') return 'Send the pairing code to the Bot before activating it.'
    if (code === 'pairing_confirmation_mismatch') return 'The confirmation word does not match. Read it from your Feishu conversation and try again.'
    if (code === 'pairing_expired') return 'This pairing attempt expired. Generate a new code.'
    if (code === 'bot_disabled') return 'Enable this Bot before pairing it.'
    return 'The Bot pool changed. Reload it before trying again.'
  }
  return 'The Feishu Bot request failed. Try again.'
}

async function responseCode(response: Response): Promise<string | null> {
  try {
    const body = await response.json() as { detail?: unknown }
    const candidate = typeof body.detail === 'string'
      ? body.detail
      : body.detail && typeof body.detail === 'object' && 'code' in body.detail
        ? (body.detail as { code?: unknown }).code
        : null
    return typeof candidate === 'string' && ERROR_CODES.has(candidate) ? candidate : null
  } catch { return null }
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (!response.ok) {
    const code = await responseCode(response)
    throw new FeishuBotRequestError(response.status, code, fixedMessage(response.status, code))
  }
  return await response.json() as T
}

function jsonRequest(method: string, body: unknown, signal?: AbortSignal): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }
}
function botUrl(botId: string, suffix = ''): string {
  return `${ROOT}/${encodeURIComponent(botId)}${suffix}`
}

export function loadFeishuBotPool(signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(ROOT, { signal })
}
export function createFeishuBot(input: FeishuBotCreateInput, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(ROOT, jsonRequest('POST', input, signal))
}
export function replaceFeishuBotSecrets(botId: string, input: FeishuBotSecretsInput, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(botUrl(botId, '/secrets'), jsonRequest('PUT', input, signal))
}
export function updateFeishuBot(botId: string, input: FeishuBotPatchInput, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(botUrl(botId), jsonRequest('PATCH', input, signal))
}
export function deleteFeishuBot(botId: string, expectedRevision: number, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(botUrl(botId), jsonRequest('DELETE', { expected_revision: expectedRevision }, signal))
}
export function startFeishuPairing(botId: string, input: FeishuPairStartInput, signal?: AbortSignal): Promise<FeishuPairStartResponse> {
  return requestJson(botUrl(botId, '/pair/start'), jsonRequest('POST', input, signal))
}
export function activateFeishuPairing(botId: string, input: FeishuPairActivateInput, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(botUrl(botId, '/pair/activate'), jsonRequest('POST', input, signal))
}
export function disconnectFeishuPairing(botId: string, expectedRevision: number, signal?: AbortSignal): Promise<FeishuBotPoolResponse> {
  return requestJson(botUrl(botId, '/pairing'), jsonRequest('DELETE', { expected_revision: expectedRevision }, signal))
}
