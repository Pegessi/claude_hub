import assert from 'node:assert/strict'
import test from 'node:test'
import { h, KeepAlive, nextTick, ref } from 'vue'
import { createSidebarLoader, installBrowser, makeRenderer, source } from './chatSidebarTestHarness.mjs'

const load = createSidebarLoader({
  'stores/terminalStore.ts': `let store; export const setStore = value => { store = value }; export const useTerminalStore = () => store`,
  'stores/appStore.ts': `import { ref } from 'vue'; const store = { mode: ref('terminal') }; export const useAppStore = () => store`,
  'composables/useViewport.ts': `import { ref } from 'vue'; export const mobile = ref(false); export const useViewport = () => ({ isMobile: mobile })`,
  'composables/useEdgeSwipeDrawer.ts': `import { ref } from 'vue'; export const calls = []; export function useEdgeSwipeDrawer(options) { calls.push(options); return { dragging: ref(false), translatePx: ref(0), backdropOpacity: ref(0) } }`,
  'composables/useTabStatus.ts': `export const useTabStatus = () => ({ getTabStatus: () => 'idle', getTabStatusLabel: () => 'Idle', isAwaitingAnswer: () => false, getTabActivityTime: tab => tab.created_at })`,
  'components/TabActionsMenu.vue': `<template><button class="menu-stub" /></template><script setup>defineProps(['tab', 'variant'])</script>`,
  'components/ChatSidebarWorkingEffect.vue': `<template><div class="working-effect" /></template><script setup>defineProps(['seed'])</script>`,
})
const { setStore } = await load('stores/terminalStore.ts')
const { calls: drawerCalls } = await load('composables/useEdgeSwipeDrawer.ts')
const { default: Sidebar } = await load('features/chat/sidebar/ChatSidebar.vue')
const { default: CompatibilitySidebar } = await load('components/ChatSidebar.vue')
const { default: Row } = await load('features/chat/sidebar/ChatSidebarRow.vue')
const { default: Group } = await load('features/chat/sidebar/ChatSidebarGroup.vue')

function find(node, predicate) {
  if (predicate(node)) return node
  for (const child of node.children ?? []) { const found = find(child, predicate); if (found) return found }
  return null
}
const hasClass = name => node => node.attributes?.class?.split(' ').includes(name)
const testTab = { id: 'a', name: 'Chat A', cwd: '/repo', session_kind: 'chat', created_at: '2026-10-17T00:00:00Z' }
function fakeStore() {
  return {
    chatTabs: ref([testTab]), pinnedChatIds: ref(new Set()), activeTabId: ref('a'), sidebarCollapsed: ref(false),
    archivedTabs: ref([]), agentStatuses: ref({}), mobileDrawerOpen: ref(false),
    toggleSidebar() {}, setActiveTab() {}, openMobileDrawer() {}, closeMobileDrawer() {}, selectMobileTab() {},
    updateTab: async () => {}, setChatPinned() {}, reorderTabById() {}, reorderChatWorkspace() {},
  }
}

test('public Sidebar re-export is the exact compiled feature component', () => {
  assert.equal(CompatibilitySidebar, Sidebar)
  assert.equal(Sidebar.name ?? Sidebar.__name, 'ChatSidebar')
  assert.match(source('components/ChatSidebar.vue'), /^<script lang="ts">\s*export \{ default \} from '@\/features\/chat\/sidebar\/ChatSidebar.vue'\s*<\/script>\s*$/)
})

test('switching public and feature imports under KeepAlive keeps one state and gesture owner', async t => {
  installBrowser(t)
  setStore(fakeStore())
  const useCompatibility = ref(true)
  const starts = drawerCalls.length
  const root = { children: [] }
  const app = makeRenderer().createApp({
    setup: () => () => h(KeepAlive, null, { default: () => h(useCompatibility.value ? CompatibilitySidebar : Sidebar, { key: 'sidebar' }) }),
  })
  app.mount(root)
  const original = find(root, node => node.tag === 'aside')
  assert.ok(original)
  assert.equal(drawerCalls.length, starts + 1)
  useCompatibility.value = false
  await nextTick()
  assert.ok(find(root, node => node.tag === 'aside') === original, 'KeepAlive must retain the sidebar host node')
  assert.equal(drawerCalls.length, starts + 1)
  app.unmount()
})

