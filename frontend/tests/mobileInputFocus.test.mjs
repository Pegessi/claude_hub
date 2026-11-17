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

test('modal and selector surfaces gate text-input auto-focus and move mobile focus to the modal container', () => {
  const sessionCreator = read('../src/features/terminal/components/SessionCreator.vue')
  const taskCreate = read('../src/components/TaskCreateDialog.vue')
  const workspaceEditor = read('../src/features/workspace/components/WorkspaceEditorDialog.vue')
  const taskEdit = read('../src/features/workspace/components/TaskEditDialog.vue')
  const goalSetup = read('../src/components/GoalSetupDialog.vue')
  const chatSettings = read('../src/features/chat/controllers/useChatSettings.ts')

  // None of the form dialogs keep an unconditional HTML autofocus attribute on a text input,
  // and each focuses its non-editable modal container when shouldAutoFocusTextInput() is false.
  for (const [name, source, expectedFallback] of [
    ['SessionCreator.vue', sessionCreator, /shouldAutoFocusTextInput\(\)\s*\?\s*tabNameInputRef\.value\s*:\s*createModalRef\.value/],
    ['TaskCreateDialog.vue', taskCreate, /shouldAutoFocusTextInput\(\)\s*\?\s*titleInputRef\.value\s*:\s*modalRef\.value/],
    ['WorkspaceEditorDialog.vue', workspaceEditor, /shouldAutoFocusTextInput\(\)\s*\?\s*nameInputRef\.value\s*:\s*modalRef\.value/],
    ['TaskEditDialog.vue', taskEdit, /shouldAutoFocusTextInput\(\)\s*\?\s*titleInputRef\.value\s*:\s*modalRef\.value/],
    ['GoalSetupDialog.vue', goalSetup, /shouldAutoFocusTextInput\(\)\s*\?\s*objectiveEl\.value\s*:\s*dialogEl\.value/],
  ]) {
    assert.doesNotMatch(source, /<input[^>]*\bautofocus\b/i, `${name} must not have unconditional input autofocus`)
    assert.doesNotMatch(source, /<textarea[^>]*\bautofocus\b/i, `${name} must not have unconditional textarea autofocus`)
    assert.match(source, expectedFallback, `${name} must focus its modal container when shouldAutoFocusTextInput() is false`)
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

test('shared visualViewport modal layout anchors mobile overlays to visualViewport top/height without keyboard padding double-subtraction', () => {
  const overlaysCss = read('../src/features/app-shell/styles/overlays.css')
  const shellCss = read('../src/features/app-shell/styles/shell.css')
  const appVue = read('../src/App.vue')
  const taskCreate = read('../src/components/TaskCreateDialog.vue')
  const sharedCompact = read('../src/features/workspace/styles/shared-compact-layout.css')
  const residentAgent = read('../src/features/workspace/components/ResidentAgentDialog.vue')
  const agentDirectory = read('../src/features/workspace/components/AgentDirectoryDialog.vue')
  const taskDetail = read('../src/features/workspace/components/TaskDetailDialog.vue')
  const envPresetManager = read('../src/components/EnvPresetManager.vue')
  const feishuBotCss = read('../src/features/feishu-bot/feishu-bot-settings-dialog.css')

  // App shell anchors .app to visualViewport offsetTop across all modes on mobile/coarse viewports.
  assert.match(
    shellCss,
    /@media \(max-width: 768px\), \(any-pointer: coarse\)[\s\S]*?\.app\s*\{\s*position:\s*fixed;\s*top:\s*var\(--visual-viewport-offset-top, 0px\);/,
  )

  // overlays.css anchors modal/sheet backdrops to visualViewport offsetTop + height with overflow: hidden
  // and does not use padding: 0 0 var(--keyboard-height, 0px) on backdrop overlays.
  assert.match(
    overlaysCss,
    /@media \(max-width: 768px\), \(any-pointer: coarse\)[\s\S]*?top:\s*var\(--visual-viewport-offset-top, 0px\);[\s\S]*?height:\s*var\(--visual-viewport-height, 100dvh\);[\s\S]*?max-height:\s*var\(--visual-viewport-height, 100dvh\);[\s\S]*?overflow:\s*hidden;/,
  )
  assert.doesNotMatch(overlaysCss, /padding:\s*0\s+0\s+var\(--keyboard-height/)

  // Modal dialogs use --visual-viewport-height instead of raw 100dvh / 70dvh so they shrink above the keyboard.
  assert.match(taskCreate, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*20px\)/)
  assert.match(sharedCompact, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*20px\)/)
  assert.match(residentAgent, /height:\s*min\(720px,\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*32px\)\)/)
  assert.match(agentDirectory, /height:\s*min\(620px,\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*32px\)\)/)
  assert.match(taskDetail, /height:\s*var\(--visual-viewport-height, 100dvh\)/)
  assert.match(envPresetManager, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*20px\)/)
  assert.match(feishuBotCss, /height:\s*calc\(var\(--visual-viewport-height, 100dvh\)\s*-\s*16px\)/)

  // App.vue syncs modal viewport scroll on visualViewport updates and focusin inside modals.
  assert.match(appVue, /syncModalViewportScroll\(document,\s*window\)/)
  assert.match(appVue, /document\.addEventListener\('focusin',\s*handleModalFocusIn\)/)
})

