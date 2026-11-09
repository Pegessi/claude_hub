import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { ref } from 'vue'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const { useChatReview } = await loadChatFeature('review/useChatReview')
const { formatReviewMessage } = await loadChatFeature('review/reviewComments')

const read = async path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')

const anchor = overrides => ({
  key: 'group-1:tool-1:3',
  turn: 'turn-1',
  group: 'group-1',
  file: 'src/app.ts',
  line: 42,
  endLine: null,
  code: 'return value',
  ...overrides,
})

function memoryStorage() {
  const entries = new Map()
  return {
    entries,
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value) },
  }
}

function review(storage = null) {
  const draftQueue = ref([])
  const controller = useChatReview({ tabKey: 'tab-1', draftQueue, storage })
  return { draftQueue, controller }
}

test('a comment keeps its row, text and one-per-anchor identity', () => {
  const { controller } = review()
  assert.equal(controller.count.value, 0)
  assert.equal(controller.addComment(anchor(), '   '), null, 'an empty note is not a comment')

  const added = controller.addComment(anchor(), 'This drops the null check.')
  assert.equal(added.file, 'src/app.ts')
  assert.equal(added.line, 42)
  assert.equal(controller.count.value, 1)

  // A second note on the same row edits it instead of stacking a duplicate.
  controller.addComment(anchor(), 'Second  thought\nhere.')
  assert.equal(controller.count.value, 1)
  assert.equal(controller.comments.value[0].text, 'Second thought here.')

  const other = controller.addComment(anchor({ key: 'group-1:tool-1:9', line: 48 }), 'Use the shared helper.')
  assert.equal(controller.count.value, 2)
  assert.equal(controller.comments.value[1].id, other.id)
})

test('comments can be edited, removed and cleared', () => {
  const { controller } = review()
  controller.addComment(anchor(), 'first')
  const second = controller.addComment(anchor({ key: 'group-1:tool-1:9', line: 48 }), 'second')
  controller.updateComment(second.id, 'second, revised')
  assert.equal(controller.comments.value[1].text, 'second, revised')
  controller.updateComment(second.id, '   ')
  assert.equal(controller.comments.value[1].text, 'second, revised', 'an emptied edit is ignored')

  controller.removeComment(second.id)
  assert.deepEqual(controller.comments.value.map(item => item.text), ['first'])
  controller.removeComment('missing')
  assert.equal(controller.count.value, 1)

  controller.clearComments()
  assert.equal(controller.count.value, 0)
  assert.equal(controller.isEmpty.value, true)
})

test('sending composes one message, queues it and empties the tray', () => {
  const { controller, draftQueue } = review()
  controller.addComment(anchor(), 'This drops the null check.')
  controller.addComment(anchor({ key: 'k2', file: 'src/b.py', line: 9, code: 'def run(payload)' }), 'Split this helper.')

  const message = controller.sendReview()
  assert.equal(message, [
    'Review comments (2):',
    '1. src/app.ts:42 — `return value` — This drops the null check.',
    '2. src/b.py:9 — `def run(payload)` — Split this helper.',
  ].join('\n'))

  // It becomes an ordinary queued message: the existing queue owns delivery,
  // busy-turn ordering and retry identity.
  assert.equal(draftQueue.value.length, 1)
  assert.equal(draftQueue.value[0].message, message)
  assert.deepEqual(draftQueue.value[0].attachments, [])
  assert.equal(draftQueue.value[0].error, undefined)
  assert.equal(controller.count.value, 0, 'the tray is cleared once the message is queued')

  assert.equal(controller.sendReview(), '', 'an empty tray sends nothing')
  assert.equal(draftQueue.value.length, 1)
})

test('the tray can ask a row to reveal itself, repeatedly', () => {
  const { controller } = review()
  assert.equal(controller.focusTarget.value, null)
  controller.requestFocus('group-1:tool-1:3')
  assert.deepEqual(controller.focusTarget.value, { key: 'group-1:tool-1:3', nonce: 1 })
  controller.requestFocus('group-1:tool-1:3')
  assert.equal(controller.focusTarget.value.nonce, 2, 'a repeat click re-fires the reveal')
})

test('comments survive a remount inside the same tab and drop malformed state', () => {
  const storage = memoryStorage()
  const first = review(storage)
  first.controller.addComment(anchor(), 'Keep this one.')
  assert.ok(storage.entries.get('chub-review:tab-1'))

  const second = review(storage)
  assert.deepEqual(second.controller.comments.value.map(item => item.text), ['Keep this one.'])
  assert.equal(second.controller.comments.value[0].line, 42)

  const sameTabRemount = review(storage)
  assert.equal(sameTabRemount.controller.count.value, 1)
  const otherTab = useChatReview({ tabKey: 'tab-2', draftQueue: ref([]), storage })
  assert.equal(otherTab.count.value, 0, 'another tab starts empty')

  // A hand-edited or older value must not be rendered as comments.
  storage.setItem('chub-review:tab-1', '{"comments":[{"text":5},{"id":"a","text":"ok","file":"f","key":"k"}]}')
  const restored = useChatReview({ tabKey: 'tab-1', draftQueue: ref([]), storage })
  assert.deepEqual(restored.comments.value.map(item => item.text), ['ok'])
  storage.setItem('chub-review:tab-1', 'not json')
  assert.equal(useChatReview({ tabKey: 'tab-1', draftQueue: ref([]), storage }).count.value, 0)
})

test('a blocked or absent store degrades to memory only', () => {
  const hostile = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  const { controller } = review(hostile)
  controller.addComment(anchor(), 'still here')
  assert.equal(controller.count.value, 1)
  assert.equal(formatReviewMessage(controller.comments.value).startsWith('Review comments (1):'), true)
})

test('the tray is mounted above the composer and the diff rows are the comment targets', async () => {
  const [composer, toolGroup, pane] = await Promise.all([
    read('features/chat/components/ChatComposer.vue'),
    read('features/chat/components/ChatToolGroup.vue'),
    read('features/chat/StructuredPane.vue'),
  ])
  assert.match(composer, /<ChatReviewTray \/>/)
  assert.ok(
    composer.indexOf('<ChatReviewTray />') < composer.indexOf('<ChatDraftQueue'),
    'review comments sit above the queued messages they join',
  )
  assert.match(pane, /const review = useChatReview\(/)
  assert.match(pane, /provide\(chatReviewKey, review\)/)
  // Each diff row is a named target; folded cards still render the affordance.
  assert.match(toolGroup, /chatReviewKey/)
  assert.match(toolGroup, /:data-diff-row="index"/)
  assert.match(toolGroup, /class="activity-diff-add"/)
  assert.match(toolGroup, /class="activity-diff-num"/)
  assert.match(toolGroup, /v-for="comment in commentsFor\(tool, index\)"/)
})
