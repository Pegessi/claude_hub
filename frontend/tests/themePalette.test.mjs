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

function styleBlock(source, selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = source.match(new RegExp(`^\\s*${escapedSelector}\\s*\\{([^}]*)\\}`, 'm'))
  assert.ok(match, `${selector} style block exists`)
  return match[1]
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

function hsl(hex) {
  const [red, green, blue] = hex.slice(1).match(/../g).map(part => Number.parseInt(part, 16) / 255)
  const max = Math.max(red, green, blue)
  const min = Math.min(red, green, blue)
  const delta = max - min
  const lightness = (max + min) / 2
  let hue = 0
  if (delta && max === red) hue = 60 * (((green - blue) / delta) % 6)
  if (delta && max === green) hue = 60 * (((blue - red) / delta) + 2)
  if (delta && max === blue) hue = 60 * (((red - green) / delta) + 4)
  if (hue < 0) hue += 360
  const saturation = delta ? delta / (1 - Math.abs(2 * lightness - 1)) * 100 : 0
  return { hue, saturation }
}

function hueDistance(a, b) {
  const distance = Math.abs(a - b)
  return Math.min(distance, 360 - distance)
}

test('dark theme uses the Maso carbon ladder and main-branch blue roles', () => {
  const dark = themeBlock(':root {')
  assert.equal(token(dark, 'ch-color-app-bg'), '#1b1b1c')
  assert.equal(token(dark, 'ch-color-main-bg'), '#181818')
  assert.equal(token(dark, 'ch-color-sidebar-bg'), '#1c1c1c')
  assert.equal(token(dark, 'ch-color-canvas'), '#171718')
  assert.equal(token(dark, 'ch-color-surface'), '#1d1d1f')
  assert.equal(token(dark, 'ch-color-surface-control'), '#29292c')
  assert.equal(token(dark, 'ch-color-surface-selected'), '#2a2f36')
  assert.equal(token(dark, 'ch-color-border-selected'), '#5a9cf0')
  assert.equal(token(dark, 'ch-color-text'), '#ececef')
  assert.equal(token(dark, 'ch-color-text-selected'), '#eef6ff')
  assert.equal(token(dark, 'ch-color-accent'), '#7cb7ff')
  assert.equal(token(dark, 'ch-color-accent-strong'), '#3276ba')
  assert.equal(token(dark, 'ch-color-accent-hover'), '#4a8ce0')
  assert.equal(token(dark, 'ch-color-accent-fill-hover'), '#3478bb')
  assert.equal(token(dark, 'ch-color-accent-soft'), 'rgba(90, 156, 240, 0.16)')
  assert.equal(token(dark, 'ch-color-session-glow'), '#5a9cf0')
  assert.equal(token(dark, 'ch-color-message-user-bg'), '#3276ba')
  assert.equal(token(dark, 'ch-color-message-user-muted'), '#f7fafc')
  assert.equal(token(dark, 'ch-color-message-user-error'), '#fffafa')
  assert.ok(contrast(token(dark, 'ch-color-accent'), token(dark, 'ch-color-surface')) >= 7)
  assert.ok(contrast(token(dark, 'ch-color-accent-ring'), token(dark, 'ch-color-surface')) >= 3)
  assert.ok(contrast(token(dark, 'ch-color-accent-strong'), token(dark, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-accent-fill-hover'), token(dark, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-message-user-bg'), token(dark, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-message-user-muted'), token(dark, 'ch-color-message-user-bg')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-message-user-error'), token(dark, 'ch-color-message-user-bg')) >= 4.5)
  assert.ok(contrast(token(dark, 'ch-color-message-user-focus'), token(dark, 'ch-color-message-user-bg')) >= 3)
  assert.ok(contrast(token(dark, 'ch-color-text-selected'), token(dark, 'ch-color-surface-selected')) >= 7)
  assert.ok(contrast(token(dark, 'ch-color-border-selected'), token(dark, 'ch-color-surface-selected')) >= 3)
  assert.notEqual(token(dark, 'ch-color-surface-selected'), token(dark, 'ch-color-accent-strong'))
})

test('light theme pairs the neutral ladder with distinct muted clay-orange roles', () => {
  const light = themeBlock(":root[data-theme='light']")
  assert.equal(token(light, 'ch-color-app-bg'), '#f4f4f5')
  assert.equal(token(light, 'ch-color-main-bg'), '#ffffff')
  assert.equal(token(light, 'ch-color-sidebar-bg'), '#f5f5f5')
  assert.equal(token(light, 'ch-color-canvas'), '#f0f0f2')
  assert.equal(token(light, 'ch-color-surface'), '#fafafa')
  assert.equal(token(light, 'ch-color-surface-selected'), '#f4eae3')
  assert.equal(token(light, 'ch-color-border-selected'), '#a9795d')
  assert.equal(token(light, 'ch-color-text'), '#25282d')
  assert.equal(token(light, 'ch-color-accent'), '#976549')
  assert.equal(token(light, 'ch-color-accent-fill-hover'), '#754a33')
  assert.equal(token(light, 'ch-color-session-glow'), '#a9795d')
  assert.equal(token(light, 'ch-color-message-user-bg'), '#82533a')
  assert.equal(token(light, 'ch-color-message-user-muted'), '#e4d9d4')
  assert.equal(token(light, 'ch-color-message-user-error'), '#ffdada')
  assert.ok(contrast(token(light, 'ch-color-accent'), token(light, 'ch-color-surface')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-accent-ring'), token(light, 'ch-color-surface')) >= 3)
  assert.ok(contrast(token(light, 'ch-color-accent-strong'), token(light, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-accent-fill-hover'), token(light, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-message-user-bg'), token(light, 'ch-color-text-inverse')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-message-user-muted'), token(light, 'ch-color-message-user-bg')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-message-user-error'), token(light, 'ch-color-message-user-bg')) >= 4.5)
  assert.ok(contrast(token(light, 'ch-color-message-user-focus'), token(light, 'ch-color-message-user-bg')) >= 3)
  assert.ok(contrast(token(light, 'ch-color-text-selected'), token(light, 'ch-color-surface-selected')) >= 7)
  assert.ok(contrast(token(light, 'ch-color-border-selected'), token(light, 'ch-color-surface-selected')) >= 3)

  const accent = hsl(token(light, 'ch-color-accent'))
  for (const semanticToken of ['ch-color-danger', 'ch-color-warning']) {
    const semantic = hsl(token(light, semanticToken))
    assert.ok(
      hueDistance(accent.hue, semantic.hue) >= 18
        || Math.abs(accent.saturation - semantic.saturation) >= 40,
      `Light accent must remain visually distinct from --${semanticToken}`,
    )
  }
})

test('text roles stay readable and ordered on the primary planes of both themes', () => {
  const textRoles = ['ch-color-text', 'ch-color-text-muted', 'ch-color-text-soft', 'ch-color-text-subtle']
  for (const selector of [':root {', ":root[data-theme='light']"]) {
    const theme = themeBlock(selector)
    const main = token(theme, 'ch-color-main-bg')
    assert.ok(contrast(token(theme, 'ch-color-text'), main) >= 14, `${selector} body text matches Maso's reading contrast`)
    for (const plane of ['ch-color-main-bg', 'ch-color-sidebar-bg', 'ch-color-surface']) {
      for (const role of textRoles) {
        assert.ok(
          contrast(token(theme, role), token(theme, plane)) >= 4.5,
          `${selector} --${role} meets WCAG AA on --${plane}`,
        )
      }
    }
    const ladder = textRoles.map(role => contrast(token(theme, role), main))
    for (let index = 1; index < ladder.length; index += 1) {
      assert.ok(ladder[index] < ladder[index - 1], `${selector} --${textRoles[index]} stays below --${textRoles[index - 1]}`)
    }
  }
})

test('Chat messages use the reading size while compact Markdown stays dense', async () => {
  const [structured, markdown] = await Promise.all([
    readFile(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/MarkdownContent.vue', import.meta.url), 'utf8'),
  ])
  const chatMarkdown = styleBlock(structured, '.conversation-bubble :deep(.markdown-content.compact)')
  const compactMarkdown = styleBlock(markdown, '.markdown-content.compact')

  assert.match(chatMarkdown, /font-size: 14px;/)
  assert.match(chatMarkdown, /line-height: 1\.65;/)
  assert.match(compactMarkdown, /font-size: 12px;/)
})

test('selected, focus, and primary-action roles remain separate in key controls', async () => {
  const [layout, pane, structured] = await Promise.all([
    readFile(new URL('../src/components/LayoutSelector.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8'),
  ])
  const userBubble = styleBlock(structured, '.conversation-bubble--user')
  const messageSource = styleBlock(structured, '.conversation-message-source')
  const attachmentPlaceholder = styleBlock(structured, '.turn-attachment-placeholder')
  const userAttachmentPlaceholder = styleBlock(structured, '.conversation-bubble--user .turn-attachment-placeholder')
  const agentImageMissing = styleBlock(structured, '.agent-image-missing')
  const editFocus = styleBlock(structured, '.edit-resend-textarea:focus-visible')
  const editPlaceholder = styleBlock(structured, '.edit-resend-textarea::placeholder')
  const editError = styleBlock(structured, '.edit-resend-error')

  assert.match(app, /\.mode-button\.active[\s\S]*?background: var\(--ch-color-surface-selected\);[\s\S]*?border-color: var\(--ch-color-border-selected\);/)
  assert.match(app, /\.mode-button:focus-visible[\s\S]*?box-shadow: 0 0 0 3px var\(--ch-color-accent-ring\);/)
  assert.match(layout, /\.layout-btn\.active[\s\S]*?border-color: var\(--ch-color-border-selected\);[\s\S]*?background-color: var\(--ch-color-surface-selected\);/)
  assert.match(pane, /\.terminal-pane\.active[\s\S]*?border-color: var\(--ch-color-border-selected\);/)
  assert.match(structured, /\.conversation-avatar \{[\s\S]*?background: var\(--ch-color-surface-selected\);[\s\S]*?color: var\(--ch-color-text-selected\);/)
  assert.match(userBubble, /background: var\(--ch-color-message-user-bg\);/)
  assert.match(userBubble, /color: var\(--ch-color-text-inverse\);/)
  assert.match(messageSource, /color: var\(--ch-color-message-user-muted\);/)
  assert.match(attachmentPlaceholder, /border: 1px dashed var\(--ch-color-border-strong\);/)
  assert.match(attachmentPlaceholder, /color: var\(--ch-color-text-muted\);/)
  assert.doesNotMatch(attachmentPlaceholder, /--ch-color-message-user-muted/)
  assert.match(userAttachmentPlaceholder, /color: var\(--ch-color-message-user-muted\);/)
  assert.doesNotMatch(agentImageMissing, /--ch-color-message-user-muted/)
  assert.match(editFocus, /outline: 2px solid var\(--ch-color-message-user-focus\);/)
  assert.match(editPlaceholder, /color: var\(--ch-color-message-user-muted\);/)
  assert.match(editError, /color: var\(--ch-color-message-user-error\);/)
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

test('filled accent controls use contrast-safe fill roles', async () => {
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
      const usesForegroundAccentAsFill = /background(?:-color)?:\s*var\(--ch-color-accent(?:-hover)?\);/.test(declarations)
      const usesLightForeground = /(?:^|;)\s*color:\s*(?:#fff(?:fff)?|white|var\(--ch-color-(?:text-inverse|on-accent)(?:,\s*#fff)?\))/.test(declarations)
      assert.ok(
        !(usesForegroundAccentAsFill && usesLightForeground),
        `${path} ${selector.trim()} must use a contrast-safe accent fill role with light text`,
      )
    }
  }

  assert.match(components[0], /\.lang-toggle-btn\.active[\s\S]*?background: var\(--ch-color-accent-strong\);/)
  assert.match(components[1], /button\.primary \{[^}]*background: var\(--ch-color-accent-strong\);/)
  assert.match(components[2], /\.user-avatar-fallback[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
  assert.match(components[3], /\.composer-send-btn[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
  assert.match(components[3], /\.approval-submit-btn[\s\S]*?background: var\(--ch-color-accent-strong\);/)
  assert.match(components[4], /\.pane-indicator[\s\S]*?background-color: var\(--ch-color-accent-strong\);/)
  assert.match(app, /\.ch-btn--primary:hover:not\(:disabled\)[\s\S]*?background: var\(--ch-color-accent-fill-hover\);/)
  assert.match(components[0], /\.primary-button:hover[\s\S]*?background: var\(--ch-color-accent-fill-hover\);/)
  assert.match(components[3], /\.composer-send-btn:hover:not\(:disabled\)[\s\S]*?background-color: var\(--ch-color-accent-fill-hover\);/)
})