test('rename input keeps the native v-model composition guard and one parent-owned value', async t => {
  installBrowser(t)
  const name = ref('Chat A')
  const saved = []
  let inputRef
  const root = { children: [] }
  const app = makeRenderer().createApp({
    setup: () => () => h(Row, {
      tab: testTab, active: true, dragging: false, status: 'idle', statusLabel: 'Idle',
      renaming: true, renameName: name.value,
      'onUpdate:renameName': value => { name.value = value },
      setRenameInputRef: element => { inputRef = element },
      onSaveRename: id => saved.push(id),
    }),
  })
  app.mount(root)
  const input = find(root, hasClass('chat-sidebar__rename-input'))
  assert.ok(inputRef === input, 'The rename owner must reference the rendered input')
  assert.equal(input.value, 'Chat A')
  input.dispatchEvent({ type: 'compositionstart', target: input })
  input.value = 'new name'
  input.dispatchEvent({ type: 'input', target: input })
  assert.equal(name.value, 'Chat A', 'partial IME input must not update the rename owner')
  input.dispatchEvent({ type: 'compositionend', target: input })
  assert.equal(name.value, 'new name')
  input.attributes.onBlur({ target: input })
  assert.deepEqual(saved, ['a'])
  await nextTick()
  app.unmount()
  assert.ok(inputRef === null, 'Unmount must release the rename input reference')
})

test('a waiting question keeps the completion colour but drops the unread badge', async t => {
  installBrowser(t)
  const waiting = { ...testTab, is_unread: true }
  const root = { children: [] }
  const app = makeRenderer().createApp({
    setup: () => () => h(Row, {
      tab: waiting, active: false, dragging: false, status: 'idle', awaiting: true,
      statusLabel: 'Session status: Waiting for your answer',
      renaming: false, renameName: '', setRenameInputRef() {},
    }),
  })
  app.mount(root)
  const light = find(root, hasClass('chat-sidebar__item-status'))
  assert.ok(light)
  assert.equal(light.attributes['data-status'], 'idle')
  assert.equal(light.attributes['data-unread'], 'false', 'answering is the action, not reading')
  assert.equal(light.attributes['data-awaiting'], 'true', 'the row marks the wait')
  assert.equal(light.attributes['aria-label'], 'Session status: Waiting for your answer')
  // The pulse belongs to a running session and must not appear here.
  assert.equal(find(root, hasClass('working-effect')), null)
})

test('group slots and row components preserve the original DOM hierarchy and accessible events', async t => {
  installBrowser(t)
  const calls = []
  const group = { key: 'cwd:/repo', cwd: '/repo', pinned: false, collapsed: false, expanded: false, tabs: [testTab], visibleTabs: [testTab], hiddenCount: 0 }
  const root = { children: [] }
  const app = makeRenderer().createApp({
    setup: () => () => h(Group, { group, dragging: false, filterText: '', onGroupClick: cwd => calls.push(['group', cwd]) }, {
      default: () => h(Row, {
        tab: testTab, active: true, dragging: false, status: 'idle', statusLabel: 'Completed, unread',
        renaming: false, renameName: '', setRenameInputRef() {},
        onRowClick: (event, tab) => calls.push(['row', event, tab]), onRename: tab => calls.push(['rename', tab]),
      }),
    }),
  })
  app.mount(root)
  const groupNode = find(root, hasClass('chat-sidebar__group'))
  const items = find(root, hasClass('chat-sidebar__group-items'))
  const row = find(root, hasClass('chat-sidebar__item'))
  const button = find(root, hasClass('chat-sidebar__item-main'))
  assert.ok(items.parent === groupNode, 'The items container must remain inside its group')
  assert.ok(row.parent === items, 'component extraction must not introduce wrapper elements')
  assert.ok(button.parent === row, 'The row action must remain a direct child of its row')
  assert.equal(row.attributes['data-tab-id'], 'a')
  assert.equal(row.attributes.role, undefined)
  assert.equal(button.tag, 'button')
  assert.equal(button.attributes['aria-current'], 'page')
  assert.equal(find(root, hasClass('chat-sidebar__item-status')).attributes['aria-label'], 'Completed, unread')
  const click = { stopPropagation() { this.stopped = true } }
  button.attributes.onClick(click)
  assert.equal(click.stopped, true)
  assert.deepEqual(calls, [['row', click, testTab]])
  button.attributes.onDblclick(click)
  assert.deepEqual(calls.at(-1), ['rename', testTab])
  await nextTick()
  app.unmount()
})

