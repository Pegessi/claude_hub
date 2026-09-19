import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(
  new URL('../src/components/ChatSidebar.vue', import.meta.url),
  'utf8',
)

test('the whole session row navigates while its actions remain independent', () => {
  assert.match(source, /class="chat-sidebar__item"[\s\S]*@click="onRowClick\(\$event, tab\)"/)
  assert.match(source, /@click\.stop="setActiveTab\(tab.id\)"/)
  assert.match(source, /closest\('.chat-sidebar__item-menu, .chat-sidebar__rename-input'\)/)
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
