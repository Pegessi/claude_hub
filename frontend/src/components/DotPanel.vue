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
          :aria-label="`${identity.name} memory`"
          data-testid="dot-panel"
        >
          <div class="dot-panel__header">
            <DotAvatar
              :identity="identity"
              :size="32"
            />
            <div class="dot-panel__heading">
              <h2 class="dot-panel__title">
                {{ identity.name }}
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
              class="ch-btn ch-btn--ghost ch-btn--sm"
              :aria-expanded="editingIdentity"
              @click="toggleIdentityEditor"
            >
              {{ editingIdentity ? 'Cancel' : 'Edit' }}
            </button>
            <button
              type="button"
              class="dot-panel__close"
              :aria-label="`Close ${identity.name} memory`"
              @click="$emit('close')"
            >
              ×
            </button>
          </div>

          <!-- Name and avatar. A new name starts a fresh Dot session. -->
          <form
            v-if="editingIdentity"
            class="dot-identity"
            data-testid="dot-identity-editor"
            @submit.prevent="saveIdentity"
          >
            <label class="dot-identity__field">
              <span>Name</span>
              <input
                v-model="nameDraft"
                class="ch-input"
                maxlength="40"
                required
                autocomplete="off"
              >
            </label>
            <label class="dot-identity__field">
              <span>Emoji</span>
              <input
                v-model="avatarDraft"
                class="ch-input dot-identity__emoji"
                maxlength="16"
                placeholder="e.g. 🤖"
                autocomplete="off"
              >
            </label>
            <div class="dot-identity__field dot-identity__field--wide">
              <span>Picture</span>
              <div class="dot-identity__image">
                <label class="ch-btn ch-btn--sm dot-identity__upload">
                  {{ identity.avatar_image ? 'Change' : 'Upload' }}
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/gif,image/webp"
                    @change="uploadAvatar"
                  >
                </label>
                <button
                  v-if="identity.avatar_image"
                  type="button"
                  class="ch-btn ch-btn--ghost ch-btn--sm"
                  @click="removeAvatar"
                >
                  Remove
                </button>
              </div>
            </div>
            <p class="dot-identity__hint">
              PNG, JPEG, GIF or WebP, up to 2 MB. A picture wins over the emoji.
            </p>
            <button
              type="submit"
              class="ch-btn ch-btn--primary ch-btn--sm dot-identity__save"
              :disabled="!nameDraft.trim() || loading"
            >
              Save
            </button>
          </form>

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
                Nothing in focus. Tell {{ identity.name }} what matters.
              </p>
            </template>

            <!-- Work: delegated tasks and new events. -->
            <template v-else-if="section === 'work'">
              <form
                class="dot-row-form"
                data-testid="dot-watch-form"
                @submit.prevent="watchTask"
              >
                <input
                  v-model="watchId"
                  class="ch-input"
                  placeholder="Task id to watch"
                  aria-label="Task id to watch"
                  autocomplete="off"
                >
                <select
                  v-model.number="watchStallHours"
                  class="ch-select dot-row-form__priority"
                  aria-label="Stuck alarm after"
                  title="Alarm when the task shows no progress this long"
                >
                  <option
                    v-for="hours in STALL_CHOICES"
                    :key="hours"
                    :value="hours"
                  >
                    {{ hours }}h
                  </option>
                </select>
                <button
                  type="submit"
                  class="ch-btn ch-btn--primary ch-btn--sm"
                  :disabled="!watchId.trim()"
                >
                  Watch
                </button>
              </form>
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
                  <span :class="['dot-item__badge', { 'dot-item__badge--warn': ALERT_EVENTS.has(event.kind) }]">
                    {{ event.kind.replace('task_', '').replace('_', ' ') }}
                  </span>
                  <div class="dot-item__info">
                    <span class="dot-item__title">{{ event.text }}</span>
                    <span class="dot-item__sub">{{ relativeTime(event.at) }} ago</span>
                  </div>
                  <div
                    v-if="event.card_id"
                    class="dot-item__actions"
                  >
                    <button
                      type="button"
                      class="ch-btn ch-btn--sm"
                      @click="openCard(event.card_id)"
                    >
                      Result
                    </button>
                  </div>
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
                  <span class="dot-item__sub">
                    {{ item.task_id }} · {{ item.watched ? 'watched' : 'delegated' }}
                    <template v-if="item.review_due">· <strong>waiting for review</strong></template>
                    <template v-else-if="item.review">· reviewed {{ item.review.verdict }}</template>
                    · stuck alarm {{ item.stall_hours ?? DEFAULT_STALL_HOURS }}h
                    · {{ relativeTime(item.created) }} ago
                  </span>
                </div>
                <div class="dot-item__actions">
                  <button
                    type="button"
                    class="ch-btn ch-btn--ghost ch-btn--sm"
                    title="Stop watching this task"
                    @click="unwatchTask(item.task_id)"
                  >
                    Stop
                  </button>
                </div>
              </div>
              <p
                v-if="!loading && work.length === 0 && inbox.length === 0"
                class="dot-panel__empty"
              >
                No tracked work. Paste a task id above to watch it.
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

            <!-- Lessons: rules for briefs, learned from tasks that came back wrong. -->
            <template v-else-if="section === 'lessons'">
              <p
                v-if="reviewStats"
                class="dot-lessons__stats"
                data-testid="dot-lesson-stats"
              >
                {{ formatFirstPass(reviewStats) }}
                <template v-if="reviewStats.returned">
                  · {{ formatReviewCoverage(reviewStats) }}
                </template>
                <template v-if="causeText">
                  · misses: {{ causeText }}
                </template>
                <template v-if="reviewStats.pending">
                  · {{ reviewStats.pending }} waiting for review
                </template>
              </p>
              <form
                class="dot-row-form"
                data-testid="dot-lesson-form"
                @submit.prevent="addLesson"
              >
                <input
                  v-model="newLesson"
                  class="ch-input"
                  placeholder="Add a rule for briefs"
                  aria-label="New lesson"
                  maxlength="200"
                >
                <button
                  type="submit"
                  class="ch-btn ch-btn--primary ch-btn--sm"
                  :disabled="!newLesson.trim()"
                >
                  Add
                </button>
              </form>
              <div
                v-for="lesson in lessons"
                :key="lesson.id"
                class="dot-item"
                data-testid="dot-lesson"
              >
                <span
                  class="dot-item__badge"
                  :title="`${lesson.votes} task(s) missed this`"
                >×{{ lesson.votes }}</span>
                <form
                  v-if="editingLesson === lesson.id"
                  class="dot-row-form dot-lessons__edit"
                  @submit.prevent="saveLesson(lesson.id)"
                >
                  <input
                    v-model="lessonDraft"
                    class="ch-input"
                    aria-label="Lesson rule"
                    maxlength="200"
                  >
                  <button
                    type="submit"
                    class="ch-btn ch-btn--primary ch-btn--sm"
                    :disabled="!lessonDraft.trim()"
                  >
                    Save
                  </button>
                  <button
                    type="button"
                    class="ch-btn ch-btn--ghost ch-btn--sm"
                    @click="editingLesson = null"
                  >
                    Cancel
                  </button>
                </form>
                <template v-else>
                  <div class="dot-item__info">
                    <span class="dot-item__title">{{ lesson.rule }}</span>
                    <span class="dot-item__sub">
                      {{ lesson.reach }} · score {{ lesson.score }} · seen {{ relativeTime(lesson.seen) }} ago
                    </span>
                  </div>
                  <div class="dot-item__actions">
                    <button
                      type="button"
                      class="ch-btn ch-btn--sm"
                      @click="editLesson(lesson)"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      class="ch-btn ch-btn--ghost ch-btn--sm"
                      @click="dropLesson(lesson.id)"
                    >
                      Drop
                    </button>
                  </div>
                </template>
              </div>
              <p
                v-if="!loading && lessons.length === 0"
                class="dot-panel__empty"
              >
                No lessons yet. When a task comes back wrong because of its brief,
                {{ identity.name }} writes one rule here.
              </p>
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
import DotAvatar from '@/components/DotAvatar.vue'
import { useDotIdentity } from '@/composables/useDotIdentity'
import { useTerminalStore } from '@/stores/terminalStore'
import { relativeTime } from '@/utils/time'
import {
  ackDotInbox,
  addDotFocus,
  addDotLesson,
  archiveDotCard,
  clearDotAvatar,
  dropDotLesson,
  formatFirstPass,
  formatReviewCoverage,
  loadDotCard,
  loadDotCards,
  loadDotFocus,
  loadDotHealth,
  loadDotInbox,
  loadDotLessons,
  loadDotStatus,
  loadDotWork,
  recallDot,
  unwatchDotTask,
  updateDotFocus,
  updateDotIdentity,
  updateDotLesson,
  uploadDotAvatar,
  watchDotTask,
  type DotCard,
  type DotCardKind,
  type DotFocusItem,
  type DotHealth,
  type DotInboxEvent,
  type DotLesson,
  type DotReviewStats,
  type DotSearchHit,
  type DotStatus,
  type DotWorkItem,
} from '@/utils/dotApi'