test('sidebar defaults to Folders view and manually switches to Status view', async t => {
  installBrowser(t)
  setStore(fakeStore())
  const root = { children: [] }
  const app = makeRenderer().createApp({ setup: () => () => h(Sidebar) })
  app.mount(root)
  const switcher = find(root, hasClass('chat-sidebar__view-switch'))
  const buttons = (switcher?.children ?? []).filter(node => node.tag === 'button')
  const textOf = node => (node?.text ?? (node?.children ?? []).map(c => c.text ?? '').join('')).trim()
  assert.deepEqual(buttons.map(textOf), ['Folders', 'Status'])
  assert.equal(buttons[0].attributes['aria-pressed'], true)
  assert.equal(buttons[1].attributes['aria-pressed'], false)
  assert.equal(textOf(find(root, hasClass('chat-sidebar__group-label'))), 'repo')

  buttons[1].attributes.onClick()
  await nextTick()
  assert.equal(buttons[0].attributes['aria-pressed'], false)
  assert.equal(buttons[1].attributes['aria-pressed'], true)
  assert.equal(textOf(find(root, hasClass('chat-sidebar__group-label'))), 'Done')
  app.unmount()
})

test('collapsed Needs you auto-expands on new arrival and when switching back to Status view', async t => {
  installBrowser(t)
  const unreadA = { ...testTab, id: 'u-1', name: 'mr 4504 fix', is_unread: true }
  const unreadB = { ...testTab, id: 'u-2', name: 'llmserver bernard', is_unread: true }
  const store = fakeStore()
  store.chatTabs = ref([unreadA])
  store.archivedManualTabs = ref([testTab])
  store.archivedTabs = ref([
    testTab,
    { ...testTab, id: 'managed-arch', managed_session_id: 'ws-1', workspace_role: 'orchestrator' },
  ])
  setStore(store)
  const root = { children: [] }
  const app = makeRenderer().createApp({ setup: () => () => h(Sidebar) })
  app.mount(root)

  const textOf = node => (node?.text ?? (node?.children ?? []).map(c => c.text ?? '').join('')).trim()
  // Archived button counts personal/manual archived tabs rather than background Workspace sessions.
  const archiveBtn = find(root, hasClass('chat-sidebar__archived-btn'))
  assert.match(textOf(archiveBtn), /Archived \(1\)/)

  // Switch to Status view -> Needs you is visible and expanded.
  const switcher = find(root, hasClass('chat-sidebar__view-switch'))
  const [foldersBtn, statusBtn] = (switcher?.children ?? []).filter(node => node.tag === 'button')
  statusBtn.attributes.onClick()
  await nextTick()

  const findGroup = key => find(root, node => node.attributes?.['data-group-key'] === key)
  let needsYouGroup = findGroup('status:needs_you')
  assert.ok(needsYouGroup, 'expected Needs you group')
  let header = find(needsYouGroup, hasClass('chat-sidebar__group-header'))
  assert.equal(header.attributes['aria-expanded'], true)
  assert.match(header.attributes.title, /Click to collapse/)
  assert.equal(find(needsYouGroup, hasClass('chat-sidebar__group-count--alert')), null)

  // User collapses Needs you -> badge switches to alert pill and title flips to Click to expand.
  header.attributes.onClick()
  await nextTick()
  needsYouGroup = findGroup('status:needs_you')
  header = find(needsYouGroup, hasClass('chat-sidebar__group-header'))
  assert.equal(header.attributes['aria-expanded'], false)
  assert.match(header.attributes.title, /Click to expand/)
  assert.ok(find(needsYouGroup, hasClass('chat-sidebar__group-count--alert')))

  // A new unread session arrives in Needs you -> group automatically re-expands.
  store.chatTabs.value = [unreadA, unreadB]
  await nextTick()
  needsYouGroup = findGroup('status:needs_you')
  header = find(needsYouGroup, hasClass('chat-sidebar__group-header'))
  assert.equal(header.attributes['aria-expanded'], true)

  // Collapse again, switch to Folders, then switch back to Status -> Needs you expands again.
  header.attributes.onClick()
  await nextTick()
  assert.equal(find(findGroup('status:needs_you'), hasClass('chat-sidebar__group-header')).attributes['aria-expanded'], false)
  foldersBtn.attributes.onClick()
  await nextTick()
  statusBtn.attributes.onClick()
  await nextTick()
  assert.equal(find(findGroup('status:needs_you'), hasClass('chat-sidebar__group-header')).attributes['aria-expanded'], true)

  app.unmount()
})
