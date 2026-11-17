import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const read = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

async function loadMobileInputFocus() {
  const source = read('../src/utils/mobileInputFocus.ts')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
}

test('shouldAutoFocusTextInput suppresses automatic text-input focus on mobile widths and coarse pointers', async () => {
  const { shouldAutoFocusTextInput } = await loadMobileInputFocus()

  // SSR / missing window defaults to true.
  assert.equal(shouldAutoFocusTextInput(undefined), true)

  // Desktop wide fine-pointer viewport allows automatic focus.
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 1280,
      matchMedia: () => ({ matches: false }),
    }),
    true,
  )

  // Phone viewport (390x844) suppresses automatic focus even without coarse pointer.
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 390,
      matchMedia: () => ({ matches: false }),
    }),
    false,
  )

  // Boundary at 768px suppresses automatic focus; 769px with fine pointer allows it.
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 768,
      matchMedia: () => ({ matches: false }),
    }),
    false,
  )
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 769,
      matchMedia: () => ({ matches: false }),
    }),
    true,
  )

  // Coarse pointer (landscape phone 844x390 or touch tablet 1024x768) suppresses automatic focus.
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 844,
      matchMedia: query => ({ matches: query === '(any-pointer: coarse)' }),
    }),
    false,
  )
  assert.equal(
    shouldAutoFocusTextInput({
      innerWidth: 1024,
      matchMedia: query => ({ matches: query === '(any-pointer: coarse)' }),
    }),
    false,
  )
})

test('modal and selector surfaces gate text-input auto-focus with shouldAutoFocusTextInput', () => {
  const sessionCreator = read('../src/features/terminal/components/SessionCreator.vue')
  const taskCreate = read('../src/components/TaskCreateDialog.vue')
  const workspaceEditor = read('../src/features/workspace/components/WorkspaceEditorDialog.vue')
  const taskEdit = read('../src/features/workspace/components/TaskEditDialog.vue')
  const goalSetup = read('../src/components/GoalSetupDialog.vue')
  const chatSettings = read('../src/features/chat/controllers/useChatSettings.ts')

  // None of the form dialogs keep an unconditional HTML autofocus attribute on a text input.
  for (const [name, source] of [
    ['SessionCreator.vue', sessionCreator],
    ['TaskCreateDialog.vue', taskCreate],
    ['WorkspaceEditorDialog.vue', workspaceEditor],
    ['TaskEditDialog.vue', taskEdit],
    ['GoalSetupDialog.vue', goalSetup],
  ]) {
    assert.doesNotMatch(source, /<input[^>]*\bautofocus\b/i, `${name} must not have unconditional input autofocus`)
    assert.doesNotMatch(source, /<textarea[^>]*\bautofocus\b/i, `${name} must not have unconditional textarea autofocus`)
    assert.match(source, /shouldAutoFocusTextInput\(\)/, `${name} must gate initial focus with shouldAutoFocusTextInput()`)
  }

  // ChatModelPicker search input focus is gated so tapping the model selector on mobile does not pop up the soft keyboard.
  assert.match(
    chatSettings,
    /if \(shouldAutoFocusTextInput\(\)\) \{\s*void nextTick\(\(\) => \{\s*modelSearchEl\.value\?\.focus\(\{ preventScroll: true \}\)\s*\}\)\s*\}/,
  )
})

test('mobile modal overlays stay above MobileControls and do not override bottom-sheet geometry', () => {
  const sessionCreatorCss = read('../src/features/terminal/styles/session-creator.css')
  const closeTabDialog = read('../src/features/terminal/components/CloseTabDialog.vue')
  const mobileControls = read('../src/components/MobileControls.vue')
  const overlaysCss = read('../src/features/app-shell/styles/overlays.css')

  // MobileControls sits below modal overlays (z-index 1000) so it never paints over SessionCreator or CloseTabDialog.
  assert.match(mobileControls, /\.mobile-controls-overlay \{[\s\S]*?z-index: 800;/)
  assert.match(sessionCreatorCss, /\.modal-overlay \{[\s\S]*?z-index: 1000;/)

  // SessionCreator and CloseTabDialog mobile rules let overlays.css own bottom-sheet alignment and visual-viewport sizing.
  const mobileCreatorBlock = sessionCreatorCss.slice(sessionCreatorCss.indexOf('@media (max-width: 640px)'))
  assert.doesNotMatch(mobileCreatorBlock, /align-items:\s*flex-start/)
  assert.doesNotMatch(mobileCreatorBlock, /\.modal\s*\{[^}]*max-height:\s*calc\(100dvh/)
  assert.match(mobileCreatorBlock, /\.file-browser-modal \{[\s\S]*?var\(--visual-viewport-height, 100dvh\)/)

  const mobileCloseBlock = closeTabDialog.slice(closeTabDialog.indexOf('@media (max-width: 640px)'))
  assert.doesNotMatch(mobileCloseBlock, /align-items:\s*flex-start/)
  assert.doesNotMatch(mobileCloseBlock, /max-height:\s*calc\(100dvh/)

  assert.match(overlaysCss, /@media \(max-width: 640px\)[\s\S]*?max-height: calc\(var\(--visual-viewport-height, 100dvh\)/)
})