test('computeModalFieldScrollDelta and syncModalViewportScroll keep focused modal inputs visible above sticky footers', async () => {
  const { computeModalFieldScrollDelta, syncModalViewportScroll } = await loadMobileInputFocus()

  // Input already visible between header and sticky footer produces zero scroll delta.
  assert.equal(
    computeModalFieldScrollDelta(
      { top: 140, bottom: 180, height: 40 },
      { top: 60, bottom: 440 },
      [{ top: 380, bottom: 440, height: 60 }],
    ),
    0,
  )

  // Input obscured by sticky footer at the bottom of a shrunk modal scrolls down so it clears the footer + padding.
  const downDelta = computeModalFieldScrollDelta(
    { top: 360, bottom: 410, height: 50 },
    { top: 60, bottom: 440 },
    [{ top: 380, bottom: 440, height: 60 }],
  )
  assert.ok(downDelta > 0, `expected positive scroll delta, got ${downDelta}`)
  assert.equal(downDelta, 410 - (380 - 12))

  // Input clipped above the top of the modal container scrolls up.
  const upDelta = computeModalFieldScrollDelta(
    { top: 40, bottom: 80, height: 40 },
    { top: 60, bottom: 440 },
    [],
  )
  assert.ok(upDelta < 0, `expected negative scroll delta, got ${upDelta}`)
  assert.equal(upDelta, 40 - (60 + 12))

  // syncModalViewportScroll resets overlay/window scroll and scrolls the modal container for a focused input.
  const FakeHTMLElement = class {}
  const origHTMLElement = globalThis.HTMLElement
  globalThis.HTMLElement = FakeHTMLElement
  try {
    const overlay = Object.assign(new FakeHTMLElement(), { scrollTop: 95, scrollLeft: 12 })
    const stickyFooter = Object.assign(new FakeHTMLElement(), {
      contains: () => false,
      getBoundingClientRect: () => ({ top: 380, bottom: 440, height: 60 }),
    })
    const modal = Object.assign(new FakeHTMLElement(), {
      scrollTop: 20,
      scrollHeight: 720,
      clientHeight: 380,
      parentElement: overlay,
      querySelectorAll: () => [stickyFooter],
      getBoundingClientRect: () => ({ top: 60, bottom: 440, height: 380 }),
    })
    const input = Object.assign(new FakeHTMLElement(), {
      parentElement: modal,
      matches: () => true,
      closest: () => modal,
      getBoundingClientRect: () => ({ top: 370, bottom: 410, height: 40 }),
    })

    let scrolledTo = null
    const fakeWin = {
      innerWidth: 390,
      scrollY: 120,
      scrollX: 0,
      scrollTo: (x, y) => {
        scrolledTo = [x, y]
      },
      matchMedia: () => ({ matches: true }),
      getComputedStyle: el => {
        if (el === modal) return { overflowY: 'auto', position: 'relative' }
        if (el === stickyFooter) return { overflowY: 'visible', position: 'sticky' }
        return { overflowY: 'visible', position: 'static' }
      },
    }
    const fakeDoc = {
      activeElement: input,
      documentElement: { scrollTop: 45, scrollLeft: 0 },
      body: { scrollTop: 30, scrollLeft: 0 },
      querySelectorAll: selector => {
        if (selector.includes('.modal-overlay')) return [overlay]
        return [modal]
      },
    }

    const didScroll = syncModalViewportScroll(fakeDoc, fakeWin)
    assert.equal(didScroll, true)
    assert.equal(overlay.scrollTop, 0)
    assert.equal(overlay.scrollLeft, 0)
    assert.deepEqual(scrolledTo, [0, 0])
    assert.equal(fakeDoc.documentElement.scrollTop, 0)
    assert.equal(fakeDoc.body.scrollTop, 0)
    assert.equal(modal.scrollTop, 20 + (410 - (380 - 12)))
  } finally {
    globalThis.HTMLElement = origHTMLElement
  }
})

