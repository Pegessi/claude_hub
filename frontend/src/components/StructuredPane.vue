<template>
  <div
    class="structured-pane"
    :class="{ 'is-dragging': isDragOver }"
    @dragover.prevent="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <!-- Connection / hydration state banner.
         role=status (polite) for hydrating; role=alert (assertive) for failed.
         Fail-closed: when failed, the parent TerminalPane switches back to raw. -->
    <div
      v-if="connectionState !== 'live'"
      class="structured-banner"
      :role="connectionState === 'failed' ? 'alert' : 'status'"
      :aria-live="connectionState === 'failed' ? 'assertive' : 'polite'"
    >
      <template v-if="connectionState === 'hydrating'">
        <span
          class="banner-spinner"
          aria-hidden="true"
        />
        <span>Loading structured view…</span>
      </template>
      <template v-else-if="connectionState === 'failed'">
        <span
          class="banner-icon"
          aria-hidden="true"
        >⚠</span>
        <span>{{ errorMessage || 'Structured view unavailable.' }}</span>
        <button
          type="button"
          class="banner-retry"
          @click="retry"
        >
          Retry
        </button>
      </template>
    </div>

    <!-- Timeline -->
    <div
      ref="timelineEl"
      class="structured-timeline"
      role="log"
      aria-live="polite"
      aria-label="Agent conversation"
    >
      <div
        v-if="turns.length === 0 && connectionState === 'live'"
        class="structured-empty"
      >
        <p>No messages yet.</p>
        <p class="empty-hint">
          Send a message below to start.
        </p>
      </div>

      <div
        v-for="turn in turns"
        :key="turn.key"
        class="structured-turn"
      >
        <!-- User message -->
        <div
          v-if="turn.userText"
          class="event event-user"
        >
          <div class="event-role">
            You
          </div>
          <div class="event-body">
            {{ turn.userText }}
          </div>
        </div>

        <!-- Assistant thinking -->
        <div
          v-if="turn.thinkingText"
          class="event event-thinking"
        >
          <details>
            <summary>Thinking</summary>
            <div class="event-body thinking-body">
              {{ turn.thinkingText }}
            </div>
          </details>
        </div>

        <!-- Assistant text -->
        <div
          v-if="turn.assistantText"
          class="event event-assistant"
        >
          <div class="event-role">
            Assistant
          </div>
          <div class="event-body">
            {{ turn.assistantText }}
          </div>
        </div>

        <!-- Tool calls -->
        <div
          v-for="tool in turn.tools"
          :key="tool.callId || tool.key"
          class="event event-tool"
        >
          <div class="tool-header">
            <span class="tool-name">{{ tool.name }}</span>
            <span
              class="tool-status"
              :class="tool.status"
            >{{ tool.status }}</span>
          </div>
          <div
            v-if="tool.argsText"
            class="tool-args"
          >
            {{ tool.argsText }}
          </div>
          <div
            v-if="tool.resultText"
            class="tool-result"
          >
            {{ tool.resultText }}
          </div>
        </div>

        <!-- Errors -->
        <div
          v-for="err in turn.errors"
          :key="err.key"
          class="event event-error"
          role="alert"
        >
          <span
            class="error-icon"
            aria-hidden="true"
          >⚠</span>
          <span class="event-body">{{ err.message }}</span>
        </div>

        <!-- Status events -->
        <div
          v-for="st in turn.statuses"
          :key="st.key"
          class="event event-status"
        >
          <span
            class="status-dot"
            aria-hidden="true"
          />
          <span class="event-body">{{ st.text }}</span>
        </div>
      </div>
    </div>

    <!-- Composer -->
    <div class="structured-composer">
      <!-- Attachment previews -->
      <div
        v-if="attachments.length > 0"
        class="composer-attachments"
      >
        <div
          v-for="att in attachments"
          :key="att.id"
          class="attachment-chip"
        >
          <img
            :src="att.preview_url"
            :alt="att.filename"
            class="attachment-thumb"
          >
          <button
            type="button"
            class="attachment-remove"
            :aria-label="`Remove ${att.filename}`"
            @click="removeAttachment(att)"
          >
            ×
          </button>
        </div>
      </div>

      <!-- Validation error -->
      <div
        v-if="composerError"
        class="composer-error"
        role="alert"
      >
        {{ composerError }}
      </div>

      <div class="composer-row">
        <button
          type="button"
          class="composer-attach-btn"
          aria-label="Attach image"
          title="Attach image"
          @click="triggerFilePicker"
        >
          <span aria-hidden="true">📎</span>
        </button>
        <input
          ref="fileInputEl"
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
          multiple
          class="composer-file-input"
          @change="handleFilePick"
        >
        <textarea
          v-model="draftMessage"
          class="composer-textarea"
          placeholder="Send a message…"
          rows="1"
          :disabled="isSending"
          @keydown.enter.exact.prevent="submit"
          @paste="handlePaste"
        />
        <button
          type="button"
          class="composer-send-btn"
          :disabled="!canSend || isSending"
          @click="submit"
        >
          {{ isSending ? 'Sending…' : 'Send' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { useAgentStream, validateImageAttachment, fileToDataUrl } from '@/composables/useAgentStream'
import { groupEventsIntoTurns } from '@/utils/agentStreamTimeline'
import type { WorkspaceAttachmentCreate } from '@/types'

const props = defineProps<{
  sessionId: string
}>()

const emit = defineEmits<{
  (e: 'fallback-to-raw'): void
}>()

const workspaceStore = useWorkspaceStore()

const { events, connectionState, errorMessage, start, stop } = useAgentStream()

// ── Timeline grouping ───────────────────────────────────────────────────────
// The flat event stream is grouped into turns by the pure
// ``groupEventsIntoTurns`` utility (see agentStreamTimeline.ts).

const turns = computed(() => groupEventsIntoTurns(events.value))

// ── Stream lifecycle ────────────────────────────────────────────────────────

onMounted(() => {
  void start(props.sessionId)
})

onUnmounted(() => {
  stop()
})

watch(
  () => props.sessionId,
  (id) => {
    void start(id)
  },
)

// Fail-closed: if the stream fails, tell the parent to switch back to raw.
watch(connectionState, (state) => {
  if (state === 'failed') {
    emit('fallback-to-raw')
  }
})

function retry() {
  void start(props.sessionId)
}

// ── Composer ────────────────────────────────────────────────────────────────

interface DraftAttachment extends WorkspaceAttachmentCreate {
  id: string
  preview_url: string
  size_bytes: number
}

const draftMessage = ref('')
const attachments = ref<DraftAttachment[]>([])
const composerError = ref<string | null>(null)
const isSending = ref(false)
const isDragOver = ref(false)
const fileInputEl = ref<HTMLInputElement | null>(null)
const timelineEl = ref<HTMLElement | null>(null)

const canSend = computed(() => draftMessage.value.trim().length > 0 || attachments.value.length > 0)

function triggerFilePicker() {
  fileInputEl.value?.click()
}

async function addFiles(files: FileList | File[]) {
  composerError.value = null
  const list = Array.from(files)
  for (const file of list) {
    const err = validateImageAttachment(file)
    if (err) {
      composerError.value = err
      continue
    }
    const dataUrl = await fileToDataUrl(file)
    attachments.value.push({
      id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      filename: file.name,
      mime_type: file.type,
      data_url: dataUrl,
      preview_url: dataUrl,
      size_bytes: file.size,
    })
  }
}

function handleFilePick(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    void addFiles(input.files)
  }
  input.value = ''
}

function handlePaste(event: ClipboardEvent) {
  const items = event.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const item of items) {
    if (item.kind === 'file') {
      const f = item.getAsFile()
      if (f) files.push(f)
    }
  }
  if (files.length > 0) {
    event.preventDefault()
    void addFiles(files)
  }
}