type Section = 'focus' | 'work' | 'lessons' | 'memory' | 'health'

const SECTIONS: { key: Section, label: string }[] = [
  { key: 'focus', label: 'Focus' },
  { key: 'work', label: 'Work' },
  { key: 'lessons', label: 'Lessons' },
  { key: 'memory', label: 'Memory' },
  { key: 'health', label: 'Health' },
]
const KINDS: DotCardKind[] = ['user', 'work', 'knowledge', 'concept']
const DEFAULT_STALL_HOURS = 2
const STALL_CHOICES = [1, 2, 4, 8, 24]
const ALERT_EVENTS = new Set(['task_failed', 'task_needs_input', 'task_stalled', 'task_missing'])
const AVATAR_MAX_BYTES = 2 * 1024 * 1024

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
const lessons = ref<DotLesson[]>([])
const reviewStats = ref<DotReviewStats | null>(null)
const newLesson = ref('')
const editingLesson = ref<string | null>(null)
const lessonDraft = ref('')
const selectedCard = ref<DotCard | null>(null)
const newFocus = ref('')
const newPriority = ref(3)
const searchText = ref('')
const kindFilter = ref<DotCardKind | ''>('')
const watchId = ref('')
const watchStallHours = ref(DEFAULT_STALL_HOURS)
const { identity, setIdentity } = useDotIdentity()
const terminalStore = useTerminalStore()
const editingIdentity = ref(false)
const nameDraft = ref('')
const avatarDraft = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

