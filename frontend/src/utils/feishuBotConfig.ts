export type FeishuBotConfigSource =
  | 'environment'
  | 'stored'
  | 'none'
  | 'invalid_environment'

export interface FeishuBotConfigStatus {
  configured: boolean
  source: FeishuBotConfigSource
  can_manage: boolean
  editable: boolean
  event_url: string | null
  revision: number | null
}

export interface FeishuBotAdminConfig extends FeishuBotConfigStatus {
  app_id: string | null
  app_secret_configured: boolean
  verification_token_configured: boolean
  encrypt_key_configured: boolean
}

export interface FeishuBotConfigInput {
  app_id: string
  app_secret: string
  verification_token: string
  encrypt_key: string
  expected_revision: number
}

const CONFIG_ERROR_CODES = new Set([
  'config_read_only',
  'config_revision_conflict',
  'app_id_change_confirmation_required',
  'oauth_app_mismatch',
  'routing_cleanup_failed',
  'public_url_invalid',
  'config_operation_busy',
  'bot_config_unavailable',
])

export class FeishuBotConfigRequestError extends Error {
  readonly status: number
  readonly code: string | null

  constructor(status: number, code: string | null, message: string) {
    super(message)
    this.name = 'FeishuBotConfigRequestError'
    this.status = status
    this.code = code
  }
}

function fixedErrorMessage(status: number, code: string | null): string {
  if (status === 400) return 'The Bot configuration was rejected. Check all fields and try again.'
  if (status === 401 || status === 403) return 'You do not have permission to manage the instance Bot.'
  if (status === 502) return 'Feishu could not validate this configuration. Check the credentials and network, then try again.'
  if (status === 500) {
    if (code === 'routing_cleanup_failed') {
      return 'The configuration may have changed, but binding cleanup failed. Check the latest configuration before taking another action.'
    }
    return 'The configuration may have changed. Reload configuration to check the latest state before trying again.'
  }
  if (status === 503) {
    if (code === 'config_operation_busy') return 'Another Bot configuration operation is in progress. Wait and try again.'
    if (code === 'public_url_invalid') return 'The instance public URL is invalid. Ask an administrator to fix it. A submitted configuration change may already have taken effect.'
    return 'The Bot configuration is unavailable or damaged. Contact an administrator.'
  }
  if (status === 409) {
    if (code === 'config_read_only') return 'This configuration is managed by environment variables and cannot be changed here.'
    if (code === 'config_revision_conflict') return 'The configuration changed. Review the latest state and enter all secrets again.'
    if (code === 'app_id_change_confirmation_required') return 'Changing the App ID requires confirmation.'
    if (code === 'oauth_app_mismatch') return 'The Bot App ID must match the configured Feishu sign-in application.'
    return 'The Bot configuration changed. Refresh and try again.'
  }
  return 'The Bot configuration request failed. Try again.'
}

async function errorCode(response: Response): Promise<string | null> {
  try {
    const body = await response.json() as { detail?: string | { code?: string } }
    const candidate = typeof body.detail === 'string'
      ? body.detail
      : body.detail?.code
    return typeof candidate === 'string' && CONFIG_ERROR_CODES.has(candidate)
      ? candidate
      : null
  } catch {
    // Error responses are deliberately mapped to fixed messages below.
  }
  return null
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (!response.ok) {
    const code = await errorCode(response)
    throw new FeishuBotConfigRequestError(response.status, code, fixedErrorMessage(response.status, code))
  }
  return await response.json() as T
}

export async function loadFeishuBotConfiguration(signal?: AbortSignal): Promise<{
  status: FeishuBotConfigStatus
  adminConfig: FeishuBotAdminConfig | null
}> {
  const status = await requestJson<FeishuBotConfigStatus>(
    '/api/feishu/bot/config/status',
    { signal },
  )
  const adminConfig = status.can_manage
    ? await requestJson<FeishuBotAdminConfig>('/api/feishu/bot/config', { signal })
    : null
  return { status, adminConfig }
}

export async function saveFeishuBotConfiguration(
  input: FeishuBotConfigInput,
  allowAppIdChange: boolean,
  signal?: AbortSignal,
): Promise<FeishuBotAdminConfig> {
  return await requestJson<FeishuBotAdminConfig>('/api/feishu/bot/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      ...input,
      allow_app_id_change: allowAppIdChange,
    }),
  })
}

export async function deactivateFeishuBotConfiguration(
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<FeishuBotAdminConfig> {
  return await requestJson<FeishuBotAdminConfig>('/api/feishu/bot/config', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({ expected_revision: expectedRevision }),
  })
}
