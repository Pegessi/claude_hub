/**
 * Geometry for the working-session light in the Chat sidebar.
 *
 * The border beam travels clockwise around the row: along the top edge, down
 * the right side, back along the bottom edge and up the left side. Each star
 * inside the row is ignited when the beam passes the column it sits in, so
 * the stars form a trail that moves with the beam instead of running on an
 * independent timeline.
 */

/**
 * Share of one orbit spent crossing the top edge (and again the bottom edge)
 * of a typical sidebar row (~220px wide, ~33px tall, 6px radius). The CSS
 * light-pool keyframes in ChatSidebarWorkingEffect.vue hard-code the same
 * split as 42.5% / 50% / 92.5%.
 */
export const WORKING_BEAM_EDGE_SHARE = 0.425

/**
 * Beam pulse lengths in SVG pathLength units, where the whole border is 100:
 * a long fading tail behind the peak and a short fade ahead of it. The CSS
 * orbit animation starts its dash offset at WORKING_BEAM_TAIL so the peak
 * sits on the orbit phase the stars follow.
 */
export const WORKING_BEAM_TAIL = 18
export const WORKING_BEAM_LEAD = 3

export type WorkingStarEdge = 'top' | 'bottom'

export interface WorkingStar {
  /** Horizontal position, percent of the row width. */
  x: number
  /** Vertical position, percent of the row height. */
  y: number
  /** Diameter in CSS pixels. */
  size: number
  /** Orbit fraction in [0, 1) at which the beam passes this star. */
  pass: number
  kind: 'dust' | 'glint'
}

/** Orbit fraction in [0, 1) at which the beam head is above or below `x`. */
export function beamPassFraction(x: number, edge: WorkingStarEdge): number {
  const column = Math.min(1, Math.max(0, x))
  const pass = edge === 'top'
    ? WORKING_BEAM_EDGE_SHARE * column
    : 0.5 + WORKING_BEAM_EDGE_SHARE * (1 - column)
  return Math.min(pass, 0.999)
}

function hashSeed(text: string): number {
  let hash = 0x811c9dc5
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return hash >>> 0
}

function seededRandom(seed: number): () => number {
  let state = seed || 1
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1)
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61)
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

const round = (value: number, digits = 2) => Number(value.toFixed(digits))

/**
 * Stroke dash arrays for the stacked beam layers. Every dash straddles the
 * same peak and each layer is shorter than the one below it, so the
 * translucent overlap brightens smoothly toward the peak and fades out at
 * both ends instead of ending in a hard segment.
 */
export function createWorkingBeamLayers(count = 12): string[] {
  return Array.from({ length: count }, (_, index) => {
    const reach = (count - index) / count
    const start = round(WORKING_BEAM_TAIL * (1 - reach ** 1.5), 3)
    const length = round(WORKING_BEAM_TAIL + WORKING_BEAM_LEAD * reach - start, 3)
    return `0 ${start} ${length} ${round(100 - start - length, 3)}`
  })
}

/**
 * A deterministic star field for one row. Columns are stratified so the
 * trail never clumps, and each star joins the top or bottom pass according
 * to the half of the row it sits in.
 */
export function createWorkingStarfield(seed: string, dustCount = 14): WorkingStar[] {
  const random = seededRandom(hashSeed(seed))
  const stars: WorkingStar[] = []
  for (let index = 0; index < dustCount; index += 1) {
    const column = (index + 0.15 + random() * 0.7) / dustCount
    const edge: WorkingStarEdge = random() < 0.5 ? 'top' : 'bottom'
    const offset = 0.14 + random() * 0.32
    stars.push({
      x: round(4 + column * 92),
      y: round((edge === 'top' ? offset : 1 - offset) * 100),
      size: round(0.9 + random() * random() * 1.1),
      pass: round(beamPassFraction(column, edge), 4),
      kind: 'dust',
    })
  }
  for (const edge of ['top', 'bottom'] as const) {
    const column = 0.28 + random() * 0.44
    const offset = 0.3 + random() * 0.12
    stars.push({
      x: round(4 + column * 92),
      y: round((edge === 'top' ? offset : 1 - offset) * 100),
      size: 9,
      pass: round(beamPassFraction(column, edge), 4),
      kind: 'glint',
    })
  }
  return stars
}
