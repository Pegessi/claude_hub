import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

// ---------------------------------------------------------------------------
// Composer clearing: the composer (draftMessage + attachments) must be cleared
// synchronously BEFORE the send promise settles, so the thumbnail disappears
// immediately on Send. On rejection the same text/images must be restored so
// the user can retry.
// ---------------------------------------------------------------------------

test('composer clears draft and attachments before awaiting sendToStream', () => {
  // The submit body must clear draftMessage and attachments BEFORE the
  // `await sendToStream(...)` call. If the clearing comes after the await,
  // the composer thumbnail lingers until the network round-trip completes.
  const submitMatch = structuredPane.match(/async function submit\([\s\S]*?\) \{[\s\S]*?\n\}/)
  assert.ok(submitMatch, 'submit function must exist')
  const body = submitMatch[0]

  const awaitIdx = body.indexOf('await sendToStream')
  assert.ok(awaitIdx > 0, 'submit must await sendToStream')

  const clearDraftIdx = body.indexOf("draftMessage.value = ''")
  const clearAttsIdx = body.indexOf('attachments.value = []')

  assert.ok(clearDraftIdx > 0, 'submit must clear draftMessage')
  assert.ok(clearAttsIdx > 0, 'submit must clear attachments')
  assert.ok(
    clearDraftIdx < awaitIdx,
    'draftMessage must be cleared BEFORE awaiting sendToStream',
  )
  assert.ok(
    clearAttsIdx < awaitIdx,
    'attachments must be cleared BEFORE awaiting sendToStream',
  )
})

test('submit restores the original draft text and attachments on send rejection', () => {
  // On error, the catch block must restore the exact message and attachments
  // that were cleared before the await, so the user can retry without losing
  // their input. We look for the saved copies being reassigned.
  const submitMatch = structuredPane.match(/async function submit\([\s\S]*?\) \{[\s\S]*?\n\}/)
  assert.ok(submitMatch)
  const body = submitMatch[0]

  // The original text and attachments must be captured before clearing.
  assert.match(body, /const message = .*draftMessage\.value/)
  assert.match(body, /const atts[:\s]/)

  // On error, the pending turn is removed and the composer is restored.
  const catchIdx = body.indexOf('catch')
  assert.ok(catchIdx > 0, 'submit must have a catch block')
  const catchBody = body.slice(catchIdx)

  assert.match(catchBody, /draftMessage\.value = message/, 'catch must restore draft text')
  assert.match(catchBody, /attachments\.value = /, 'catch must restore attachments')
})

// ---------------------------------------------------------------------------
// Optimistic user bubble: the pending turn must render the actual image(s)
// (via a data URL / preview URL), not just a count string.
// ---------------------------------------------------------------------------

test('PendingTurn carries attachment preview data, not just a count', () => {
  // The PendingTurn type must include an attachments array with renderable
  // image data (preview_url / mime_type), so the optimistic bubble can show
  // the thumbnail immediately.
  const typeMatch = structuredPane.match(/type PendingTurn = \{[\s\S]*?\n\}/)
  assert.ok(typeMatch, 'PendingTurn type must exist')
  const typeBody = typeMatch[0]

  assert.match(typeBody, /attachments/, 'PendingTurn must have an attachments field')
  assert.doesNotMatch(
    typeBody,
    /attachmentCount:\s*number/,
    'PendingTurn must not rely on attachmentCount alone',
  )
})

test('optimistic pending turn renders an <img> for each attachment', () => {
  // The pending-turn template must render <img> elements from the attachment
  // preview URLs, not a "N images attached" text label.
  const pendingBlock = structuredPane.match(
    /v-for="turn in pendingTurns"[\s\S]*?structured-turn--pending[\s\S]*?<\/div>\s*<\/div>/,
  )
  assert.ok(pendingBlock, 'pending turns block must exist')
  const block = pendingBlock[0]

  assert.match(block, /<img/, 'pending turn must render <img> for attachments')
  assert.doesNotMatch(
    block,
    /images attached/,
    'pending turn must not render a count-only label',
  )
})

// ---------------------------------------------------------------------------
// Authoritative user bubble: the rendered user turn must also show the image
// (resolved from the durable attachment id), not just the text.
// ---------------------------------------------------------------------------

test('authoritative user turn renders attachment images', () => {
  const userBlock = structuredPane.match(
    /conversation-row--user[\s\S]*?conversation-bubble--user[\s\S]*?<\/div>/,
  )
  assert.ok(userBlock, 'user turn block must exist')
  const block = userBlock[0]

  assert.match(block, /<img/, 'authoritative user turn must render <img> for attachments')
})