const causeText = computed(() =>
  Object.entries(reviewStats.value?.causes ?? {}).map(([cause, count]) => `${cause} ${count}`).join(', '),
)

const cardTotal = computed(() =>
  Object.values(status.value?.cards ?? {}).reduce((sum, count) => sum + (count ?? 0), 0),
)

// Actions can overlap (a search while a section loads): loading ends with the last one.
let running = 0

async function run(action: () => Promise<void>) {
  errorText.value = ''
  running += 1
  loading.value = true
  try {
    await action()
  } catch (error) {
    errorText.value = error instanceof Error ? error.message : String(error)
  } finally {
    running -= 1
    loading.value = running > 0
  }
}

async function loadSection() {
  await run(async () => {
    status.value = await loadDotStatus()
    if (status.value.identity) setIdentity(status.value.identity)
    if (section.value === 'focus') focus.value = await loadDotFocus()
    else if (section.value === 'work') [work.value, inbox.value] = await Promise.all([loadDotWork(), loadDotInbox()])
    else if (section.value === 'lessons') await loadLessons()
    else if (section.value === 'memory') await loadCards()
    else health.value = await loadDotHealth()
  })
}

// A slow answer for an older search must not replace the newer one.
let cardsRequest = 0

async function loadCards() {
  const request = ++cardsRequest
  const kind = kindFilter.value || undefined
  const text = searchText.value.trim()
  const found = text ? await recallDot(text, kind) : await loadDotCards(kind)
  if (request === cardsRequest) cards.value = found
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

async function loadLessons() {
  const data = await loadDotLessons()
  lessons.value = data.lessons
  reviewStats.value = data.stats
}

function addLesson() {
  const rule = newLesson.value.trim()
  if (!rule) return
  void run(async () => {
    await addDotLesson(rule)
    newLesson.value = ''
    await loadLessons()
  })
}

function editLesson(lesson: DotLesson) {
  editingLesson.value = lesson.id
  lessonDraft.value = lesson.rule
}

function saveLesson(lessonId: string) {
  const rule = lessonDraft.value.trim()
  if (!rule) return
  void run(async () => {
    await updateDotLesson(lessonId, rule)
    editingLesson.value = null
    await loadLessons()
  })
}

function dropLesson(lessonId: string) {
  void run(async () => {
    await dropDotLesson(lessonId)
    await loadLessons()
  })
}

function toggleIdentityEditor() {
  editingIdentity.value = !editingIdentity.value
  nameDraft.value = identity.value.name
  avatarDraft.value = identity.value.avatar
}

function saveIdentity() {
  void run(async () => {
    const renamed = nameDraft.value.trim() !== identity.value.name
    setIdentity(await updateDotIdentity({ name: nameDraft.value.trim(), avatar: avatarDraft.value.trim() }))
    editingIdentity.value = false
    // The backend renamed the Dot tab too; refresh so the tab bar shows it now.
    if (renamed) await terminalStore.fetchTabs({ force: true })
  })
}

function uploadAvatar(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > AVATAR_MAX_BYTES) {
    errorText.value = 'The picture must be at most 2 MB.'
    return
  }
  void run(async () => {
    setIdentity(await uploadDotAvatar(file))
  })
}

