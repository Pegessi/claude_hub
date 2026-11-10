import type { ChatRuntimeStatus } from '../owners'
import type { AgentRuntimeStatus } from '@/types'

/** One tab's polled status, as the backend sends it. */
export interface RuntimeStatusSample {
  status: AgentRuntimeStatus
  activity?: string | null
  sampled_at?: string | null
}

/** A polled sample older than this is worse than no answer at all. */
export const RUNTIME_STATUS_MAX_AGE_MS = 60_000

/** Server timestamps can arrive without a zone — ``2026-11-10T17:18:49.175424``.
 *
 *  ``Date.parse`` would read those as *local* time, so an eight-hour-old sample
 *  looks current to a +8 client (and a future-looking one to anywhere else).
 *  Treat a missing offset as UTC, which is what the Hub writes. */
function sampledAtMs(raw: string): number {
  const normalised = /(Z|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`
  return Date.parse(normalised)
}

/** Reduce a polled status to what a footer sentence needs, or ``null``. */
export function runtimeStatusFor(
  sample: RuntimeStatusSample | null | undefined,
  now: number = Date.now(),
): ChatRuntimeStatus | null {
  if (!sample) return null
  const parsed = sample.sampled_at ? sampledAtMs(sample.sampled_at) : Number.NaN
  // No timestamp is treated as "just now": the payload came from a live poll,
  // and dropping it would trade a fact for silence.
  const ageMs = Number.isNaN(parsed) ? 0 : Math.max(0, now - parsed)
  if (ageMs > RUNTIME_STATUS_MAX_AGE_MS) return null
  return { status: sample.status, activity: sample.activity ?? null, ageMs }
}
