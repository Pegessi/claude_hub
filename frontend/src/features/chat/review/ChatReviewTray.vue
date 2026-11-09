<template>
  <p
    class="review-tray-live"
    role="status"
    aria-live="polite"
  >
    {{ liveMessage }}
  </p>
  <section
    v-if="comments.length"
    class="review-tray"
    aria-label="Review comments"
  >
    <div class="review-tray-bar">
      <button
        type="button"
        class="review-tray-count"
        :aria-expanded="isOpen"
        @click="isOpen = !isOpen"
      >
        <span
          class="review-tray-chevron"
          aria-hidden="true"
        >{{ isOpen ? '▾' : '▸' }}</span>
        <span>{{ reviewCountLabel(comments.length) }}</span>
      </button>
      <button
        type="button"
        class="review-tray-send"
        @click="send"
      >
        Send to agent
      </button>
      <button
        type="button"
        class="review-tray-clear"
        @click="clearAll"
      >
        Clear
      </button>
    </div>
    <ol
      v-if="isOpen"
      class="review-tray-list"
    >
      <li
        v-for="comment in comments"
        :key="comment.id"
        class="review-tray-item"
      >
        <div class="review-tray-item-head">
          <button
            type="button"
            class="review-tray-jump"
            :title="`Show the diff line for ${reviewLocation(comment)}`"
            @click="reveal(comment)"
          >
            {{ reviewShortLocation(comment) }}
          </button>
          <div class="review-tray-item-actions">
            <template v-if="editingId === comment.id">
              <button
                type="button"
                :disabled="!editingText.trim()"
                @click="saveEdit(comment)"
              >
                Save
              </button>
              <button
                type="button"
                @click="stopEdit"
              >
                Cancel
              </button>
            </template>
            <template v-else>
              <button
                type="button"
                :aria-label="`Edit comment on ${reviewLocation(comment)}`"
                @click="startEdit(comment)"
              >
                Edit
              </button>
              <button
                type="button"
                :aria-label="`Remove comment on ${reviewLocation(comment)}`"
                @click="removeComment(comment.id)"
              >
                Remove
              </button>
            </template>
          </div>
        </div>
        <p
          v-if="editingId !== comment.id"
          class="review-tray-note"
        >
          {{ comment.text }}
        </p>
        <label
          v-else
          class="review-tray-editor"
        >Comment
          <textarea
            v-model="editingText"
            rows="2"
            :aria-label="`Edit comment on ${reviewLocation(comment)}`"
            @keydown.enter.exact.prevent="saveEdit(comment)"
            @keydown.escape="stopEdit"
          />
        </label>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { requireChatOwner, chatReviewKey, chatTimelineKey } from '../owners'
import { reviewCountLabel, reviewLocation, reviewShortLocation, type ReviewComment } from '../review/reviewComments'

const { comments, updateComment, removeComment, clearComments, requestFocus, sendReview } = requireChatOwner(chatReviewKey)
const { expandedToolGroups } = requireChatOwner(chatTimelineKey)

/** Open by default: the list is the useful part, the toggle only saves space. */
const isOpen = ref(true)
const editingId = ref<string | null>(null)
const editingText = ref('')
const liveMessage = ref('')

watch(() => comments.value.length, () => {
  if (editingId.value && !comments.value.some(comment => comment.id === editingId.value)) stopEdit()
})

/** Reopen the folded card holding the row before asking it to scroll into
 *  view; the row can only be measured once it is mounted. */
function reveal(comment: ReviewComment): void {
  const previous = expandedToolGroups.get(comment.turn)
  if (!previous?.has(comment.group)) {
    expandedToolGroups.set(comment.turn, new Set([...(previous ?? []), comment.group]))
  }
  void nextTick(() => requestFocus(comment.key))
  liveMessage.value = `Showing ${reviewLocation(comment)}`
}

function startEdit(comment: ReviewComment): void {
  editingId.value = comment.id
  editingText.value = comment.text
}

function stopEdit(): void {
  editingId.value = null
  editingText.value = ''
}

function saveEdit(comment: ReviewComment): void {
  updateComment(comment.id, editingText.value)
  stopEdit()
}

function clearAll(): void {
  const removed = comments.value.length
  stopEdit()
  clearComments()
  liveMessage.value = removed === 1 ? 'Review comment cleared' : `${removed} review comments cleared`
}

function send(): void {
  const sent = comments.value.length
  stopEdit()
  if (!sendReview()) return
  liveMessage.value = `${sent === 1 ? 'Comment' : `${sent} comments`} sent to the agent as one message`
}
</script>

<style scoped>
/* Review state stays with the composer: it is unsent input, not transcript. */
.review-tray {
  margin: 0 0 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
}

.review-tray-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
}

.review-tray-count {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 1 1 auto;
  min-width: 0;
  padding: 3px 4px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: none;
  color: var(--ch-color-text);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
}

.review-tray-count:hover {
  background: var(--ch-color-row-hover, var(--ch-color-surface-control-hover));
}

.review-tray-chevron {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
}

.review-tray-send {
  flex: 0 0 auto;
  padding: 3px 10px;
  border: 1px solid var(--ch-color-accent-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}

.review-tray-send:hover {
  background: var(--ch-color-accent-fill-hover);
}

.review-tray-clear {
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: none;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.review-tray-clear:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.review-tray-list {
  display: grid;
  gap: 4px;
  max-height: 168px;
  margin: 0;
  padding: 4px 6px 6px;
  overflow-y: auto;
  overscroll-behavior: contain;
  border-top: 1px solid var(--ch-color-border-muted);
  list-style: none;
}

.review-tray-item {
  display: grid;
  gap: 3px;
  min-width: 0;
  padding: 5px 7px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface);
}

.review-tray-item-head {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.review-tray-jump {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  padding: 1px 5px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-code);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  text-overflow: ellipsis;
  text-align: left;
  white-space: nowrap;
  cursor: pointer;
}

.review-tray-jump:hover {
  color: var(--ch-color-accent);
}

.review-tray-note {
  margin: 0;
  color: var(--ch-color-text);
  font-size: 12px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.review-tray-editor {
  display: grid;
  gap: 3px;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.review-tray-editor textarea {
  box-sizing: border-box;
  width: 100%;
  padding: 5px 7px;
  resize: vertical;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  font: inherit;
  font-size: 12px;
  letter-spacing: normal;
  text-transform: none;
}

.review-tray-item-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 4px;
  margin-left: auto;
}

.review-tray-item-actions button {
  padding: 2px 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface);
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.review-tray-item-actions button:hover:not(:disabled) {
  color: var(--ch-color-text);
  border-color: var(--ch-color-border-hover);
}

.review-tray-item-actions button:disabled {
  opacity: 0.5;
  cursor: default;
}

.review-tray-live {
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.review-tray-count:focus-visible,
.review-tray-send:focus-visible,
.review-tray-clear:focus-visible,
.review-tray-jump:focus-visible,
.review-tray-item-actions button:focus-visible,
.review-tray-editor textarea:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}

/* Touch: no hover to discover the controls, and a fingertip needs a taller
   target than a pointer. */
@media (hover: none), (pointer: coarse) {
  .review-tray-send,
  .review-tray-clear,
  .review-tray-jump,
  .review-tray-item-actions button {
    min-height: 44px;
  }

  .review-tray-count {
    min-height: 40px;
  }
}

@media (max-width: 640px) {
  .review-tray-list {
    max-height: 184px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .review-tray-count,
  .review-tray-clear {
    transition: none;
  }
}
</style>
