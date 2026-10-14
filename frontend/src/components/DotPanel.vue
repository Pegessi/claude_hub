<template>
  <Teleport to="body">
    <Transition name="dot-fade">
      <div
        v-if="open"
        class="dot-overlay"
        @click.self="$emit('close')"
      >
        <aside
          class="dot-panel"
          role="dialog"
          aria-label="Dot memory"
          data-testid="dot-panel"
        >
          <div class="dot-panel__header">
            <div>
              <h2 class="dot-panel__title">
                Dot
              </h2>
              <p
                v-if="status"
                class="dot-panel__meta"
              >
                {{ cardTotal }} cards · session turn {{ status.session.turns }}/{{ status.session.max_turns }}
                · tidy {{ status.last_tidy_at ? `${relativeTime(status.last_tidy_at)} ago` : 'never' }}
              </p>
            </div>
            <button
              type="button"
              class="dot-panel__close"
              aria-label="Close Dot memory"
              @click="$emit('close')"
            >
              ×
            </button>
          </div>

          <div class="dot-panel__tabs">
            <div
              class="ch-segmented"
              role="tablist"
            >
              <button
                v-for="item in SECTIONS"
                :key="item.key"
                type="button"
                role="tab"
                :class="['ch-segmented__btn', { active: section === item.key }]"
                :aria-selected="section === item.key"
                @click="section = item.key"
              >
                {{ item.label }}
              </button>
            </div>
          </div>

          <div class="dot-panel__body">
            <p
              v-if="errorText"
              class="dot-panel__error"
            >
              {{ errorText }}
            </p>

            <!-- Focus: what matters now (at most 7). -->
            <template v-if="section === 'focus'">
              <form
                class="dot-row-form"
                @submit.prevent="addFocus"
              >
                <input
                  v-model="newFocus"
                  class="ch-input"
                  placeholder="Add a focus item"
                  maxlength="300"
                >
                <select
                  v-model.number="newPriority"
                  class="ch-select dot-row-form__priority"
                  aria-label="Priority"
                >
                  <option
                    v-for="p in 5"
                    :key="p"
                    :value="p"
                  >
                    P{{ p }}
                  </option>
                </select>
                <button
                  type="submit"
                  class="ch-btn ch-btn--primary ch-btn--sm"
                  :disabled="!newFocus.trim()"
                >
                  Add
                </button>
              </form>
              <div
                v-for="item in focus"
                :key="item.id"
                class="dot-item"
              >
                <span class="dot-item__badge">P{{ item.priority }}</span>
                <div class="dot-item__info">
                  <span class="dot-item__title">{{ item.title }}</span>
                  <span
                    v-if="item.why"
                    class="dot-item__sub"
                  >{{ item.why }}</span>
                </div>
                <div class="dot-item__actions">
                  <button
                    type="button"
                    class="ch-btn ch-btn--sm"
                    @click="closeFocus(item.id, 'done')"
                  >
                    Done
                  </button>
                  <button
                    type="button"
                    class="ch-btn ch-btn--ghost ch-btn--sm"
                    @click="closeFocus(item.id, 'dropped')"
                  >
                    Drop
                  </button>
                </div>
              </div>
              <p
                v-if="!loading && focus.length === 0"
                class="dot-panel__empty"
              >
                Nothing in focus. Tell Dot what matters.
              </p>
            </template>

            <!-- Work: delegated tasks and new events. -->
            <template v-else-if="section === 'work'">
              <div
                v-if="inbox.length"
                class="dot-group"
              >
                <div class="dot-group__head">
                  <span>New events ({{ inbox.length }})</span>
                  <button
                    type="button"
                    class="ch-btn ch-btn--ghost ch-btn--sm"
                    @click="ackInbox"
                  >
                    Mark read
                  </button>
                </div>
                <div
                  v-for="event in inbox"
                  :key="event.id"
                  class="dot-item"
                >
                  <span class="dot-item__badge">{{ event.kind.replace('task_', '') }}</span>
                  <div class="dot-item__info">
                    <span class="dot-item__title">{{ event.text }}</span>
                    <span class="dot-item__sub">{{ relativeTime(event.at) }} ago</span>
                  </div>
                  <button
                    v-if="event.card_id"
                    type="button"
                    class="ch-btn ch-btn--sm"
                    @click="openCard(event.card_id)"
                  >
                    Result
                  </button>
                </div>
              </div>
              <div
                v-for="item in work"
                :key="item.task_id"
                class="dot-item"
              >
                <span class="dot-item__badge">{{ item.status || '?' }}</span>
                <div class="dot-item__info">
                  <span class="dot-item__title">{{ item.title || item.task_id }}</span>
                  <span class="dot-item__sub">{{ item.task_id }} · {{ relativeTime(item.created) }} ago</span>
                </div>
              </div>
              <p
                v-if="!loading && work.length === 0 && inbox.length === 0"
                class="dot-panel__empty"
              >
                No delegated work.
              </p>
            </template>

            <!-- Memory: search and read cards. -->
            <template v-else-if="section === 'memory'">
              <template v-if="selectedCard">
                <button
                  type="button"
                  class="ch-btn ch-btn--ghost ch-btn--sm dot-inline-btn"
                  @click="selectedCard = null"
                >
                  ← Back
                </button>
                <article class="dot-card">
                  <h3 class="dot-card__title">
                    {{ selectedCard.title }}
                  </h3>
                  <p class="dot-item__sub">
                    {{ selectedCard.kind }} · importance {{ selectedCard.importance }}
                    · updated {{ relativeTime(selectedCard.updated) }} ago
                  </p>
                  <p
                    v-if="selectedCard.summary"
                    class="dot-card__summary"
                  >
                    {{ selectedCard.summary }}
                  </p>
                  <p
                    v-if="selectedCard.tags.length"
                    class="dot-item__sub"
                  >
                    {{ selectedCard.tags.map(tag => `#${tag}`).join(' ') }}
                  </p>
                  <ul
                    v-if="selectedCard.files.length"
                    class="dot-card__files"
                  >
                    <li
                      v-for="file in selectedCard.files"
                      :key="file"
                    >
                      <a
                        :href="`/api/dot/${file}`"
                        target="_blank"
                        rel="noopener"
                      >{{ file }}</a>
                    </li>
                  </ul>
                  <pre
                    v-if="selectedCard.body"
                    class="dot-card__body"
                  >{{ selectedCard.body }}</pre>
                  <button
                    v-if="!selectedCard.archived"
                    type="button"
                    class="ch-btn ch-btn--danger ch-btn--sm dot-inline-btn"
                    @click="archiveCard(selectedCard.id)"
                  >
                    Archive
                  </button>
                </article>
              </template>
              <template v-else>
                <div class="dot-row-form">
                  <input
                    v-model="searchText"
                    class="ch-input"
                    type="search"
                    placeholder="Search memory"
                    aria-label="Search memory"
                  >
                  <select
                    v-model="kindFilter"
                    class="ch-select dot-row-form__kind"
                    aria-label="Card kind"
                  >
                    <option value="">
                      All
                    </option>
                    <option
                      v-for="kind in KINDS"
                      :key="kind"
                      :value="kind"
                    >
                      {{ kind }}
                    </option>
                  </select>
                </div>
                <button
                  v-for="card in cards"
                  :key="card.id"
                  type="button"
                  class="dot-item dot-item--button"
                  @click="openCard(card.id)"
                >
                  <span class="dot-item__badge">{{ card.kind }}</span>
                  <span class="dot-item__info">
                    <span class="dot-item__title">{{ card.title }}</span>
                    <span class="dot-item__sub">{{ card.summary }}</span>
                  </span>
                </button>
                <p
                  v-if="!loading && cards.length === 0"
                  class="dot-panel__empty"
                >
                  {{ searchText.trim() ? 'No match.' : 'No memory yet.' }}
                </p>
              </template>
            </template>

            <!-- Health: deterministic checks, no model call. -->
            <template v-else>
              <div
                v-for="check in health?.checks ?? []"
                :key="check.name"
                class="dot-item"
              >
                <span :class="['dot-item__badge', check.ok ? 'dot-item__badge--ok' : 'dot-item__badge--warn']">
                  {{ check.ok ? 'ok' : 'warn' }}
                </span>
                <div class="dot-item__info">
                  <span class="dot-item__title">{{ check.name }}</span>
                  <span class="dot-item__sub">{{ check.detail }}</span>
                </div>
              </div>
            </template>
          </div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { relativeTime } from '@/utils/time'
