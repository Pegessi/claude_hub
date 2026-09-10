/** Wall-clock parsing and formatting shared by the timelines.
 *
 *  Extracted from ``AgentWorkspaceView`` so the Chat timeline and the
 *  workspace progress timeline read the same way instead of drifting apart.
 */

/** Parse an ISO-8601 timestamp into epoch milliseconds.
 *
 *  Returns ``null`` for a missing or unparseable value so callers can omit a
 *  label rather than render ``NaN``. */
export function parseTimestampMs(value?: string | null): number | null {
  if (!value) return null
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? timestamp : null
}

/** Render an ISO-8601 timestamp as a wall-clock label — ``17:27``.
 *
 *  Local time, 24-hour, no seconds: this sits beside the actions under a
 *  message, where the reader only needs to place it in the day. Returns an
 *  empty string for a missing or unparseable value so callers can drop the
 *  label rather than render ``NaN:NaN``. */
export function formatClockTime(value?: string | null): string {
  const ms = parseTimestampMs(value)
  if (ms === null) return ''
  const date = new Date(ms)
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${hours}:${minutes}`
}

/** Render a duration as a coarse, human-readable span.
 *
 *  Floored to whole units on purpose: ``45s``, ``3m``, ``2h 5m``, ``3d``. A
 *  progress label reads better rounded down than padded with a second unit the
 *  reader has to parse. */
export function formatElapsedDuration(valueMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(valueMs / 1000))
  if (totalSeconds < 60) return `${totalSeconds}s`

  const totalMinutes = Math.floor(totalSeconds / 60)
  if (totalMinutes < 60) return `${totalMinutes}m`

  const totalHours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (totalHours < 24) {
    return minutes > 0 && totalHours < 12 ? `${totalHours}h ${minutes}m` : `${totalHours}h`
  }

  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  return hours > 0 && days < 14 ? `${days}d ${hours}h` : `${days}d`
}
