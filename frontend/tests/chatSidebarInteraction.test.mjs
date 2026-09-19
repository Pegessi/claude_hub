import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(
  new URL('../src/components/ChatSidebar.vue', import.meta.url),
  'utf8',
)

test('the whole session row navigates while its actions remain independent', () => {
  assert.match(source, /class="chat-sidebar__item"[\s\S]*@click="onRowClick\(\$event, tab\)"/)
  assert.match(source, /@click\.stop="onRowClick\(\$event, tab\)"/)
  assert.match(source, /closest\('.chat-sidebar__item-menu, .chat-sidebar__rename-input'\)/)
})

test('session reorder uses pointer events instead of browser-native drag and dataTransfer', () => {
  assert.match(source, /@pointerdown="onRowPointerDown\(\$event, tab\)"/)
  assert.match(source, /window\.addEventListener\('pointermove', onPointerMove/)
  assert.match(source, /document\.elementFromPoint\(clientX, clientY\)/)
  assert.match(source, /store\.reorderTabById\(source.id, targetId, dropTarget\.value!\.position\)/)
  assert.match(source, /\.chat-sidebar__pin-drop \{[\s\S]*position: absolute;/)
  assert.doesNotMatch(source, /@dragstart=/)
  assert.doesNotMatch(source, /dataTransfer/)
})

test('sidebar resize is bounded, accessible, and browser-persisted', () => {
  assert.match(source, /class="chat-sidebar__resize-handle"/)
  assert.match(source, /role="separator"/)
  assert.match(source, /SIDEBAR_MIN_WIDTH = 200/)
  assert.match(source, /SIDEBAR_MAX_WIDTH = 480/)
  assert.match(source, /claude_hub_chat_sidebar_width/)
  assert.match(source, /@pointerdown="startResize"/)
  assert.match(source, /@keydown="resizeWithKeyboard"/)
})
