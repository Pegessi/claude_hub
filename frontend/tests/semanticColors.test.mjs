import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

// Regression test for the status-color desaturation regression. The semantic
// status colors (success/warning/attention/danger) must be saturated enough to
// be readable in both themes. We parse App.vue's CSS custom properties and
// assert each main status color clears a minimum HSL saturation floor.

const appVue = await readFile(
  new URL('../src/App.vue', import.meta.url),
  'utf8',
)

// Match ONLY the bare semantic color declarations
// (--ch-color-success, --ch-color-warning, --ch-color-attention,
// --ch-color-danger) and NOT the suffixed variants (-strong, -hover,
// -bg, -border, -text). The bare color is the primary semantic token
// used for status dots/badges; suffixed variants are derived accents.
// The dark-theme block appears first in the file, light-theme second,
// so the first bare match per name is dark and the second is light.
const colorDeclRe = /--ch-color-(success|warning|attention|danger)\s*:\s*([^;]+);/g

/** Parse a hex (#rgb / #rrggbb) or rgba(r,g,b,a) string into [r,g,b] 0-255. */
function parseColor(value) {
  const v = value.trim()
  if (v.startsWith('#')) {
    let hex = v.slice(1)
    if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('')
    const num = parseInt(hex, 16)
    return [(num >> 16) & 255, (num >> 8) & 255, num & 255]
  }
  const m = v.match(/rgba?\(([^)]+)\)/)
  if (m) {
    const parts = m[1].split(',').map((s) => parseFloat(s.trim()))
    return [parts[0], parts[1], parts[2]]
  }
  return null
}

/** Convert [r,g,b] 0-255 to {h,s,l} with s,l in [0,1]. */
function rgbToHsl([r, g, b]) {
  r /= 255; g /= 255; b /= 255
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  let h = 0
  const l = (max + min) / 2
  let s = 0
  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break
      case g: h = ((b - r) / d + 2) / 6; break
      case b: h = ((r - g) / d + 4) / 6; break
    }
  }
  return { h, s, l }
}

// Collect all declarations in file order. The first occurrence of each main
// color (success, warning, attention, danger) is the dark-theme value; the
// second is the light-theme value.
const mainColors = ['success', 'warning', 'attention', 'danger']
const byName = new Map(mainColors.map((n) => [n, []]))

let match
while ((match = colorDeclRe.exec(appVue)) !== null) {
  const [, name, value] = match
  // Only track the bare color (not -strong/-hover/-bg etc.) for saturation.
  if (!mainColors.includes(name)) continue
  byName.get(name).push(value.trim())
}

// Minimum saturation floor. The pre-regression (desaturated) palette sat
// around 0.35-0.55; the restored Codex-style palette sits at 0.7+.
const SATURATION_FLOOR = 0.6

test('dark-theme status colors are saturated above the readability floor', () => {
  for (const name of mainColors) {
    const values = byName.get(name)
    assert.ok(values.length >= 1, `dark --ch-color-${name} not found`)
    const rgb = parseColor(values[0])
    assert.ok(rgb, `could not parse dark --ch-color-${name}: ${values[0]}`)
    const { s } = rgbToHsl(rgb)
    assert.ok(
      s >= SATURATION_FLOOR,
      `dark --ch-color-${name} saturation ${s.toFixed(3)} below floor ${SATURATION_FLOOR} (value=${values[0]})`,
    )
  }
})

test('light-theme status colors are saturated above the readability floor', () => {
  for (const name of mainColors) {
    const values = byName.get(name)
    assert.ok(values.length >= 2, `light --ch-color-${name} not found`)
    const rgb = parseColor(values[1])
    assert.ok(rgb, `could not parse light --ch-color-${name}: ${values[1]}`)
    const { s } = rgbToHsl(rgb)
    assert.ok(
      s >= SATURATION_FLOOR,
      `light --ch-color-${name} saturation ${s.toFixed(3)} below floor ${SATURATION_FLOOR} (value=${values[1]})`,
    )
  }
})
