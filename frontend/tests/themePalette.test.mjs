import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const app = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')

function themeBlock(selector) {
  const start = app.indexOf(selector)
  assert.notEqual(start, -1, `${selector} block exists`)
  const open = app.indexOf('{', start)
  const close = app.indexOf('}', open)
  return app.slice(open + 1, close)
}

function token(block, name) {
  const match = block.match(new RegExp(`--${name}\\s*:\\s*([^;]+);`))
  assert.ok(match, `--${name} exists`)
  return match[1].trim()
}

function relativeLuminance(hex) {
  const channels = hex.slice(1).match(/../g).map(part => Number.parseInt(part, 16) / 255)
  const linear = channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
}

function contrast(a, b) {
  const [lighter, darker] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x)
  return (lighter + 0.05) / (darker + 0.05)
}

test('dark theme keeps the Codex-inspired neutral canvas and warm accent', () => {
  const dark = themeBlock(':root {')
  assert.equal(token(dark, 'ch-color-app-bg'), '#10100f')
  assert.equal(token(dark, 'ch-color-surface'), '#171716')
  assert.equal(token(dark, 'ch-color-text'), '#e6e4df')
  assert.equal(token(dark, 'ch-color-accent'), '#e58a68')
  assert.ok(contrast(token(dark, 'ch-color-accent-strong'), token(dark, 'ch-color-text-inverse')) >= 4.5)
})

test('light theme keeps the warm paper hierarchy and readable accent', () => {
  const light = themeBlock(":root[data-theme='light']")
  assert.equal(token(light, 'ch-color-app-bg'), '#e9e8e4')
  assert.equal(token(light, 'ch-color-surface'), '#f8f7f4')
  assert.equal(token(light, 'ch-color-text'), '#292724')
  assert.equal(token(light, 'ch-color-accent'), '#b25539')
  assert.ok(contrast(token(light, 'ch-color-accent'), token(light, 'ch-color-surface')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-accent-strong'), '#fffaf6') >= 4.5)
})
