/**
 * Presentation logic for the provider quota panel (`GET /api/quota`).
 *
 * Two properties of the contract drive this module:
 *
 * - A percentage is legitimately missing (`null`) when a provider reports no
 *   limit, no data yet, or an unrecognized state. Rendering those as `0%`
 *   would claim the quota is exhausted, so a missing number always resolves to
 *   a reason instead of a number.
 * - A reading can be frozen. Codex only reports after a turn finishes, so
 *   `as_of` can lag by days. Every provider therefore carries the age of its
 *   own reading, and a stale reading says so.
 *
 * Everything here is pure: `now` is injected so the output is testable.
 */
import type { ProviderQuota, QuotaLimit, QuotaResponse } from '@/types'

/** Display states; any unrecognized backend state collapses into `unknown`. */
export type QuotaDisplayState =
  | 'within'
  | 'near'
  | 'over'
  | 'no_limit'
  | 'no_data'
  | 'unknown'
  | 'unavailable'
  | 'not_configured'

/**
 * How a provider's message should read. `not_configured` is a fact about this
 * deployment, not a failure, so it must never borrow the alarm styling that
 * makes a real outage worth noticing.
 */
export type QuotaMessageTone = 'error' | 'neutral'

/** Balance colors are independent of the provider's enforcement status. */
export type QuotaBalanceTone = 'healthy' | 'caution' | 'low' | 'critical' | 'neutral'

export interface QuotaLimitRow {
  key: string
  label: string
  state: QuotaDisplayState
  tone: QuotaBalanceTone
  /** Primary text: a real percentage, or why there is no percentage. */
  value: string
  /** Remaining percentage as reported, or null when it is not a number. */
  remainingPercent: number | null
  /** Clamped 0-100 width for the bar; null means no bar may be drawn. */
  barPercent: number | null
  /** Secondary "x% used" text; empty when it would repeat `value`. */
  usedText: string
  resetText: string
  /** Raw ISO reset timestamp for a tooltip; empty when unknown. */
  resetTitle: string
}

export interface QuotaProviderView {
  key: string
  title: string
  state: QuotaDisplayState
  statusText: string
  /** Backend-supplied reason; empty when the provider answered normally. */
  message: string
  messageTone: QuotaMessageTone
  asOfText: string
  /** Raw ISO reading timestamp for a tooltip; empty when unknown. */
  asOfTitle: string
  stale: boolean
  staleNote: string
  /**
   * The reading is old enough that it describes the past. Distinct from
   * `stale`, which means the provider itself declared the reading invalid.
   */
  aging: boolean
  agingNote: string
  /** Set after a manual refresh that came back with the very same reading. */
  unchangedNote: string
  rows: QuotaLimitRow[]
  /** Shown instead of rows when the provider reported none. */
  emptyText: string
  notes: string[]
}

export interface QuotaViewOptions {
  /** Provider names proven unchanged by the last refresh, from `findUnchangedProviders`. */
  unchangedProviders?: readonly string[]
}

export const QUOTA_ENDPOINT = '/api/quota'

const MINUTE_MS = 60_000
const HOUR_MS = 3_600_000
const DAY_MS = 86_400_000

/**
 * Past this age a reading is presented as a last known value rather than a
 * current one.
 *
 * One hour is the smallest span that cannot be ordinary traffic: Codex
 * publishes a reading every time a turn finishes, so anyone actually working
 * refreshes it within minutes. An hour without one means nothing published
 * while the user kept going, which is exactly when an unqualified percentage
 * starts to mislead. Shorter would flag normal pauses; longer would leave the
 * "my Refresh is broken" case (a two hour old reading) looking current.
 */
export const AGING_READING_MS = HOUR_MS

const DISPLAY_STATES: readonly QuotaDisplayState[] = [
  'within', 'near', 'over', 'no_limit', 'no_data', 'unknown', 'unavailable', 'not_configured',
]

const STATE_LABELS: Record<QuotaDisplayState, string> = {
  within: 'Within limit',
  near: 'Near limit',
  over: 'Limit reached',
  no_limit: 'No limit',
  no_data: 'No data',
  unknown: 'Unknown',
  unavailable: 'Unavailable',
  not_configured: 'Not configured',
}

/** Text used when a row has no usable percentage. Never "0%". */
const MISSING_VALUE_TEXT: Record<QuotaDisplayState, string> = {
  within: 'Percentage unavailable',
  near: 'Percentage unavailable',
  over: 'Percentage unavailable',
  no_limit: 'No limit',
  no_data: 'No data yet',
  unknown: 'Unknown',
  unavailable: 'Unavailable',
  not_configured: 'Not configured',
}

const PROVIDER_TITLES: Record<string, string> = {
  codex: 'Codex',
  maso: 'MASO',
}

/**
 * Why a refresh can legitimately return the identical reading. Codex has a
 * hard constraint the Hub cannot work around: usage is only written when a
 * conversation turn finishes, so between turns every refresh re-reads the
 * same record. Saying nothing makes the button look broken.
 */