function removeAvatar() {
  void run(async () => {
    setIdentity(await clearDotAvatar())
  })
}

function watchTask() {
  const taskId = watchId.value.trim()
  if (!taskId) return
  void run(async () => {
    await watchDotTask(taskId, watchStallHours.value)
    watchId.value = ''
    work.value = await loadDotWork()
  })
}

function unwatchTask(taskId: string) {
  void run(async () => {
    await unwatchDotTask(taskId)
    work.value = await loadDotWork()
  })
}

function ackInbox() {
  void run(async () => {
    // Only what is on screen: an event that arrived since must stay unread.
    await ackDotInbox(inbox.value.map(event => event.id))
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
    status.value = await loadDotStatus()
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
      editingIdentity.value = false
      editingLesson.value = null
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
  align-items: center;
  gap: 10px;
  padding: 14px 16px 10px;
}

.dot-panel__heading {
  flex: 1;
  min-width: 0;
}

.dot-panel__title {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

.dot-identity {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px;
  gap: 8px 10px;
  margin: 0 16px 10px;
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
}

.dot-identity__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  color: var(--ch-color-text-muted);
}

.dot-identity__field--wide,
.dot-identity__hint,
.dot-identity__save {
  grid-column: 1 / -1;
}

.dot-identity__emoji {
  text-align: center;
}

.dot-identity__image {
  display: flex;
  gap: 6px;
}

.dot-identity__upload {
  position: relative;
  overflow: hidden;
}

.dot-identity__upload input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.dot-identity__hint {
  margin: 0;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.dot-identity__save {
  justify-self: end;
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

.dot-lessons__stats {
  margin: 0 0 8px;
  padding: 0 4px;
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-muted);
}

.dot-lessons__edit {
  flex: 1;
  min-width: 0;
  margin-bottom: 0;
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

/* Phones: a full-screen sheet with finger-sized targets. Inputs use 16px text
   so iOS Safari does not zoom in on focus. */
@media (max-width: 768px) {
  .dot-panel {
    width: 100%;
    border-left: none;
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
  }

  .dot-panel__header {
    padding: 10px 8px 8px 14px;
  }

  .dot-panel__close {
    width: 44px;
    height: 44px;
    font-size: 24px;
  }

  .dot-identity {
    margin: 0 12px 10px;
  }

  .dot-panel__tabs {
    padding: 0 12px 10px;
  }

  .dot-panel__tabs .ch-segmented {
    display: flex;
    width: 100%;
    overflow-x: auto;
  }

  .dot-panel__tabs .ch-segmented__btn {
    flex: 1 0 auto;
    min-height: 36px;
  }

  .dot-panel__body {
    padding: 10px 8px;
  }

  .dot-panel .ch-input,
  .dot-panel .ch-select {
    min-height: 40px;
    font-size: 16px;
  }

  .dot-panel .ch-btn {
    min-height: 40px;
    padding-inline: 14px;
  }

  .dot-row-form {
    flex-wrap: wrap;
  }

  .dot-row-form .ch-input {
    flex: 1 1 100%;
  }

  .dot-row-form .ch-btn {
    flex: 1;
  }

  .dot-row-form__priority,
  .dot-row-form__kind {
    flex: 1;
  }

  .dot-item {
    flex-wrap: wrap;
    padding: 10px 8px;
  }

  /* Actions drop below the text instead of squeezing it. */
  .dot-item__info {
    flex-basis: calc(100% - 64px);
  }

  .dot-item__actions {
    margin-left: auto;
  }

  .dot-item__title {
    font-size: var(--ch-font-size-md);
  }

  .dot-item__sub {
    font-size: var(--ch-font-size-sm);
  }
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
