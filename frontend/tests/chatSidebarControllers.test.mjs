import assert from 'node:assert/strict'
import test from 'node:test'
import { ref, nextTick } from 'vue'
import { createSidebarLoader, installBrowser, mountController, pointer, TestElement } from './chatSidebarTestHarness.mjs'

const load = createSidebarLoader()
const { useSidebarPointerDrag } = await load('features/chat/sidebar/useSidebarPointerDrag.ts')
const { useSidebarResize, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } = await load('features/chat/sidebar/useSidebarResize.ts')
const { useSidebarRename } = await load('features/chat/sidebar/useSidebarRename.ts')
const tab = (id, cwd = '/repo', extra = {}) => ({ id, name: id, cwd, session_kind: 'chat', ...extra })
const group = (key, cwd, tabs, extra = {}) => ({ key, cwd, tabs, pinned: false, ...extra })
const listenerCount = browser => [...browser.listeners.values()].reduce((sum, listeners) => sum + listeners.size, 0)
function dragHarness(t) {
  const browser = installBrowser(t)
  const tabs = [tab('a'), tab('b'), tab('foreign', '/other')]
  const groups = [group('cwd:/repo', '/repo', tabs.slice(0, 2)), group('cwd:/other', '/other', tabs.slice(2))]
  const calls = []
  const dependencies = {
    chatTabs: ref(tabs), filteredGroups: ref(groups), filterText: ref(''), renamingTabId: ref(null), isMobile: ref(false),
    selectMobileTab: id => calls.push(['mobile', id]), setActiveTab: id => calls.push(['select', id]),
    setChatPinned: (...args) => calls.push(['pin', ...args]), reorderTabById: (...args) => calls.push(['row', ...args]),
    reorderChatWorkspace: (...args) => calls.push(['group', ...args]), toggleGroup: cwd => calls.push(['toggle', cwd]),
  }
  const mounted = mountController(t, () => useSidebarPointerDrag(dependencies))
  function target(groupIndex = 0, rowId = 'b') {
    const element = new TestElement()
    const groupElement = new TestElement()
    groupElement.group = groupElement
    groupElement.dataset.groupKey = groups[groupIndex].key
    element.group = groupElement
    element.row = rowId ? element : null
    element.dataset.tabId = rowId
    browser.hit(element)
    return element
  }
  return { browser, tabs, groups, calls, dependencies, target, ...mounted }
}

test('row drag uses a five-pixel threshold and commits the last visible drop edge', t => {
  const h = dragHarness(t)
  h.api.onRowPointerDown(pointer(), h.tabs[0])
  assert.equal(listenerCount(h.browser), 3)
  h.target()
  h.browser.fire('pointermove', pointer({ clientX: 2 }))
  assert.equal(h.api.draggedTabId.value, null)
  h.browser.fire('pointermove', pointer({ clientY: 30, pointerId: 2 }))
  assert.equal(h.api.draggedTabId.value, null, 'another pointer cannot take over')
  h.browser.fire('pointermove', pointer({ clientY: 30 }))
  assert.equal(h.api.draggedTabId.value, 'a')
  assert.deepEqual(h.api.dropTarget.value, { id: 'b', position: 'after' })
  assert.equal(h.browser.classes.has('chat-sidebar-dragging'), true)
  h.target(0, 'a')
  h.browser.fire('pointerup', pointer({ clientY: 0 }))
  assert.deepEqual(h.calls, [['pin', 'a', false], ['row', 'a', 'b', 'after']])
  assert.equal(listenerCount(h.browser), 0)
  assert.equal(h.browser.classes.size, 0)
  h.api.onRowClick(pointer(), h.tabs[0])
  assert.equal(h.calls.length, 2, 'the drag-release click must not navigate')
  h.browser.flushTimers()
  h.api.onRowClick(pointer(), h.tabs[0])
  assert.deepEqual(h.calls.at(-1), ['select', 'a'])
  h.unmount()
})

test('row drag rejects wrong buttons, editing rows and action-menu controls', t => {
  const h = dragHarness(t)
  const control = new TestElement(); control.control = control
  for (const event of [pointer({ button: 1 }), pointer({ isPrimary: false }), pointer({ target: control })]) {
    h.api.onRowPointerDown(event, h.tabs[0])
    assert.equal(listenerCount(h.browser), 0)
  }
  h.dependencies.renamingTabId.value = 'a'
  h.api.onRowPointerDown(pointer(), h.tabs[0])
  assert.equal(listenerCount(h.browser), 0)
  h.api.onRowClick(pointer({ target: control }), h.tabs[0])
  assert.deepEqual(h.calls, [])
  h.unmount()
})

test('pin zone and group-start drops preserve pin semantics without moving across directories', t => {
  const h = dragHarness(t)
  h.api.onRowPointerDown(pointer(), h.tabs[0])
  const pin = new TestElement(); pin.pin = pin; h.browser.hit(pin)
  h.browser.fire('pointermove', pointer({ clientY: 10 }))
  h.browser.fire('pointerup', pointer())
  assert.deepEqual(h.calls, [['pin', 'a', true]])
  h.browser.flushTimers()
  h.api.onRowPointerDown(pointer(), h.tabs[0])
  h.target(0, null)
  h.browser.fire('pointermove', pointer({ clientY: 10 }))
  h.browser.fire('pointerup', pointer())
  assert.deepEqual(h.calls.slice(1), [['pin', 'a', false], ['row', 'a', 'b', 'before']])
  h.browser.flushTimers()
  h.api.onRowPointerDown(pointer(), h.tabs[0])
  h.target(1, 'foreign')
  h.browser.fire('pointermove', pointer({ clientY: 10 }))
  h.browser.fire('pointerup', pointer())
  assert.equal(h.calls.length, 3)
  h.unmount()
})

