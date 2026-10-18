import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const read = path => readFileSync(new URL(`../src/features/chat/${path}`, import.meta.url), 'utf8')
const pane = read('StructuredPane.vue')
const pendingTurn = read('components/ChatPendingTurn.vue')
const userMessage = read('components/ChatUserMessage.vue')
const lightbox = read('components/ChatImageLightbox.vue')
const types = read('controllers/types.ts')
const attachmentsController = read('controllers/useChatAttachments.ts')
const composerController = read('controllers/useChatComposer.ts')
const { useChatDelivery } = await loadChatFeature('controllers/useChatDelivery')

function makeDelivery() {
  const attachments = ref([{ id: 'a', filename: 'a.png', mime_type: 'image/png', data_url: 'data:a', preview_url: 'data:a', size_bytes: 1 }])
  const controller = useChatDelivery({
    attachments,
    authoritativeTurns: computed(() => []),
    connectionState: ref('live'),
    draftQueue: ref([]),
    enqueueDraft() {},
    events: ref([]),
    goal: ref(null),
    goalComposerReason: ref(null),
    goalError: ref(null),
    goalImageRecovery: ref(false),
    goalInputBlocked: ref(false),
    goalSteerOnly: ref(false),
    async hydrateGoal() {},
    isDraftQueuePaused: ref(false),
    isGoalHydrating: ref(false),
    isGoalMutating: ref(false),
    isManagedChat: ref(false),
    isPreparingAttachments: ref(false),
    isUpdatingMode: ref(false),
    isUpdatingModel: ref(false),
    isUpdatingReasoningEffort: ref(false),
    nudge() {},
    requestLatestAnchor() {},
    streamBasePath: ref('/api/workspaces/tabs/tab-a/stream'),
    syncComposerTextareaHeight() {},
    terminalStore: { async fetchAgentStatuses() {} },
  })
  controller.draftMessage.value = 'hello'
  return { controller, attachments }
}

test('composer clears draft and attachments before send settles', async t => {
  let resolveSend
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { resolveSend = resolve }))
  const { controller, attachments } = makeDelivery()
  const sending = controller.submit()
  await Promise.resolve()
  assert.equal(controller.draftMessage.value, '')
  assert.deepEqual(attachments.value, [])
  resolveSend({ ok: true })
  assert.equal(await sending, true)
})

test('submit restores the exact draft text and attachments on rejection', async t => {
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('offline') })
  const { controller, attachments } = makeDelivery()
  const original = attachments.value[0]
  assert.equal(await controller.submit(), false)
  assert.equal(controller.draftMessage.value, 'hello')
  assert.deepEqual(attachments.value, [original])
})

test('PendingTurn carries attachment preview data, not just a count', () => {
  const typeMatch = types.match(/export type PendingTurn = \{[\s\S]*?\n\}/)
  assert.ok(typeMatch, 'PendingTurn type must exist')
  assert.match(typeMatch[0], /attachments/)
  assert.doesNotMatch(typeMatch[0], /attachmentCount:\s*number/)
})

test('optimistic pending turn renders an image for each attachment', () => {
  assert.match(pendingTurn, /v-for="\(att, i\) in turn\.attachments"/)
  assert.match(pendingTurn, /<img/)
  assert.doesNotMatch(pendingTurn, /images attached/)
})

test('authoritative user turn renders compact attachment lightbox buttons', () => {
  assert.match(userMessage, /class="turn-attachments"/)
  assert.match(userMessage, /class="turn-attachment-button"/)
  assert.match(userMessage, /@click="openImageLightbox/)
  assert.match(userMessage, /<img/)
})

test('pending attachments also open the image lightbox', () => {
  assert.match(pendingTurn, /class="turn-attachment-button"/)
  assert.match(pendingTurn, /@click="openImageLightbox/)
})

test('attachment thumbnails have a bounded footprint', () => {
  for (const component of [pendingTurn, userMessage]) {
    const buttonRule = component.match(/\.turn-attachment-button\s*\{[\s\S]*?\}/)
    assert.ok(buttonRule)
    assert.match(buttonRule[0], /width:\s*clamp\(72px,\s*8vw,\s*88px\)/)
    assert.match(buttonRule[0], /aspect-ratio:/)
    assert.match(buttonRule[0], /overflow:\s*hidden/)
    const imageRule = component.match(/\.turn-attachment-img\s*\{[\s\S]*?\}/)
    assert.match(imageRule[0], /width:\s*100%/)
    assert.match(imageRule[0], /height:\s*100%/)
    assert.match(imageRule[0], /object-fit:\s*cover/)
  }
})

test('image lightbox supports backdrop, close button, Escape, and viewport bounds', () => {
  assert.match(lightbox, /<Teleport to="body">[\s\S]*?class="structured-image-lightbox"/)
  assert.match(lightbox, /role="dialog"/)
  assert.match(lightbox, /aria-modal="true"/)
  assert.match(lightbox, /@click\.self="closeImageLightbox"/)
  assert.match(lightbox, /aria-label="Close image preview"/)
  assert.match(composerController, /event\.key === 'Escape'[\s\S]*?deps\.imageLightboxUrl\.value/)
  assert.match(lightbox, /max-width:\s*min\([^;]*calc\(100vw\s*-\s*48px\)\)/)
  assert.match(lightbox, /max-height:\s*calc\(100dvh\s*-\s*96px\)/)
  assert.match(lightbox, /\.structured-image-lightbox-close\s*\{[\s\S]*?width:\s*44px[\s\S]*?height:\s*44px/)
})

test('unmount invalidates in-flight attachment preparation', () => {
  const unmount = pane.match(/onUnmounted\(\(\) => \{[\s\S]*?\n\}\)/)
  assert.ok(unmount)
  assert.match(unmount[0], /attachmentsController\.preparationEpoch\.value\+\+/)
})

test('only the owning preparation epoch may unlock or report errors', () => {
  assert.match(attachmentsController, /const epoch = preparationEpoch\.value/)
  assert.match(attachmentsController, /if \(preparationEpoch\.value !== epoch \|\| deps\.isSending\.value\) return/)
  assert.match(attachmentsController, /finally \{[\s\S]*?if \(preparationEpoch\.value === epoch\)[\s\S]*?isPreparingAttachments\.value = false/)
  assert.match(attachmentsController, /catch \(e\) \{[\s\S]*?if \(preparationEpoch\.value !== epoch\) return[\s\S]*?deps\.composerError\.value =/)
})
