/** Paseo-compatible paced reveal for lumpy provider text deltas. */
export const TEXT_REVEAL_HORIZON_MS = 150
export const TEXT_REVEAL_FRAME_INTERVAL_MS = 1000 / 60
const MAX_ELAPSED_MS = 250

export interface TextRevealState {
  readonly target: string
  readonly revealed: number
}

export function computeRevealStep(input: {
  backlog: number
  elapsedMs: number
  horizonMs?: number
}): number {
  if (input.backlog <= 0) return 0
  const horizonMs = input.horizonMs ?? TEXT_REVEAL_HORIZON_MS
  if (horizonMs <= 0) return input.backlog
  const elapsedMs = Math.min(Math.max(input.elapsedMs, 0), MAX_ELAPSED_MS)
  if (elapsedMs <= 0) return 0
  if (elapsedMs >= horizonMs) return input.backlog
  return Math.min(input.backlog, Math.max(1, Math.ceil((input.backlog * elapsedMs) / horizonMs)))
}

type GraphemeSegments = { containing: (index: number) => { index: number } | undefined }
type GraphemeSegmenter = { segment: (text: string) => GraphemeSegments }
type GraphemeSegmenterConstructor = new (
  locales?: string | string[],
  options?: { granularity: 'grapheme' },
) => GraphemeSegmenter

const Segmenter = (Intl as unknown as { Segmenter?: GraphemeSegmenterConstructor }).Segmenter
const segmenter = Segmenter ? new Segmenter(undefined, { granularity: 'grapheme' }) : null
const ZERO_WIDTH_JOINER = 0x200d

export function isTextRevealPacingSupported(): boolean {
  return segmenter !== null
}

export function clampToSafeRevealBoundary(text: string, index: number): number {
  if (index <= 0) return 0
  if (index >= text.length) return text.length
  const segment = segmenter?.segment(text).containing(index)
  return segment?.index ?? 0
}

export function beginTextReveal(text: string): TextRevealState {
  return { target: text, revealed: text.length }
}

export function retargetTextReveal(state: TextRevealState, text: string): TextRevealState {
  if (state.target === text) return state
  return { target: text, revealed: Math.min(state.revealed, text.length) }
}

export function advanceTextReveal(
  state: TextRevealState,
  elapsedMs: number,
  horizonMs?: number,
): TextRevealState {
  const step = computeRevealStep({
    backlog: state.target.length - state.revealed,
    elapsedMs,
    ...(horizonMs !== undefined ? { horizonMs } : {}),
  })
  if (step <= 0) return state
  return { target: state.target, revealed: Math.min(state.target.length, state.revealed + step) }
}

export function completeTextReveal(state: TextRevealState): TextRevealState {
  return state.revealed >= state.target.length
    ? state
    : { target: state.target, revealed: state.target.length }
}

export function isTextRevealSettled(state: TextRevealState): boolean {
  return state.revealed >= state.target.length
}

function countTrailingRegionalIndicators(text: string): number {
  let count = 0
  let cursor = text.length
  while (cursor >= 2) {
    const codePoint = text.codePointAt(cursor - 2)
    if (codePoint === undefined || codePoint < 0x1f1e6 || codePoint > 0x1f1ff) break
    count += 1
    cursor -= 2
  }
  return count
}

function trimIncompleteTrailingCluster(text: string): string {
  if (text.length === 0) return text
  const lastUnit = text.charCodeAt(text.length - 1)
  if (lastUnit >= 0xd800 && lastUnit <= 0xdbff) return text.slice(0, -1)
  if (lastUnit === ZERO_WIDTH_JOINER) return text.slice(0, -1)
  if (countTrailingRegionalIndicators(text) % 2 === 1) return text.slice(0, -2)
  return text
}

export function visibleRevealedText(
  state: TextRevealState,
  options?: { streaming?: boolean },
): string {
  if (state.revealed >= state.target.length) {
    return options?.streaming ? trimIncompleteTrailingCluster(state.target) : state.target
  }
  if (!segmenter) return state.target
  return state.target.slice(0, clampToSafeRevealBoundary(state.target, state.revealed))
}

export interface TextRevealFrame {
  elapsedMs: number
  frameAtMs: number
}

export function nextTextRevealFrame(
  previousFrameAtMs: number | null,
  timestampMs: number,
): TextRevealFrame | null {
  const elapsedMs = previousFrameAtMs === null
    ? TEXT_REVEAL_FRAME_INTERVAL_MS
    : timestampMs - previousFrameAtMs
  if (elapsedMs < TEXT_REVEAL_FRAME_INTERVAL_MS) return null
  return {
    elapsedMs,
    frameAtMs: timestampMs - (elapsedMs % TEXT_REVEAL_FRAME_INTERVAL_MS),
  }
}