function handleDragOver(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
  isDragOver.value = true
}

function handleDragLeave() {
  isDragOver.value = false
}

function handleDrop(event: DragEvent) {
  event.preventDefault()
  isDragOver.value = false
  const files = event.dataTransfer?.files
  if (files && files.length > 0) {
    void addFiles(files)
  }
}

function removeAttachment(att: DraftAttachment) {
  const idx = attachments.value.findIndex((a) => a.id === att.id)
  if (idx >= 0) attachments.value.splice(idx, 1)
}

async function submit() {
  if (!canSend.value || isSending.value) return
  isSending.value = true
  composerError.value = null
  const message = draftMessage.value
  const atts: WorkspaceAttachmentCreate[] = attachments.value.map(({ filename, mime_type, data_url }) => ({
    filename,
    mime_type,
    data_url,
  }))
  try {
    await workspaceStore.sendMessage(props.sessionId, message, atts)
    // Success: clear the composer.
    draftMessage.value = ''
    attachments.value = []
  } catch (err) {
    // Retain message + attachments on error so the user can retry.
    composerError.value = err instanceof Error ? err.message : 'Failed to send message.'
  } finally {
    isSending.value = false
  }
}

// Auto-scroll the timeline to the bottom when new events arrive.
watch(
  () => events.value.length,
  () => {
    nextTick(() => {
      const el = timelineEl.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)
</script>

<style scoped>
.structured-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background-color: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  font-size: 13px;
  line-height: 1.5;
}

.structured-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 12px;
  color: var(--ch-color-text-muted);
  flex-shrink: 0;
}

.banner-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--ch-color-border);
  border-top-color: var(--ch-color-accent);
  border-radius: 50%;
  animation: structured-spin 0.8s linear infinite;
}