test('pointer cancellation and unmount remove row/group listeners, body classes and timers', t => {
  const h = dragHarness(t)
  h.api.onRowPointerDown(pointer(), h.tabs[0]); h.target()
  h.browser.fire('pointermove', pointer({ clientY: 30 }))
  h.browser.fire('pointercancel', pointer())
  assert.deepEqual(h.calls, [])
  assert.equal(listenerCount(h.browser), 0)
  assert.equal(h.browser.classes.size, 0)
  assert.equal(h.browser.timers.size, 1)
  h.api.onGroupPointerDown(pointer(), h.groups[0]); h.target(1, null)
  h.browser.fire('pointermove', pointer({ clientY: 30 }))
  assert.equal(h.browser.classes.has('chat-sidebar-group-dragging'), true)
  h.unmount()
  assert.equal(listenerCount(h.browser), 0)
  assert.equal(h.browser.classes.size, 0)
  assert.equal(h.browser.timers.size, 0)
  h.browser.fire('pointerup', pointer())
  assert.deepEqual(h.calls, [])
})

test('workspace drag ordering keeps its own threshold, filter guard and suppressed click', t => {
  const h = dragHarness(t)
  h.dependencies.filterText.value = 'query'
  h.api.onGroupPointerDown(pointer(), h.groups[0])
  assert.equal(listenerCount(h.browser), 0)
  h.dependencies.filterText.value = ''
  h.api.onGroupPointerDown(pointer(), { ...h.groups[0], pinned: true })
  assert.equal(listenerCount(h.browser), 0)
  h.api.onGroupPointerDown(pointer(), h.groups[0]); h.target(1, null)
  h.browser.fire('pointermove', pointer({ clientY: 30 }))
  assert.deepEqual(h.api.groupDropTarget.value, { cwd: '/other', position: 'after' })
  h.browser.fire('pointerup', pointer())
  h.api.onGroupHeaderClick('/repo')
  assert.deepEqual(h.calls, [['group', '/repo', '/other', 'after']])
  h.browser.flushTimers()
  h.api.onGroupHeaderClick('/repo')
  assert.deepEqual(h.calls.at(-1), ['toggle', '/repo'])
  h.unmount()
})

test('mobile row navigation uses selectMobileTab and never duplicates desktop selection', t => {
  const h = dragHarness(t)
  h.dependencies.isMobile.value = true
  h.api.onRowClick(pointer(), h.tabs[0])
  assert.deepEqual(h.calls, [['mobile', 'a']])
  h.unmount()
})

test('resize clamps keyboard and pointer widths and persists only the owned completion', t => {
  const browser = installBrowser(t)
  const key = 'claude_hub_chat_sidebar_width'
  browser.storage.set(key, '260')
  const collapsed = ref(false)
  const h = mountController(t, () => useSidebarResize(collapsed))
  assert.equal(h.api.sidebarWidth.value, 260)
  assert.deepEqual(h.api.sidebarStyle.value, { width: '260px', minWidth: '260px' })
  h.api.startResize(pointer({ clientX: 100 }))
  assert.equal(listenerCount(browser), 3)
  browser.fire('pointermove', pointer({ clientX: 1000 }))
  assert.equal(h.api.sidebarWidth.value, SIDEBAR_MAX_WIDTH)
  assert.equal(browser.storage.get(key), '260')
  browser.fire('pointercancel', pointer())
  assert.equal(browser.storage.get(key), '480')
  assert.equal(browser.classes.size, 0)
  assert.equal(listenerCount(browser), 0)
  h.api.resizeWithKeyboard(pointer({ key: 'Home' }))
  assert.equal(h.api.sidebarWidth.value, SIDEBAR_MIN_WIDTH)
  h.api.resizeWithKeyboard(pointer({ key: 'ArrowLeft' }))
  assert.equal(h.api.sidebarWidth.value, 200)
  h.api.resizeWithKeyboard(pointer({ key: 'End' }))
  h.api.resizeWithKeyboard(pointer({ key: 'ArrowLeft' }))
  assert.equal(h.api.sidebarWidth.value, 464)
  h.api.resetSidebarWidth()
  assert.equal(h.api.sidebarWidth.value, 240)
  collapsed.value = true
  assert.equal(h.api.sidebarStyle.value, undefined)
  h.api.startResize(pointer()); browser.fire('pointermove', pointer({ clientX: 10 }))
  h.unmount()
  assert.equal(browser.storage.get(key), '250')
  assert.equal(listenerCount(browser), 0)
  assert.equal(browser.classes.size, 0)
})

test('rename keeps one save in flight and ignores duplicate or stale row events', async t => {
  installBrowser(t)
  const calls = []
  let resolve
  const h = mountController(t, () => useSidebarRename((...args) => { calls.push(args); return new Promise(done => { resolve = done }) }))
  const input = new TestElement('input')
  h.api.setRenameInputRef(input)
  h.api.startRename(tab('a'))
  await nextTick()
  assert.equal(input.focused, true)
  assert.equal(input.selected, true)
  h.api.renamingTabName.value = ' renamed '
  const saving = h.api.handleRenameTab('a')
  h.api.startRename(tab('b'))
  h.api.cancelRename('a')
  await h.api.handleRenameTab('a'); await h.api.handleRenameTab('b')
  assert.equal(h.api.renamingTabId.value, 'a')
  assert.deepEqual(calls, [['a', { name: 'renamed' }]])
  resolve(); await saving
  assert.equal(h.api.renamingTabId.value, null)
  h.api.startRename(tab('b')); h.api.renamingTabName.value = ' '
  await h.api.handleRenameTab('b')
  assert.equal(calls.length, 1)
  assert.equal(h.api.renamingTabId.value, null)
  h.unmount()
})
