<template>
  <div
    ref="contentRef"
    class="inbox-content"
    :data-variant="variant"
    tabindex="-1"
    @keydown="onKeydown"
  >
    <div class="inbox-toolbar">
      <div
        class="inbox-scope"
        role="group"
        aria-label="Inbox filter"
      >
        <button
          v-for="option in SCOPE_OPTIONS"
          :key="option.value"
          type="button"
          :data-active="scope === option.value"
          @click="setScope(option.value)"
        >
          {{ option.label }}
        </button>
      </div>
      <LoadingButton
        type="button"
        class="inbox-refresh"
        title="Refresh inbox"
        :loading="isRefreshing"
        hide-content-while-loading
        loading-label="Refreshing inbox"
        @click="refresh"
      >
        ↻
      </LoadingButton>
    </div>

    <div
      v-if="renderedGroups.length === 0"
      class="inbox-empty"
    >
      {{ quietText }}
    </div>

    <div
      v-else
      class="inbox-groups"
    >
      <p
        v-if="quiet"
        class="inbox-quiet"
      >
        {{ quietText }}
      </p>
      <section
        v-for="group in renderedGroups"
        :key="group.key"
        class="inbox-group"
        :data-group="group.key"
      >
        <button
          v-if="group.key === 'done'"
          type="button"
          class="inbox-group-header inbox-group-header--toggle"
          :aria-expanded="!doneCollapsed"
          @click="doneCollapsed = !doneCollapsed"
        >
          <span>{{ group.title }}</span>
          <strong>{{ group.rows.length }}</strong>
          <span
            class="inbox-group-caret"
            aria-hidden="true"
          >{{ doneCollapsed ? '▸' : '▾' }}</span>
        </button>
        <div
          v-else
          class="inbox-group-header"
        >
          <span>{{ group.title }}</span>
          <strong>{{ group.rows.length }}</strong>
        </div>
        <div
          v-if="group.key !== 'done' || !doneCollapsed"
          class="inbox-group-body"
        >
          <div
            v-for="section in sectionsFor(group)"
            :key="`${group.key}:${section.key}`"
            class="inbox-section"
          >
            <div
              v-if="section.title"
              class="inbox-workspace-header"
            >
              <span :title="section.title">{{ section.title }}</span>
            </div>
            <ul class="inbox-list">
              <li
                v-for="row in section.rows"
                :key="row.tab.id"
                class="inbox-row"
                :data-row-tab-id="row.tab.id"
                :class="{ 'is-cursor': row.tab.id === cursorRowId, 'is-expanded': row.tab.id === expandedTabId }"
                :data-group="group.key"
                :data-workspace="row.tab.workspace_id || undefined"
              >
                <div class="inbox-row-line">
                  <button
                    :ref="el => registerRowButton(row.tab.id, el)"
                    type="button"
                    class="inbox-row-main"
                    :title="rowTooltip(row)"
                    @click="openTab(row)"
                    @focus="focusRow(row)"
                  >
                    <span
                      class="inbox-row-dot"
                      :data-status="rowState(row)"
                    />
                    <span class="inbox-row-text">
                      <span class="inbox-row-top">
                        <span class="inbox-row-name">{{ row.tab.name }}</span>
                        <span
                          v-if="row.tab.parent_tab_id"
                          class="inbox-row-sub"
                          title="Hub sub-agent started from another Chat"
                        >sub</span>
                        <span
                          v-if="row.tab.agent_type"
                          class="inbox-row-provider"
                          :data-kind="row.tab.agent_type"
                        >{{ row.tab.agent_type }}</span>
                      </span>
                      <span class="inbox-row-reason">{{ row.reason }}</span>
                      <span
                        v-if="row.excerpt"
                        class="inbox-row-excerpt"
                      >{{ row.excerpt }}</span>
                    </span>
                    <span class="inbox-row-meta">
                      <span
                        v-if="elapsedLabel(row)"
                        class="inbox-row-time"
                      >{{ elapsedLabel(row) }}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    class="inbox-row-expand"
                    :aria-expanded="row.tab.id === expandedTabId"
                    :aria-label="row.tab.id === expandedTabId ? `Collapse ${row.tab.name}` : `Expand ${row.tab.name}`"
                    @click="toggleExpand(row.tab.id)"
                  >
                    {{ row.tab.id === expandedTabId ? '▾' : '▸' }}
                  </button>
                </div>

                <div
                  v-if="row.tab.id === expandedTabId"
                  class="inbox-row-detail"
                >
                  <p
                    v-if="row.excerpt"
                    class="inbox-row-detail-text"
                  >
                    {{ row.excerpt }}
                  </p>
                  <button
                    v-if="row.status?.pending_question"
                    type="button"
                    class="inbox-row-answer"
                    @click="openTab(row)"
                  >
                    Answer in chat →
                  </button>
                  <form
                    v-else-if="canReply(row)"
                    class="inbox-reply"
                    @submit.prevent="submitReply(row)"
                  >
                    <input
                      v-model="draftFor(row.tab.id).text"
                      type="text"
                      class="inbox-reply-input"
                      :placeholder="`Reply to ${row.tab.name}`"
                      :aria-label="`Reply to ${row.tab.name}`"
                      :disabled="draftFor(row.tab.id).sending"
                    >
                    <button
                      type="submit"
                      class="inbox-reply-send"
                      :disabled="!draftFor(row.tab.id).text.trim() || draftFor(row.tab.id).sending"
                    >
                      {{ draftFor(row.tab.id).sending ? 'Sending…' : 'Send' }}
                    </button>
                  </form>
                  <p
                    v-if="draftFor(row.tab.id).error"
                    class="inbox-reply-error"
                    role="alert"
                  >
                    {{ draftFor(row.tab.id).error }}
                  </p>
                  <p
                    v-if="!row.status?.pending_question && !canReply(row)"
                    class="inbox-row-detail-note"
                  >
                    {{ replyBlockedReason(row) }}
                  </p>
                </div>
              </li>
            </ul>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingButton from '@/components/LoadingButton.vue'
