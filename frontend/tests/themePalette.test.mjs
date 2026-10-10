import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import test from 'node:test'

const app = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const sourceRoot = new URL('../src/', import.meta.url)

async function loadVueSources() {
  const vuePaths = (await readdir(sourceRoot, { recursive: true }))
    .filter(path => path.endsWith('.vue'))
  return Promise.all(vuePaths.map(async path => ({
    path,
    source: await readFile(new URL(path, sourceRoot), 'utf8'),
  })))
}

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

test('dark theme uses a readable slate surface ladder and distinct interaction roles', () => {
  const dark = themeBlock(':root {')
  assert.equal(token(dark, 'ch-color-app-bg'), '#1f2023')
  assert.equal(token(dark, 'ch-color-canvas'), '#18191b')
  assert.equal(token(dark, 'ch-color-surface'), '#212225')
  assert.equal(token(dark, 'ch-color-surface-control'), '#2e3135')
  assert.equal(token(dark, 'ch-color-surface-selected'), '#293947')
  assert.equal(token(dark, 'ch-color-border-selected'), '#568aa8')
  assert.equal(token(dark, 'ch-color-text'), '#d7d9dd')
  assert.equal(token(dark, 'ch-color-text-selected'), '#e4f3fb')
  assert.equal(token(dark, 'ch-color-accent'), '#83b6d8')
  assert.ok(contrast(token(dark, 'ch-color-accent'), token(dark, 'ch-color-surface')) >= 7)
  assert.ok(contrast(token(dark, 'ch-color-accent-ring'), token(dark, 'ch-color-surface')) >= 3)
  assert.ok(contrast(token(dark, 'ch-color-accent-strong'), token(dark, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-text-selected'), token(dark, 'ch-color-surface-selected')) >= 7)
  assert.ok(contrast(token(dark, 'ch-color-border-selected'), token(dark, 'ch-color-surface-selected')) >= 3)
  assert.notEqual(token(dark, 'ch-color-surface-selected'), token(dark, 'ch-color-accent-strong'))
})

test('light theme mirrors the neutral ladder and readable blue roles', () => {
  const light = themeBlock(":root[data-theme='light']")
  assert.equal(token(light, 'ch-color-app-bg'), '#f3f4f6')
  assert.equal(token(light, 'ch-color-canvas'), '#eef0f2')
  assert.equal(token(light, 'ch-color-surface'), '#fbfbfc')
  assert.equal(token(light, 'ch-color-surface-selected'), '#e2edf4')
  assert.equal(token(light, 'ch-color-border-selected'), '#5f8aa5')
  assert.equal(token(light, 'ch-color-text'), '#25282d')
  assert.equal(token(light, 'ch-color-accent'), '#246f98')
  assert.ok(contrast(token(light, 'ch-color-accent'), token(light, 'ch-color-surface')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-accent-ring'), token(light, 'ch-color-surface')) >= 3)
  assert.ok(contrast(token(light, 'ch-color-accent-strong'), token(light, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-text-selected'), token(light, 'ch-color-surface-selected')) >= 7)
  assert.ok(contrast(token(light, 'ch-color-border-selected'), token(light, 'ch-color-surface-selected')) >= 3)
})

test('selected, focus, and primary-action roles remain separate in key controls', async () => {
  const [layout, pane, structured] = await Promise.all([
    readFile(new URL('../src/components/LayoutSelector.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8'),
  ])

  assert.match(app, /\.mode-button\.active[\s\S]*?background: var\(--ch-color-surface-selected\);[\s\S]*?border-color: var\(--ch-color-border-selected\);/)
  assert.match(app, /\.mode-button:focus-visible[\s\S]*?box-shadow: 0 0 0 3px var\(--ch-color-accent-ring\);/)
  assert.match(layout, /\.layout-btn\.active[\s\S]*?border-color: var\(--ch-color-border-selected\);[\s\S]*?background-color: var\(--ch-color-surface-selected\);/)
  assert.match(pane, /\.terminal-pane\.active[\s\S]*?border-color: var\(--ch-color-border-selected\);/)
  assert.match(structured, /\.conversation-avatar \{[\s\S]*?background: var\(--ch-color-surface-selected\);[\s\S]*?color: var\(--ch-color-text-selected\);/)
  assert.match(app, /\.ch-btn--primary \{[\s\S]*?background: var\(--ch-color-accent-strong\);/)
})

test('keyboard focus uses the solid ring rather than a translucent selected-state token', async () => {
  const vueSources = await loadVueSources()

  for (const { path, source } of vueSources) {
    for (const match of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const [, selector, declarations] = match
      if (!selector.includes(':focus-visible')) continue
      assert.doesNotMatch(
        declarations,
        /var\(--ch-color-accent-ring-strong\)/,
        `${path} ${selector.trim()} must use the solid --ch-color-accent-ring focus token`,
      )
    }
  }
})

test('filled accent controls use the contrast-safe strong role', async () => {
  const componentUrls = [
    '../src/components/AgentWorkspaceView.vue',
    '../src/components/GoalSetupDialog.vue',
    '../src/components/LayoutSelector.vue',
    '../src/components/StructuredPane.vue',
    '../src/components/TabBar.vue',
  ]
  const components = await Promise.all(componentUrls.map(url => (
    readFile(new URL(url, import.meta.url), 'utf8')
  )))

  const vueSources = await loadVueSources()

  for (const { path, source } of vueSources) {
    for (const match of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const [, selector, declarations] = match
      const usesForegroundAccentAsFill = /background(?:-color)?:\s*var\(--ch-color-accent\);/.test(declarations)
      const usesLightForeground = /(?:^|;)\s*color:\s*(?:#fff(?:fff)?|white|var\(--ch-color-(?:text-inverse|on-accent)(?:,\s*#fff)?\))/.test(declarations)
      assert.ok(
        !(usesForegroundAccentAsFill && usesLightForeground),
        `${path} ${selector.trim()} must use --ch-color-accent-strong for a light-text fill`,
      )
    }
  }

  assert.match(components[0], /\.lang-toggle-btn\.active[\s\S]*?background: var\(--ch-color-accent-strong\);/)
  assert.match(components[1], /button\.primary \{[^}]*background: var\(--ch-color-accent-strong\);/)
  assert.match(components[2], /\.user-avatar-fallback[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
  assert.match(components[3], /\.composer-send-btn[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
  assert.match(components[3], /\.approval-submit-btn[\s\S]*?background: var\(--ch-color-accent-strong\);/)
  assert.match(components[4], /\.pane-indicator[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
})
