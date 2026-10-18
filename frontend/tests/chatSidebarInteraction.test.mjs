import assert from 'node:assert/strict'
import test from 'node:test'
import { compileStyle, parse, source } from './chatSidebarTestHarness.mjs'

const sidebar = source('features/chat/sidebar/ChatSidebar.vue')
const row = source('features/chat/sidebar/ChatSidebarRow.vue')
const group = source('features/chat/sidebar/ChatSidebarGroup.vue')
const drag = source('features/chat/sidebar/useSidebarPointerDrag.ts')
const resize = source('features/chat/sidebar/useSidebarResize.ts')

test('the whole session row navigates while its actions remain independent', () => {
  assert.match(row, /class="chat-sidebar__item"[\s\S]*@click="emit\('row-click', \$event, tab\)"/)
  assert.match(row, /@click\.stop="emit\('row-click', \$event, tab\)"/)
  assert.match(drag, /closest\('.chat-sidebar__item-menu, .chat-sidebar__rename-input'\)/)
  assert.match(sidebar, /@row-click="onRowClick"/)
})

test('session reorder uses pointer events and the owned last-visible drop edge', () => {
  assert.match(row, /@pointerdown="emit\('row-pointerdown', \$event, tab\)"/)
  assert.match(sidebar, /@row-pointerdown="onRowPointerDown"/)
  assert.match(drag, /window\.addEventListener\('pointermove', onPointerMove/)
  assert.match(drag, /document\.elementFromPoint\(clientX, clientY\)/)
  assert.match(drag, /reorderTabById\(source.id, targetId, dropTarget\.value!\.position\)/)
  assert.match(sidebar, /\.chat-sidebar__pin-drop \{[\s\S]*position: absolute;/)
  for (const text of [sidebar, row, group, drag]) {
    assert.doesNotMatch(text, /@dragstart=|dataTransfer|@keydown\.alt\.(up|down)|@move=/)
  }
})

test('workspace headers retain pointer ordering and visible drop-edge markup/styles', () => {
  assert.match(group, /@pointerdown="emit\('group-pointerdown', \$event, group\)"/)
  assert.match(sidebar, /@group-pointerdown="onGroupPointerDown"/)
  assert.match(drag, /reorderChatWorkspace\(/)
  assert.match(drag, /groupDropTarget.value = \{/)
  assert.ok(group.includes('.chat-sidebar__group[data-group-drop-position] > .chat-sidebar__group-header::before'))
})

test('Pinned still folds through the single shell-owned selection state', () => {
  const header = group.match(/<button\s+v-if="group\.pinned"[\s\S]*?<\/button>/)?.[0] ?? ''
  assert.match(header, /class="chat-sidebar__group-header chat-sidebar__pinned-header"/)
  assert.match(header, /:aria-expanded="!group\.collapsed"/)
  assert.match(header, /@click="emit\('toggle-pinned'\)"/)
  assert.match(header, /class="chat-sidebar__group-chevron"/)
  assert.match(sidebar, /@toggle-pinned="pinnedCollapsed = !pinnedCollapsed"/)
  assert.match(sidebar, /pinnedCollapsed: pinnedCollapsed\.value,/)
})

test('only working rows mount their stable per-session beam and star field', () => {
  assert.match(row, /<ChatSidebarWorkingEffect\s+v-if="status === 'working'"\s+:seed="tab\.id"\s*\/>/)
  assert.match(sidebar, /:status="getTabStatus\(tab\)"/)
  assert.doesNotMatch(row, /working-comet/)
})

test('sidebar resize keeps its accessible bounds and persisted controller', () => {
  assert.match(sidebar, /class="chat-sidebar__resize-handle"/)
  assert.match(sidebar, /role="separator"/)
  assert.match(resize, /SIDEBAR_MIN_WIDTH = 200/)
  assert.match(resize, /SIDEBAR_MAX_WIDTH = 480/)
  assert.match(resize, /claude_hub_chat_sidebar_width/)
  assert.match(sidebar, /@pointerdown="startResize"/)
  assert.match(sidebar, /@keydown="resizeWithKeyboard"/)
  assert.match(resize, /onUnmounted\(stopResize\)/)
})

test('scoped shell layout rules still reach nested mobile rows and pinned groups', () => {
  const { descriptor } = parse(sidebar)
  const { code, errors } = compileStyle({ source: descriptor.styles[0].content, id: 'data-v-sidebar', scoped: true })
  assert.deepEqual(errors, [])
  assert.match(code, /\.chat-sidebar\.mobile-drawer\[data-v-sidebar\] \.chat-sidebar__item\s*\{\s*min-height: 44px/)
  assert.match(code, /\.chat-sidebar\.mobile-drawer\[data-v-sidebar\] \.chat-sidebar__group--pinned\s*\{\s*position: relative/)
  assert.match(code, /\.chat-sidebar\.mobile-drawer\[data-v-sidebar\]:not\(\.mobile-drawer--open\) \.working-effect \*/)
  assert.match(group, /\.chat-sidebar__group--pinned \{\s*position: sticky/)
  assert.match(row, /\.chat-sidebar__item:hover > :deep\(\.chat-sidebar__item-menu\)/)
})