const UNCHANGED_REASONS: Record<string, string> = {
  codex: 'Refresh returned the same reading. Codex only publishes usage after a conversation '
    + 'turn finishes, so this will not change until one completes.',
}

const UNCHANGED_FALLBACK = 'Refresh returned the same reading; this provider has not published '
  + 'a newer one yet.'

function normalizeState(state: unknown): QuotaDisplayState {
  return DISPLAY_STATES.find(candidate => candidate === state) ?? 'unknown'
}

function finiteNumber(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatPercent(value: number): string {
  const rounded = Math.round(value * 10) / 10
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`
}

function balanceTone(state: QuotaDisplayState, remaining: number | null): QuotaBalanceTone {
  // A provider-declared warning must never become reassuring because of a
  // contradictory or missing percentage. Absence of a limit stays neutral.
  if (state === 'no_limit' || state === 'no_data') return 'neutral'
  if (state === 'over' || (remaining !== null && remaining <= 10)) return 'critical'
  if (state === 'near' || (remaining !== null && remaining <= 20)) return 'low'
  if (remaining === null) return 'neutral'
  return remaining <= 50 ? 'caution' : 'healthy'
}

function parseTime(iso: string | null | undefined): number | null {
  if (!iso) return null
  const value = new Date(iso).getTime()
  return Number.isNaN(value) ? null : value
}

/**
 * Coarse duration. Hours are kept up to two days and days up to three months
 * because these spans are rounded down: jumping to "1d" at 47 hours would
 * understate how far behind a reading is, which is exactly the mistake this
 * panel exists to prevent.
 */
function formatSpan(ms: number): string {
  const minutes = Math.floor(ms / MINUTE_MS)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(ms / HOUR_MS)
  if (hours < 48) return `${hours}h`
  const days = Math.floor(ms / DAY_MS)
  if (days < 90) return `${days}d`
  const months = Math.floor(days / 30)
  if (months < 24) return `${months}mo`
  return `${Math.floor(months / 12)}y`
}

/**
 * Age of a reading. A provider that never reported a timestamp says so rather
 * than looking current.
 */
export function formatAsOf(iso: string | null | undefined, now = Date.now()): string {
  const taken = parseTime(iso)
  if (taken === null) return 'Reading time unknown'
  const age = now - taken
  if (age < -MINUTE_MS) return 'Reading is timestamped in the future'
  if (age < MINUTE_MS) return 'Updated just now'
  return `Updated ${formatSpan(age)} ago`
}

/** Countdown to a reset, or empty when the provider reported no reset. */
export function formatReset(iso: string | null | undefined, now = Date.now()): string {
  const resets = parseTime(iso)
  if (resets === null) return ''
  const remaining = resets - now
  if (remaining <= 0) return 'Reset time has passed'
  if (remaining < MINUTE_MS) return 'Resets in under a minute'
  return `Resets in ${formatSpan(remaining)}`
}

function buildLimitRow(limit: QuotaLimit, index: number, now: number): QuotaLimitRow {
  const state = normalizeState(limit?.state)
  // `no_limit` and `no_data` describe the absence of a quota, so the state
  // wins even if the provider also sent a number.
  const stateWins = state === 'no_limit' || state === 'no_data'
  const remaining = stateWins ? null : finiteNumber(limit?.remaining_percent)
  const used = stateWins ? null : finiteNumber(limit?.used_percent)
  const showsRemaining = remaining !== null
  const value = showsRemaining
    ? `${formatPercent(remaining)} left`
    : used !== null
      ? `${formatPercent(used)} used`
      : MISSING_VALUE_TEXT[state]

  return {
    key: limit?.scope || `limit-${index}`,
    label: limit?.label || limit?.scope || 'Unnamed limit',
    state,
    tone: balanceTone(state, remaining),
    value,
    remainingPercent: remaining,
    barPercent: showsRemaining ? Math.min(100, Math.max(0, remaining)) : null,
    usedText: showsRemaining && used !== null ? `${formatPercent(used)} used` : '',
    resetText: formatReset(limit?.resets_at, now),
    resetTitle: limit?.resets_at || '',
  }
}

function buildNotes(provider: ProviderQuota): string[] {
  const notes: string[] = []
  if (provider.plan) notes.push(`Plan: ${provider.plan}`)
  if (provider.credit_balance) {
    // Keep the provider's exact decimal value without converting it to a
    // floating-point number just to remove insignificant trailing zeroes.
    const balance = provider.credit_balance.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')
    notes.push(`Prepaid credit: ${balance} (unit not reported)`)
  }
  if (provider.enforcing === false) {
    notes.push('Reaching a limit does not refuse requests here, so these percentages are advisory.')
  } else if (provider.enforcing === true) {
    notes.push('Requests are refused once a limit is reached.')
  }
  return notes
}

/** One provider block: status, reading age, rows and provider-specific notes. */
export function buildProviderView(
  provider: ProviderQuota,
  now = Date.now(),
  unchanged = false,
): QuotaProviderView {
  const available = provider.available === true
  const reported = normalizeState(provider.state)
  // A provider this deployment never configured is not a failed one: the
  // state survives `available: false` instead of collapsing into an outage.
  const state = reported === 'not_configured'
    ? 'not_configured'
    : available ? reported : 'unavailable'
  const configured = state !== 'not_configured'
  const stale = provider.stale === true
  const limits = Array.isArray(provider.limits) ? provider.limits : []
  const rows = limits.map((limit, index) => buildLimitRow(limit, index, now))
  const taken = parseTime(provider.as_of)
  // A provider that already declared the reading invalid says the stronger
  // thing; do not stack a second caution on top of it.
  const aging = !stale && taken !== null && now - taken >= AGING_READING_MS
  const fallbackMessage = !configured
    ? 'This Hub is not configured for this provider.'
    : available ? '' : 'This provider did not report any quota.'

  return {
    key: provider.provider || `provider-${state}`,
    title: PROVIDER_TITLES[provider.provider] || provider.provider || 'Unknown provider',
    state,
    statusText: STATE_LABELS[state],
    message: provider.message || fallbackMessage,
    messageTone: configured ? 'error' : 'neutral',
    // An unconfigured provider has no reading at all, so "Reading time
    // unknown" would be noise suggesting something should have been there.
    asOfText: configured ? formatAsOf(provider.as_of, now) : '',
    asOfTitle: provider.as_of || '',
    stale,
    staleNote: stale
      ? 'This reading is out of date; actual usage may already be higher.'
      : '',
    aging,
    agingNote: aging
      ? 'These percentages are the last known values, not a live reading.'
      : '',
    unchangedNote: unchanged
      ? UNCHANGED_REASONS[provider.provider] || UNCHANGED_FALLBACK
      : '',
    rows,
    emptyText: rows.length || !available ? '' : 'This provider reported no limits.',
    notes: buildNotes(provider),
  }
}

/** Provider blocks in backend order; a missing payload renders nothing. */
export function buildQuotaView(
  response: QuotaResponse | null | undefined,
  now = Date.now(),
  options: QuotaViewOptions = {},
): QuotaProviderView[] {
  const providers = Array.isArray(response?.providers) ? response.providers : []
  const unchanged = options.unchangedProviders ?? []
  return providers.map(provider => buildProviderView(
    provider,
    now,
    unchanged.includes(provider.provider),
  ))
}

/**
 * Providers whose refresh produced the exact same reading.
 *
 * Only a provider that timestamped both readings can be proven unchanged: a
 * missing `as_of` means the age is unknown, not that nothing moved, and an
 * unavailable provider has no reading to compare at all. Being conservative
 * here keeps the "no newer reading" notice away from MASO, which really does
 * advance on every refresh.
 */
export function findUnchangedProviders(
  previous: QuotaResponse | null | undefined,
  next: QuotaResponse | null | undefined,
): string[] {
  const before = new Map<string, string>()
  for (const provider of previous?.providers ?? []) {
    if (provider.available === true && provider.as_of) before.set(provider.provider, provider.as_of)
  }
  return (next?.providers ?? [])
    .filter(provider => provider.available === true
      && !!provider.as_of
      && before.get(provider.provider) === provider.as_of)
    .map(provider => provider.provider)
}

/**
 * One-line headline for the collapsed menu entry. It reports the lowest
 * remaining percentage across every provider, and never hides an unavailable
 * provider, a stale reading or an aging one behind a healthy number.
 *
 * A provider this deployment never configured is deliberately silent here: a
 * Codex-only Hub must not look like a Hub with something broken in it.
 */
export function summarizeQuota(views: QuotaProviderView[]): string {
  if (!views.length) return 'No data'
  const remaining = views
    .flatMap(view => view.rows)
    .map(row => row.remainingPercent)
    .filter((value): value is number => value !== null)
  const configured = views.filter(view => view.state !== 'not_configured')
  const unavailable = configured.filter(view => view.state === 'unavailable').length

  const parts: string[] = []
  if (remaining.length) parts.push(`Lowest ${formatPercent(Math.min(...remaining))} left`)
  else if (!configured.length) parts.push('Not configured')
  else if (unavailable === configured.length) parts.push('Unavailable')
  else parts.push('No data')
  if (remaining.length && unavailable) {
    parts.push(`${unavailable} provider${unavailable > 1 ? 's' : ''} unavailable`)
  }
  // "stale" is the stronger claim and already implies the number is not live.
  if (views.some(view => view.stale)) parts.push('stale')
  else if (views.some(view => view.aging)) parts.push('last known')
  return parts.join(' · ')
}

/** `refresh=true` asks the backend for a new reading instead of a cached one. */
export function quotaRequestUrl(refresh: boolean): string {
  return refresh ? `${QUOTA_ENDPOINT}?refresh=true` : QUOTA_ENDPOINT
}