import { useTerminalStore } from '@/stores/terminalStore'
import { canReplyInline, newInboxReplyTurnId, sendInboxReply } from './inboxReply'
import {
  buildInbox,
  flattenRows,
  formatElapsed,
  groupRowsByWorkspace,
  moveSelection,
  statusLabel,
  type InboxGroupData,
  type InboxRow,
  type InboxScope,
  type InboxWorkspaceSection,
} from './inboxModel'

withDefaults(defineProps<{
  /** `sheet` is the phone-sized presentation: full-height, never a popover. */
  variant?: 'panel' | 'sheet'
}>(), { variant: 'panel' })

const emit = defineEmits<{
  (event: 'open', tabId: string): void
  (event: 'close'): void
}>()

const SCOPE_OPTIONS: Array<{ value: InboxScope; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'manual', label: 'Chats' },
  { value: 'workspace', label: 'Agents' },
]

const ROW_STATE: Record<string, string> = {
  working: 'working',
  attention: 'attention',
  offline: 'offline',
  idle: 'idle',
}

const store = useTerminalStore()
const { tabs, agentStatuses } = storeToRefs(store)

const scope = ref<InboxScope>('all')
const doneCollapsed = ref(true)
const cursorRowId = ref<string | null>(null)
const expandedTabId = ref<string | null>(null)
const isRefreshing = ref(false)
const contentRef = ref<HTMLElement | null>(null)
const rowButtons = new Map<string, HTMLElement>()
// Elapsed labels and the "recent" window only need to be roughly current.
const nowMs = ref(Date.now())

interface ReplyDraft {
  text: string
  sending: boolean
  error: string | null
  clientTurnId: string
}

const drafts = reactive<Record<string, ReplyDraft>>({})
let clockTimer: number | null = null

const groups = computed<InboxGroupData[]>(() =>
  buildInbox(tabs.value, agentStatuses.value, { scope: scope.value, nowMs: nowMs.value }),
)

/** Groups as rendered: the collapsed "Recently done" bucket keeps its header. */
const renderedGroups = computed(() => groups.value.filter(group => group.rows.length > 0))

const visibleRows = computed<InboxRow[]>(() => {
  const buckets = groups.value.filter(group => group.key !== 'done' || !doneCollapsed.value)
  return flattenRows(buckets)
})

const cursorIndex = computed(() =>
  visibleRows.value.findIndex(row => row.tab.id === cursorRowId.value),
)

/**
 * Rows of one bucket, split by workspace in the Agents view. The old floating
 * panel always grouped managed sessions under their workspace; keeping that in
 * the Agents filter is what lets a sub-agent started from another Chat be read
 * as "this belongs to Repo" instead of a loose row. The Chats view stays flat.
 */
function sectionsFor(group: InboxGroupData): InboxWorkspaceSection[] {
  if (scope.value !== 'workspace') return [{ key: 'flat', title: '', rows: group.rows }]
  return groupRowsByWorkspace(group.rows)
}

/**
 * The filter is a view, not a different Hub: saying "nothing needs you" while
 * the trigger badge shows another filter's count would read as a bug.
 */
const quietText = computed(() => {
  if (scope.value === 'all') return 'Nothing needs you right now.'
  const label = SCOPE_OPTIONS.find(option => option.value === scope.value)?.label ?? 'this view'
  return `Nothing needs you in ${label}.`
})

/** Nothing needs the user; only finished history is left. */
const quiet = computed(() => groups.value.every(group => group.key === 'done' || group.rows.length === 0))

onMounted(() => {
  cursorRowId.value = visibleRows.value[0]?.tab.id ?? null
  contentRef.value?.focus?.()
  clockTimer = window.setInterval(() => {
    nowMs.value = Date.now()
  }, 30000)
})

onUnmounted(() => {
  if (clockTimer !== null) window.clearInterval(clockTimer)
  clockTimer = null
})

watch(visibleRows, rows => {
  if (rows.length === 0) {
    cursorRowId.value = null
    return
  }
  if (!rows.some(row => row.tab.id === cursorRowId.value)) {
    cursorRowId.value = rows[0].tab.id
  }
})