import {
  ackDotInbox,
  addDotFocus,
  archiveDotCard,
  loadDotCard,
  loadDotCards,
  loadDotFocus,
  loadDotHealth,
  loadDotInbox,
  loadDotStatus,
  loadDotWork,
  recallDot,
  updateDotFocus,
  type DotCard,
  type DotCardKind,
  type DotFocusItem,
  type DotHealth,
  type DotInboxEvent,
  type DotSearchHit,
  type DotStatus,
  type DotWorkItem,
} from '@/utils/dotApi'

type Section = 'focus' | 'work' | 'memory' | 'health'

const SECTIONS: { key: Section, label: string }[] = [
  { key: 'focus', label: 'Focus' },
  { key: 'work', label: 'Work' },
  { key: 'memory', label: 'Memory' },
  { key: 'health', label: 'Health' },
]
const KINDS: DotCardKind[] = ['user', 'work', 'knowledge', 'concept']

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const section = ref<Section>('focus')
const loading = ref(false)
const errorText = ref('')
const status = ref<DotStatus | null>(null)
const focus = ref<DotFocusItem[]>([])
const work = ref<DotWorkItem[]>([])
const inbox = ref<DotInboxEvent[]>([])
const cards = ref<(DotCard | DotSearchHit)[]>([])
const health = ref<DotHealth | null>(null)
const selectedCard = ref<DotCard | null>(null)
const newFocus = ref('')
const newPriority = ref(3)
const searchText = ref('')
const kindFilter = ref<DotCardKind | ''>('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

const cardTotal = computed(() =>
  Object.values(status.value?.cards ?? {}).reduce((sum, count) => sum + (count ?? 0), 0),
)

async function run(action: () => Promise<void>) {
  errorText.value = ''
  loading.value = true
  try {
    await action()
  } catch (error) {
    errorText.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

async function loadSection() {
  await run(async () => {
    status.value = await loadDotStatus()
    if (section.value === 'focus') focus.value = await loadDotFocus()
    else if (section.value === 'work') [work.value, inbox.value] = await Promise.all([loadDotWork(), loadDotInbox()])
    else if (section.value === 'memory') await loadCards()
    else health.value = await loadDotHealth()
  })
}

async function loadCards() {
  const kind = kindFilter.value || undefined
  const text = searchText.value.trim()
  cards.value = text ? await recallDot(text, kind) : await loadDotCards(kind)
}

function addFocus() {
  const title = newFocus.value.trim()
  if (!title) return
  void run(async () => {
    await addDotFocus({ title, priority: newPriority.value })
    newFocus.value = ''
    focus.value = await loadDotFocus()
  })
}

function closeFocus(focusId: string, state: 'done' | 'dropped') {
  void run(async () => {
    await updateDotFocus(focusId, { status: state })
    focus.value = await loadDotFocus()
  })
}

function ackInbox() {
  void run(async () => {
    await ackDotInbox()
    inbox.value = await loadDotInbox()
  })
}

function openCard(cardId: string) {
  section.value = 'memory'
  void run(async () => {
    selectedCard.value = await loadDotCard(cardId)
  })
}

function archiveCard(cardId: string) {
  void run(async () => {
    await archiveDotCard(cardId)
    selectedCard.value = null
    await loadCards()
  })
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  isOpen => {
    if (isOpen) {
      selectedCard.value = null
      void loadSection()
      window.addEventListener('keydown', handleKeydown)
    } else {
      window.removeEventListener('keydown', handleKeydown)
    }
  },
)
watch(section, () => {
  if (props.open) void loadSection()
})
watch([searchText, kindFilter], () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { void run(loadCards) }, 250)
})
onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.dot-overlay {
  position: fixed;
  inset: 0;
  z-index: 900;
  background: var(--ch-color-overlay);
}

.dot-panel {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 420px;
  max-width: 100vw;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-surface);
  border-left: 1px solid var(--ch-color-border);
  box-shadow: var(--ch-shadow-soft);
}