@keyframes structured-spin {
  to {
    transform: rotate(360deg);
  }
}

.banner-icon {
  color: var(--ch-color-warning, #e0a800);
}

.banner-retry {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 12px;
  color: var(--ch-color-accent);
  background: transparent;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
}

.banner-retry:hover {
  background-color: var(--ch-color-surface-control-hover);
}

.structured-timeline {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  min-height: 0;
}

.structured-empty {
  text-align: center;
  color: var(--ch-color-text-subtle);
  padding: 24px 12px;
}

.empty-hint {
  font-size: 12px;
  opacity: 0.7;
}

.structured-turn {
  margin-bottom: 16px;
}

.event {
  margin-bottom: 8px;
}

.event-role {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--ch-color-text-subtle);
  margin-bottom: 2px;
}

.event-body {
  white-space: pre-wrap;
  word-break: break-word;
}

.event-user .event-body {
  background-color: var(--ch-color-accent-soft);
  border-radius: var(--ch-radius-md);
  padding: 8px 10px;
}

.event-assistant .event-body {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 8px 10px;
}

.event-thinking details {
  background-color: var(--ch-color-surface);
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
}

.event-thinking summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
  outline: none;
}

.event-thinking summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
  border-radius: var(--ch-radius-sm);
}

.thinking-body {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
}

.event-tool {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
  font-size: 12px;
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tool-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}

.tool-status {
  font-size: 10px;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.tool-status.completed {
  background-color: var(--ch-color-success-bg, rgba(46, 160, 67, 0.12));
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.failed {
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.running {
  background-color: var(--ch-color-warning-bg, rgba(224, 168, 0, 0.12));
  color: var(--ch-color-warning, #e0a800);
}

.tool-args,
.tool-result {
  margin-top: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
  word-break: break-all;
}

.event-error {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.1));
  border: 1px solid var(--ch-color-danger-border, rgba(248, 81, 73, 0.3));
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 12px;
}

.error-icon {
  flex-shrink: 0;
}

.event-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--ch-color-text-subtle);
  flex-shrink: 0;
}

/* Composer */
.structured-composer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  flex-shrink: 0;
}

.composer-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.attachment-chip {
  position: relative;
  width: 48px;
  height: 48px;
  border-radius: var(--ch-radius-sm);
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
}

.attachment-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.attachment-remove {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background-color: var(--ch-color-danger-strong, #f85149);
  color: #fff;
  border: none;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.composer-error {
  font-size: 12px;
  color: var(--ch-color-danger-strong, #f85149);
  margin-bottom: 6px;
}

.composer-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.composer-attach-btn {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  color: var(--ch-color-text-muted);
  flex-shrink: 0;
}

.composer-attach-btn:hover {
  background-color: var(--ch-color-surface-control-hover);
}

.composer-attach-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-file-input {
  display: none;
}

.composer-textarea {
  flex: 1;
  min-height: 32px;
  max-height: 120px;
  resize: none;
  padding: 7px 10px;
  font-size: 13px;
  line-height: 1.4;
  color: var(--ch-color-text);
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  outline: none;
  font-family: inherit;
}

.composer-textarea:focus-visible {
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.composer-textarea:disabled {
  opacity: 0.6;
}

.composer-send-btn {
  height: 32px;
  padding: 0 14px;
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  background-color: var(--ch-color-accent);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.composer-send-btn:hover:not(:disabled) {
  background-color: var(--ch-color-accent-hover);
}

.composer-send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-send-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

/* Drag-over highlight */
.structured-pane.is-dragging .structured-timeline {
  outline: 2px dashed var(--ch-color-accent);
  outline-offset: -4px;
}

/* Narrow viewport: tighten composer and timeline padding */
@media (max-width: 640px) {
  .structured-timeline {
    padding: 8px;
  }

  .structured-composer {
    padding: 6px 8px;
  }

  .composer-send-btn {
    padding: 0 10px;
  }

  .attachment-chip {
    width: 40px;
    height: 40px;
  }
}
</style>
