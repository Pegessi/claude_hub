import { computed, type ComputedRef, type Ref } from 'vue'
import type { Pane, TerminalTab } from '@/types'
import type { QueuedChatActivationRequest, QueuedChatState } from './types'

interface QueuedChatRuntime {
  tabs: Ref<TerminalTab[]>
  panes: Ref<Pane[]>
  activePaneId: Ref<string | null>
  activeTabId: Ref<string | null>
  states: Ref<QueuedChatState[]>
  cacheEpochs: Ref<Record<string, number>>
  requests: Ref<QueuedChatActivationRequest[]>
  setActivePane: (paneId: string) => void
  setActiveTab: (tabId: string) => void
}

export function createQueuedChatRuntime(runtime: QueuedChatRuntime) {
  const { tabs, panes, activePaneId, activeTabId, states, cacheEpochs, requests } = runtime
  let nextRequestId = 1
  const owner: ComputedRef<QueuedChatActivationRequest | null> = computed(() => requests.value[0] ?? null)

  function sameQueuedChat(
    entry: { paneId: string; tabId: string },
    paneId: string,
    tabId: string,
  ): boolean {
    return entry.paneId === paneId && entry.tabId === tabId
  }

  function stateIsProtected(state: QueuedChatState): boolean {
    return state.drafts.length > 0 || state.paused || state.sendInProgress || state.editing
  }

  function getState(paneId: string, tabId: string): QueuedChatState {
    let state = states.value.find(entry => entry.tabId === tabId)
    if (!state) {
      states.value.push({
        paneId,
        tabId,
        drafts: [],
        paused: false,
        editing: false,
        editingDraftId: null,
        editingMessage: '',
        sendInProgress: false,
        autoActivationBlock: null,
      })
      state = states.value[states.value.length - 1]
    } else {
      moveToPane(tabId, paneId)
    }
    return state
  }

  function moveToPane(tabId: string, paneId: string) {
    const state = states.value.find(entry => entry.tabId === tabId)
    if (!state || state.paneId === paneId) return
    state.paneId = paneId
    for (const request of requests.value) {
      if (request.tabId === tabId) request.paneId = paneId
    }
    ensureActivation()
  }

  function paneHasProtected(paneId: string): boolean {
    return states.value.some(state => state.paneId === paneId && stateIsProtected(state))
  }

  function protectedIds(paneId: string): string[] {
    return states.value
      .filter(state => state.paneId === paneId && stateIsProtected(state))
      .map(state => state.tabId)
  }

  function cacheKey(tabId: string): string {
    return `${tabId}:${cacheEpochs.value[tabId] ?? 0}`
  }

  function observedIds(paneId: string): string[] {
    return states.value
      .filter(state =>
        state.paneId === paneId &&
        state.drafts.length > 0 &&
        !state.paused &&
        !state.editing &&
        !state.sendInProgress
      )
      .map(state => state.tabId)
  }

  function discard(tabId: string) {
    states.value = states.value.filter(state => state.tabId !== tabId)
    const releasedOwner = requests.value[0]?.tabId === tabId
    requests.value = requests.value.filter(request => request.tabId !== tabId)
    cacheEpochs.value[tabId] = (cacheEpochs.value[tabId] ?? 0) + 1
    if (releasedOwner) ensureActivation()
  }

  function prune(paneId: string, tabId: string) {
    const state = states.value.find(entry => entry.tabId === tabId)
    if (
      state &&
      state.paneId === paneId &&
      !stateIsProtected(state) &&
      !requests.value.some(request => request.tabId === tabId)
    ) {
      states.value = states.value.filter(entry => entry !== state)
    }
  }

  function isOwner(paneId: string, tabId: string): boolean {
    const current = owner.value
    return Boolean(current && sameQueuedChat(current, paneId, tabId))
  }

  function hasRequest(paneId: string, tabId: string): boolean {
    return requests.value.some(entry => sameQueuedChat(entry, paneId, tabId))
  }

  function requestId(paneId: string, tabId: string): number | null {
    return requests.value.find(entry => sameQueuedChat(entry, paneId, tabId))?.id ?? null
  }

  function ensureActivation() {
    let current = owner.value
    while (current && (
      states.value.some(state => state.tabId === current?.tabId && state.editing) ||
      !tabs.value.some(tab => tab.id === current?.tabId) ||
      !panes.value.some(pane => pane.id === current?.paneId)
    )) {
      requests.value.shift()
      current = owner.value
    }
    if (!current) return
    if (activePaneId.value !== current.paneId) runtime.setActivePane(current.paneId)
    if (activeTabId.value !== current.tabId) runtime.setActiveTab(current.tabId)
  }

  function requestActivation(paneId: string, tabId: string) {
    if (getState(paneId, tabId).editing) return
    if (!requests.value.some(entry => entry.tabId === tabId)) {
      requests.value.push({ id: nextRequestId++, paneId, tabId })
    }
    ensureActivation()
  }

  function releaseActivation(paneId: string, tabId: string) {
    const index = requests.value.findIndex(entry => sameQueuedChat(entry, paneId, tabId))
    if (index < 0) return
    const releasedOwner = index === 0
    requests.value.splice(index, 1)
    if (releasedOwner) ensureActivation()
  }

  function settleActivation(tabId: string, expectedRequestId: number | null) {
    if (expectedRequestId === null) return
    const index = requests.value.findIndex(
      entry => entry.tabId === tabId && entry.id === expectedRequestId
    )
    if (index < 0) return
    const releasedOwner = index === 0
    requests.value.splice(index, 1)
    if (releasedOwner) ensureActivation()
  }

  return {
    owner,
    stateIsProtected,
    getState,
    moveToPane,
    paneHasProtected,
    protectedIds,
    cacheKey,
    observedIds,
    discard,
    prune,
    isOwner,
    hasRequest,
    requestId,
    ensureActivation,
    requestActivation,
    releaseActivation,
    settleActivation,
  }
}