test('conversation attachments are compact buttons that open the image lightbox', () => {
  const authoritativeBlock = structuredPane.match(
    /class="turn-attachments"[\s\S]*?Placeholder for no-preview/,
  )
  assert.ok(authoritativeBlock, 'authoritative attachment block must exist')
  assert.match(authoritativeBlock[0], /class="turn-attachment-button"/)
  assert.match(authoritativeBlock[0], /@click="openImageLightbox/)

  const pendingBlock = structuredPane.match(
    /v-for="turn in pendingTurns"[\s\S]*?structured-turn--pending[\s\S]*?<\/div>\s*<\/div>/,
  )
  assert.ok(pendingBlock, 'pending turns block must exist')
  assert.match(pendingBlock[0], /class="turn-attachment-button"/)
  assert.match(pendingBlock[0], /@click="openImageLightbox/)
})

test('attachment thumbnails have a bounded footprint instead of using intrinsic size', () => {
  const buttonRule = structuredPane.match(/\.turn-attachment-button\s*\{[\s\S]*?\}/)
  assert.ok(buttonRule, 'thumbnail button CSS must exist')
  assert.match(buttonRule[0], /width:\s*clamp\(72px,\s*8vw,\s*88px\)/)
  assert.match(buttonRule[0], /aspect-ratio:/)
  assert.match(buttonRule[0], /overflow:\s*hidden/)

  const imageRule = structuredPane.match(/\.turn-attachment-img\s*\{[\s\S]*?\}/)
  assert.ok(imageRule, 'thumbnail image CSS must exist')
  assert.match(imageRule[0], /width:\s*100%/)
  assert.match(imageRule[0], /height:\s*100%/)
  assert.match(imageRule[0], /object-fit:\s*cover/)

  const placeholderRule = structuredPane.match(/\.turn-attachment-placeholder\s*\{[\s\S]*?\}/)
  assert.ok(placeholderRule, 'expired thumbnail placeholder CSS must exist')
  assert.match(placeholderRule[0], /width:\s*clamp\(72px,\s*8vw,\s*88px\)/)
})

test('image lightbox supports backdrop, close button, and Escape', () => {
  assert.match(structuredPane, /<Teleport to="body">[\s\S]*?class="structured-image-lightbox"/)
  assert.match(structuredPane, /role="dialog"/)
  assert.match(structuredPane, /aria-modal="true"/)
  assert.match(structuredPane, /@click\.self="closeImageLightbox"/)
  assert.match(structuredPane, /aria-label="Close image preview"/)
  assert.match(structuredPane, /event\.key === 'Escape'/)
})

test('mobile image controls retain a touch target and keep the lightbox inside the viewport', () => {
  const buttonRule = structuredPane.match(/\.turn-attachment-button\s*\{[\s\S]*?\}/)
  assert.ok(buttonRule, 'thumbnail button CSS must exist')
  const minWidth = Number(buttonRule[0].match(/clamp\((\d+)px/)?.[1])
  assert.ok(minWidth >= 44, 'mobile thumbnail must remain at least a 44px touch target')

  const imageRule = structuredPane.match(/\.structured-image-lightbox-img\s*\{[\s\S]*?\}/)
  assert.ok(imageRule, 'lightbox image CSS must exist')
  assert.match(imageRule[0], /max-width:\s*min\([^;]*calc\(100vw\s*-\s*48px\)\)/)
  assert.match(imageRule[0], /max-height:\s*calc\(100dvh\s*-\s*48px\)/)

  const closeRule = structuredPane.match(/\.structured-image-lightbox-close\s*\{[\s\S]*?\}/)
  assert.ok(closeRule, 'lightbox close button CSS must exist')
  assert.match(closeRule[0], /width:\s*44px/)
  assert.match(closeRule[0], /height:\s*44px/)
})

// ---------------------------------------------------------------------------
// Async preparation ownership: a FileReader/canvas continuation from a prior
// Chat must never unlock or mutate the composer for the newly selected Chat
// (including the A→B→A ABA case).
// ---------------------------------------------------------------------------

test('source changes invalidate every in-flight attachment preparation batch', () => {
  const sourceWatch = structuredPane.match(
    /watch\(\s*\(\) => props\.tabId[\s\S]*?startStream\(\)\s*\},\s*\)/,
  )
  assert.ok(sourceWatch, 'Chat-tab watcher must reset the structured composer')
  assert.match(
    sourceWatch[0],
    /preparationEpoch\.value\+\+/,
    'source switch must invalidate old FileReader/canvas continuations',
  )
})

test('only the owning preparation epoch may unlock or report errors', () => {
  const addFiles = structuredPane.match(/async function addFiles\([\s\S]*?\n\}/)
  assert.ok(addFiles, 'addFiles function must exist')
  const body = addFiles[0]

  assert.match(body, /const epoch = preparationEpoch\.value/)
  assert.match(
    body,
    /if \(preparationEpoch\.value !== epoch \|\| isSending\.value\) return/,
    'every async batch must reject stale ownership before appending',
  )
  assert.match(
    body,
    /finally \{[\s\S]*?if \(preparationEpoch\.value === epoch\) \{[\s\S]*?isPreparingAttachments\.value = false/,
    'a stale batch must not unlock a newer batch',
  )
  assert.match(
    body,
    /catch \(e\) \{[\s\S]*?if \(preparationEpoch\.value !== epoch\) return[\s\S]*?composerError\.value =/,
    'a stale batch must not overwrite the new source error state',
  )
})
