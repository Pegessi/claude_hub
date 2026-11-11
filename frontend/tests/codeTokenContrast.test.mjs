import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// ---- WCAG relative luminance / contrast -------------------------------------
// Code tokens are small text (12.5px) on the code block's own background, so 4.5:1
// (the AA threshold for normal-size text) is the floor, and it has to hold in
// both themes: a palette that reads well in dark mode is not automatically
// legible in light mode.
function luminance(hex) {
  const value = hex.replace('#', '')
  const full = value.length === 3 ? value.split('').map(c => c + c).join('') : value
  const [r, g, b] = [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16) / 255)
  const linear = channel => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.05) / 1.055) ** 2.4)
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const THEME_CSS = readFileSync(new URL('../src/features/app-shell/styles/theme.css', import.meta.url), 'utf8')
const MIN_CONTRAST = 4.5

function themeBlock(startPattern) {
  const index = THEME_CSS.indexOf(startPattern)
  assert.ok(index >= 0, `theme block ${startPattern} is missing`)
  const end = THEME_CSS.indexOf('\n}', index)
  return THEME_CSS.slice(index, end)
}

function tokensOf(block) {
  const tokens = {}
  for (const [, name, colour] of block.matchAll(/--ch-code-(\w+):\s*(#[0-9a-fA-F]{3,8})/g)) {
    tokens[name] = colour
  }
  return tokens
}

const THEMES = [
  ['dark', themeBlock(':root {')],
  ['light', themeBlock(":root[data-theme='light']")],
]

for (const [theme, block] of THEMES) {
  test(`every code token stays legible on the code block in the ${theme} theme`, () => {
    const background = block.match(/--ch-color-canvas:\s*(#[0-9a-fA-F]{3,8})/)?.[1]
    assert.ok(background, 'the theme defines the code block background')
    const tokens = tokensOf(block)
    assert.ok(Object.keys(tokens).length >= 15, `the ${theme} theme defines the code palette`)
    const failing = Object.entries(tokens)
      .map(([name, colour]) => ({ name, colour, ratio: contrast(colour, background) }))
      .filter(entry => entry.ratio < MIN_CONTRAST)
    assert.deepEqual(
      failing.map(entry => `${entry.name} ${entry.colour} → ${entry.ratio.toFixed(2)}:1`),
      [],
      `tokens below ${MIN_CONTRAST}:1 on ${background}`,
    )
  })
}

test('both themes name the same code tokens', () => {
  // A token defined only in one theme falls back to the other theme's colour,
  // which is how a palette silently drifts out of legibility in one of them.
  const [dark, light] = THEMES.map(([, block]) => Object.keys(tokensOf(block)).sort())
  assert.deepEqual(light, dark)
})