function setScope(next: InboxScope) {
  scope.value = next
  expandedTabId.value = null
}

function rowState(row: InboxRow): string {
  // A blocked question is its own state: the turn may still report "working"
  // while the provider is really waiting for the user.
  if (row.status?.pending_question) return 'needs'
  const status = row.status?.status ?? (row.group === 'working' ? 'working' : 'idle')
  return ROW_STATE[status] ?? 'idle'
}

function elapsedLabel(row: InboxRow): string {
  if (row.group === 'done' && row.sinceMs === null) return ''
  return formatElapsed(row.sinceMs, nowMs.value)
}

function rowTooltip(row: InboxRow): string {
  return `${row.tab.name} — ${statusLabel(row)}`
}

function registerRowButton(tabId: string, el: unknown) {
  const focusable = el as { focus?: () => void } | null
  if (focusable && typeof focusable.focus === 'function') rowButtons.set(tabId, focusable as HTMLElement)
  else rowButtons.delete(tabId)
}

function focusRow(row: InboxRow) {
  cursorRowId.value = row.tab.id
}

function moveCursor(delta: number) {
  const index = moveSelection(cursorIndex.value, delta, visibleRows.value.length)
  if (index < 0) return
  const row = visibleRows.value[index]
  if (!row) return
  cursorRowId.value = row.tab.id
  rowButtons.get(row.tab.id)?.focus?.()
}

async function toggleExpand(tabId: string) {
  expandedTabId.value = expandedTabId.value === tabId ? null : tabId
  cursorRowId.value = tabId
  if (expandedTabId.value !== tabId) return
  // The detail (reply field or full excerpt) can be below the scroll edge;
  // bring the row into view instead of leaving it half cut off.
  await nextTick()
  const rows = contentRef.value?.querySelectorAll?.('[data-row-tab-id]') ?? []
  for (const element of rows) {
    if (element.getAttribute?.('data-row-tab-id') === tabId) {
      (element as HTMLElement).scrollIntoView?.({ block: 'nearest' })
      return
    }
  }
}

function openTab(row: InboxRow) {
  emit('open', row.tab.id)
}

function canReply(row: InboxRow): boolean {
  return canReplyInline({
    chatTab: row.tab.session_kind === 'chat',
    pendingQuestion: Boolean(row.status?.pending_question),
    working: row.status?.status === 'working',
  })
}

function replyBlockedReason(row: InboxRow): string {
  if (row.tab.session_kind !== 'chat') return 'Open the terminal to type there.'
  if (row.status?.status === 'working') return 'This turn is still running — open the Chat to steer it.'
  return 'Open the Chat to reply.'
}

function draftFor(tabId: string): ReplyDraft {
  if (!drafts[tabId]) {
    drafts[tabId] = { text: '', sending: false, error: null, clientTurnId: newInboxReplyTurnId() }
  }
  return drafts[tabId]
}

async function submitReply(row: InboxRow) {
  const draft = draftFor(row.tab.id)
  const text = draft.text.trim()
  if (!text || draft.sending) return
  if (!canReply(row)) {
    draft.error = replyBlockedReason(row)
    return
  }
  draft.sending = true
  draft.error = null
  try {
    await sendInboxReply({ tab: row.tab, text, clientTurnId: draft.clientTurnId })
    // A deliberate send is acknowledged: clear the text and mint the next
    // turn identity only after the request succeeded.
    draft.text = ''
    draft.clientTurnId = newInboxReplyTurnId()
    void store.fetchAgentStatuses()
  } catch (error) {
    // Keep the text and the same client_turn_id: a retry must reuse the
    // identity of the turn that may have been accepted.
    draft.error = error instanceof Error ? error.message : 'Reply failed.'
  } finally {
    draft.sending = false
  }
}

async function refresh() {
  if (isRefreshing.value) return
  isRefreshing.value = true
  try {
    await Promise.all([store.fetchTabs(), store.fetchAgentStatuses()])
    nowMs.value = Date.now()
  } finally {
    isRefreshing.value = false
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    moveCursor(event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  if (event.key === 'ArrowRight') {
    if (!cursorRowId.value) return
    event.preventDefault()
    expandedTabId.value = cursorRowId.value
    return
  }
  if (event.key === 'ArrowLeft') {
    if (!cursorRowId.value) return
    event.preventDefault()
    if (expandedTabId.value === cursorRowId.value) expandedTabId.value = null
    return
  }
  if (event.key === 'Enter') {
    const row = visibleRows.value.find(candidate => candidate.tab.id === cursorRowId.value)
    // Enter inside the reply field submits the form, not the row.
    const target = event.target as HTMLElement | null
    if (target && target.tagName !== 'BUTTON' && target.classList?.contains('inbox-reply-input')) return
    if (row) {
      event.preventDefault()
      openTab(row)
    }
    return
  }
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

defineExpose({ focus: () => contentRef.value?.focus?.() })
</script>

<style scoped src="./agent-inbox.css"></style>