.dot-panel__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 14px 16px 10px;
}

.dot-panel__title {
  margin: 0;
  font-size: var(--ch-font-size-lg);
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.dot-panel__meta {
  margin: 2px 0 0;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.dot-panel__close {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: 18px;
  cursor: pointer;
}

.dot-panel__close:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.dot-panel__tabs {
  padding: 0 16px 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.dot-panel__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.dot-panel__empty {
  padding: 24px 8px;
  text-align: center;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-subtle);
}

.dot-panel__error {
  margin: 0 0 8px;
  padding: 8px 10px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
  font-size: var(--ch-font-size-sm);
}

.dot-row-form {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 8px;
}

.dot-row-form__priority {
  width: 72px;
  flex: 0 0 auto;
}

.dot-row-form__kind {
  width: 120px;
  flex: 0 0 auto;
}

.dot-inline-btn {
  align-self: flex-start;
}

.dot-group {
  margin-bottom: 10px;
}

.dot-group__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  color: var(--ch-color-text-muted);
}

.dot-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--ch-radius-md);
}

.dot-item:hover {
  background: var(--ch-color-row-hover);
}

.dot-item--button {
  width: 100%;
  border: none;
  background: transparent;
  text-align: left;
  cursor: pointer;
  font: inherit;
}

.dot-item__badge {
  flex: 0 0 auto;
  min-width: 34px;
  padding: 2px 6px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  text-align: center;
}

.dot-item__badge--ok {
  color: var(--ch-color-success-text, var(--ch-color-text-muted));
}

.dot-item__badge--warn {
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
}

.dot-item__info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.dot-item__title {
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  color: var(--ch-color-text);
  overflow-wrap: anywhere;
}

.dot-item__sub {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-muted);
  overflow-wrap: anywhere;
}

.dot-item__actions {
  display: flex;
  gap: 4px;
  flex: 0 0 auto;
}

.dot-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 4px;
}

.dot-card__title {
  margin: 0;
  font-size: var(--ch-font-size-md);
  color: var(--ch-color-text-strong);
}

.dot-card__summary {
  margin: 0;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text);
}

.dot-card__files {
  margin: 0;
  padding-left: 18px;
  font-size: var(--ch-font-size-xs);
}

.dot-card__body {
  margin: 0;
  padding: 10px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.dot-fade-enter-active,
.dot-fade-leave-active {
  transition: opacity var(--ch-motion-standard);
}

.dot-fade-enter-from,
.dot-fade-leave-to {
  opacity: 0;
}
</style>
