<template>
  <section
    v-if="drafts.length"
    class="draft-queue"
    aria-label="Queued messages"
  >
    <p
      class="queue-heading"
      aria-live="polite"
    >
      {{ drafts.length }} {{ drafts.length === 1 ? 'message' : 'messages' }} queued <span v-if="goalActive">· Waiting until the Goal stops</span>
    </p>
    <article
      v-for="(draft, index) in drafts"
      :key="draft.id"
      class="queued-draft"
    >
      <details :open="draft.error || editingId === draft.id ? true : undefined">
        <summary><span>{{ index + 1 }}. {{ draft.message.trim() || 'Image message' }}</span><small>{{ draft.attachments.length ? `${draft.attachments.length} ${draft.attachments.length === 1 ? 'image' : 'images'}` : '' }}</small></summary>
        <label
          v-if="editingId === draft.id"
          class="queue-editor"
        >Message
          <textarea
            v-model="editMessage"
            rows="3"
            :disabled="busy"
          />
        </label>
        <p
          v-else
          class="queue-message"
        >
          {{ draft.message || 'Image message' }}
        </p>
        <div
          v-if="draft.attachments.length"
          class="queue-images"
        >
          <div
            v-for="attachment in draft.attachments"
            :key="attachment.id"
            class="queue-image"
          >
            <button
              type="button"
              :aria-label="`View ${attachment.filename}`"
              @click="emit('preview', attachment, $event)"
            >
              <img
                :src="attachment.preview_url"
                :alt="attachment.filename"
              >
            </button>
            <button
              type="button"
              class="remove-image"
              :disabled="busy"
              :aria-label="`Remove ${attachment.filename}`"
              @click="emit('update', draft.id, draft.message, draft.attachments.filter(item => item.id !== attachment.id))"
            >
              ×
            </button>
          </div>
        </div>
      </details>
      <p
        v-if="draft.error"
        class="queue-error"
        role="alert"
      >
        {{ draft.error }} Delivery is unconfirmed. Check Chat before editing and sending a new message. An observed delivery is removed automatically.
      </p>
      <div class="queue-actions">
        <button
          type="button"
          :disabled="busy || Boolean(editingId) || Boolean(draft.error) || !ready || !hasContent(draft)"
          @click="emit('send', draft.id)"
        >
          {{ goalActive || turnInFlight ? 'Steer now' : 'Send now' }}
        </button>
        <template v-if="editingId === draft.id">
          <button
            type="button"
            :disabled="busy"
            @click="saveEdit(draft)"
          >
            Save
          </button>
          <button
            type="button"
            :disabled="busy"
            @click="finishEdit"
          >
            Discard edit
          </button>
        </template>
        <template v-else>
          <button
            type="button"
            :disabled="busy || Boolean(editingId)"
            @click="startEdit(draft)"
          >
            Edit
          </button>
          <button
            type="button"
            :disabled="busy || Boolean(editingId)"
            @click="emit('edit', draft.id)"
          >
            {{ draft.error ? 'Review in composer as new message' : 'Move to composer / add images' }}
          </button>
        </template>
        <button
          type="button"
          :disabled="busy"
          @click="emit('cancel', draft.id)"
        >
          Cancel
        </button>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import type { QueuedChatDraft, QueuedChatDraftAttachment } from '@/stores/terminalStore'
const props = defineProps<{ drafts: QueuedChatDraft[]; busy: boolean; ready: boolean; goalActive: boolean; turnInFlight: boolean; editingId: string | null; editingMessage: string }>()
const emit = defineEmits<{
  editing: [id: string | null, message: string]
  update: [id: string, message: string, attachments: QueuedChatDraftAttachment[]]
  edit: [id: string]
  cancel: [id: string]
  send: [id: string]
  preview: [attachment: QueuedChatDraftAttachment, event: MouseEvent]
}>()
function hasContent(draft: QueuedChatDraft) { return Boolean(draft.message.trim() || draft.attachments.length) }
const editingId = computed(() => props.editingId)
const editMessage = computed({ get: () => props.editingMessage, set: value => emit('editing', editingId.value, value) })
function startEdit(draft: QueuedChatDraft) {
  emit('editing', draft.id, draft.message)
}
function finishEdit() { emit('editing', null, '') }
function saveEdit(draft: QueuedChatDraft) {
  emit('update', draft.id, editMessage.value, draft.attachments)
  finishEdit()
}
watch(() => props.drafts.map(draft => draft.id), ids => {
  if (editingId.value && !ids.includes(editingId.value)) finishEdit()
})

</script>

<style scoped>
.draft-queue { display: grid; gap: 8px; max-height: 280px; overflow-y: auto; padding: 8px; border: 1px solid var(--ch-color-border); border-radius: 8px; }
.queue-heading { margin: 0; font-size: 12px; color: var(--ch-color-text-muted); }
.queued-draft { padding: 8px; border-radius: 6px; background: var(--ch-color-app-bg); }
summary::before { content: '▸'; }
details[open] summary::before { content: '▾'; }
summary { display: flex; gap: 8px; cursor: pointer; }
summary span { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
small { color: var(--ch-color-text-muted); }
.queue-message { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.queue-editor { display: grid; gap: 4px; margin: 8px 0; font-size: 12px; }
textarea { box-sizing: border-box; width: 100%; resize: vertical; padding: 8px; color: var(--ch-color-text); background: var(--ch-color-surface); border: 1px solid var(--ch-color-border); border-radius: 4px; font: inherit; }
.queue-actions, .queue-images { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
button { padding: 5px 9px; color: var(--ch-color-text); background: var(--ch-color-surface); border: 1px solid var(--ch-color-border); border-radius: 4px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
.queue-image { position: relative; }
.queue-image > button:first-child { padding: 0; display: flex; }
img { width: 72px; height: 64px; object-fit: contain; }
.remove-image { position: absolute; top: -5px; right: -5px; padding: 0; width: 24px; height: 24px; }
.queue-error { color: var(--ch-color-danger); font-size: 12px; }
button:focus-visible, summary:focus-visible, textarea:focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
@media (pointer: coarse) { .queue-actions button { min-height: 44px; } }
</style>
