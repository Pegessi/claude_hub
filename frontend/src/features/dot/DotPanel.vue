<template>
  <Teleport to="body">
    <Transition name="dot-fade">
      <div
        v-if="open"
        class="dot-overlay"
        @click.self="closePanel"
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
                · tidy {{ status.last_tidy_at ? ago(status.last_tidy_at) : 'never' }}
              </p>
            </div>
            <button
              type="button"
              class="ch-btn ch-btn--ghost ch-btn--sm"
              :aria-expanded="editingIdentity"
              :disabled="executionBusy"
              @click="toggleIdentityEditor"
            >
              {{ editingIdentity ? 'Cancel' : 'Edit' }}
            </button>
            <button
              type="button"
              class="dot-panel__close"
              :aria-label="`Close ${identity.name} memory`"
              :disabled="executionBusy"
              @click="closePanel"
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
                :disabled="executionBusy"
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
                :disabled="executionBusy"
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
                    :disabled="executionBusy"
                    accept="image/png,image/jpeg,image/gif,image/webp"
                    @change="uploadAvatar"
                  >
                </label>
                <button
                  v-if="identity.avatar_image"
                  type="button"
                  class="ch-btn ch-btn--ghost ch-btn--sm"
                  :disabled="executionBusy"
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
              :disabled="!nameDraft.trim() || loading || executionBusy"
            >
              Save
            </button>
          </form>

          <DotExecutionEditor
            v-if="editingIdentity"
            :disabled="loading"
            @busy-change="setExecutionBusy"
            @updated="loadSection"
          />

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
                    <span class="dot-item__sub">{{ ago(event.at) }}</span>
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
                    · {{ ago(item.created) }}
                  </span>
                </div>
                <div class="dot-item__actions">
                  <button
                    type="button"
                    class="ch-btn ch-btn--ghost ch-btn--sm"
                    title="Stop watching this task"
                    @click="unwatchTask(item)"
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
                  @click="closeCard"
                >
                  ← Back
                </button>
                <article class="dot-card">
                  <h3 class="dot-card__title">
                    {{ selectedCard.title }}
                  </h3>
                  <p class="dot-item__sub">
                    {{ selectedCard.kind }} · importance {{ selectedCard.importance }}
                    · updated {{ ago(selectedCard.updated) }}
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
                    maxlength="2000"
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
                      {{ lesson.reach }} · score {{ lesson.score }} · seen {{ ago(lesson.seen) }}
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
import DotAvatar from '@/components/DotAvatar.vue'
import DotExecutionEditor from './DotExecutionEditor.vue'
import { useDotPanel, type DotPanelProps } from './useDotPanel'

const props = defineProps<DotPanelProps>()
const emit = defineEmits<{ close: [] }>()
const {
  SECTIONS, KINDS, DEFAULT_STALL_HOURS, STALL_CHOICES, ALERT_EVENTS, section, loading, errorText, status,
  focus, work, inbox, cards, health, lessons, reviewStats, newLesson, editingLesson,
  lessonDraft, selectedCard, newFocus, newPriority, searchText, kindFilter, watchId,
  watchStallHours, identity, editingIdentity, executionBusy, nameDraft, avatarDraft, causeText, cardTotal,
  ago, addFocus, closeFocus, addLesson, editLesson, saveLesson, dropLesson,
  toggleIdentityEditor, setExecutionBusy, closePanel, loadSection, saveIdentity, uploadAvatar, removeAvatar,
  watchTask, unwatchTask, ackInbox, openCard, closeCard, archiveCard, formatFirstPass, formatReviewCoverage,
} = useDotPanel(props, { close: () => emit('close') })
</script>

<style scoped src="./dot-panel.css"></style>
