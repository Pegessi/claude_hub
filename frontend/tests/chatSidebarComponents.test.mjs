import assert from 'node:assert/strict'
import test from 'node:test'
import { h, KeepAlive, nextTick, ref } from 'vue'
import { createSidebarLoader, installBrowser, makeRenderer, source } from './chatSidebarTestHarness.mjs'

const load = createSidebarLoader({
  'stores/terminalStore.ts': `let store; export const setStore = value => { store = value }; export const useTerminalStore = () => store`,
  'stores/appStore.ts': `import { ref } from 'vue'; const store = { mode: ref('terminal') }; export const useAppStore = () => store`,
  'composables/useViewport.ts': `import { ref } from 'vue'; export const mobile = ref(false); export const useViewport = () => ({ isMobile: mobile })`,
  'composables/useEdgeSwipeDrawer.ts': `import { ref } from 'vue'; export const calls = []; export function useEdgeSwipeDrawer(options) { calls.push(options); return { dragging: ref(false), translatePx: ref(0), backdropOpacity: ref(0) } }`,
  'composables/useTabStatus.ts': `export const useTabStatus = () => ({ getTabStatus: () => 'idle', getTabStatusLabel: () => 'Idle' })`,
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
  assert.equal(find(root, node => node.tag === 'aside'), original)
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
  assert.equal(inputRef, input)
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
  assert.equal(inputRef, null)
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
  assert.equal(items.parent, groupNode)
  assert.equal(row.parent, items, 'component extraction must not introduce wrapper elements')
  assert.equal(button.parent, row)
  assert.equal(row.attributes['data-tab-id'], 'a')
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
