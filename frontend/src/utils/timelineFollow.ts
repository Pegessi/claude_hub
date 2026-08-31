export interface TimelineScrollMetrics {
  scrollTop: number
  clientHeight: number
  scrollHeight: number
}

export const TIMELINE_FOLLOW_THRESHOLD_PX = 64

/**
 * Whether a conversation viewport is close enough to its tail to keep
 * following content growth. Non-measurable layouts default to following so a
 * newly mounted pane can settle at the latest turn once it receives geometry.
 */
export function isTimelineNearBottom(
  metrics: TimelineScrollMetrics,
  thresholdPx = TIMELINE_FOLLOW_THRESHOLD_PX,
): boolean {
  const values = [metrics.scrollTop, metrics.clientHeight, metrics.scrollHeight]
  if (!values.every(Number.isFinite)) return true
  if (metrics.clientHeight <= 0 || metrics.scrollHeight <= 0) return true
  const threshold = Number.isFinite(thresholdPx) ? Math.max(0, thresholdPx) : 0
  return metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop <= threshold
}
