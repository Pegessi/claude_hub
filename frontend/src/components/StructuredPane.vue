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
      <div class="structured-timeline-content">
        <div
          v-if="turns.length === 0 && connectionState === 'live'"
          class="structured-empty"
        >
          <span
            class="empty-orbit"
            aria-hidden="true"
          >✦</span>
          <strong>Ready when you are</strong>
          <p>Send a message below to start this agent conversation.</p>
        </div>

        <div
          v-for="turn in turns"
          :key="turn.key"
          class="structured-turn"
        >
          <!-- A right-aligned user bubble and a left-aligned agent bubble make
               this the same conversation as the terminal, not terminal text
               pasted into a second surface. -->
          <div
            v-if="turn.userText"
            class="conversation-row conversation-row--user"
          >
            <div class="conversation-bubble conversation-bubble--user">
              <MarkdownContent
                :text="turn.userText"
                compact
              />
            </div>
          </div>

          <details
            v-if="turn.thinkingText"
            class="thinking-card"
          >
            <summary>
              <span
                class="thinking-indicator"
                aria-hidden="true"
              />
              Thinking
            </summary>
            <MarkdownContent
              :text="turn.thinkingText"
              compact
              class="thinking-body"
            />
          </details>

          <div
            v-if="turn.assistantText"
            class="conversation-row conversation-row--assistant"
          >
            <span
              class="conversation-avatar"
              aria-hidden="true"
            >✦</span>
            <div class="conversation-bubble conversation-bubble--assistant">
              <MarkdownContent
                :text="turn.assistantText"
                compact
              />
            </div>
          </div>

          <div
            v-for="tool in turn.tools"
            :key="tool.callId || tool.key"
            class="conversation-row conversation-row--assistant"
          >
            <span
              class="conversation-avatar conversation-avatar--tool"
              aria-hidden="true"
            >⌘</span>
            <details class="tool-card">
              <summary class="tool-header">
                <span class="tool-name">{{ tool.name }}</span>
                <span
                  class="tool-status"
                  :class="tool.status"
                >{{ tool.status }}</span>
              </summary>
              <div
                v-if="tool.argsText"
                class="tool-block"
              >
                <span>Input</span>
                <pre>{{ tool.argsText }}</pre>
              </div>
              <div
                v-if="tool.resultText"
                class="tool-block"
              >
                <span>Result</span>
                <pre>{{ tool.resultText }}</pre>
              </div>
            </details>
          </div>

          <div
            v-for="err in turn.errors"
            :key="err.key"
            class="event-error"
            role="alert"
          >
            <span
              class="error-icon"
              aria-hidden="true"
            >⚠</span>
            <span>{{ err.message }}</span>
          </div>

          <div
            v-for="st in turn.statuses"
            :key="st.key"
            class="event-status"
          >
            <span>{{ st.text }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Composer -->
    <div class="structured-composer">
      <div class="composer-shell">
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
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { useAgentStream, validateImageAttachment, fileToDataUrl } from '@/composables/useAgentStream'
import { groupEventsIntoTurns } from '@/utils/agentStreamTimeline'
import MarkdownContent from '@/components/MarkdownContent.vue'
import type { WorkspaceAttachmentCreate } from '@/types'

const props = defineProps<{
  /** A Workspace-managed agent keeps the existing durable message path. */
  sessionId?: string
  /** A normal Terminal agent tab owns its transcript directly. */
  tabId?: string
}>()

const emit = defineEmits<{
  (e: 'fallback-to-raw'): void
}>()

const workspaceStore = useWorkspaceStore()

const { events, connectionState, errorMessage, start, stop } = useAgentStream()

const isTerminalTabSource = computed(() => Boolean(props.tabId))

function startStream() {
  if (props.tabId) {
    void start(props.tabId, 'terminal-tab')
  } else if (props.sessionId) {
    void start(props.sessionId, 'managed-session')
  }
}

// ── Timeline grouping ───────────────────────────────────────────────────────
// The flat event stream is grouped into turns by the pure
// ``groupEventsIntoTurns`` utility (see agentStreamTimeline.ts).

const turns = computed(() => groupEventsIntoTurns(events.value))

// ── Stream lifecycle ────────────────────────────────────────────────────────

onMounted(() => {
  startStream()
})

onUnmounted(() => {
  stop()
})

watch(
  () => [props.sessionId, props.tabId],
  startStream,
)

// Fail-closed: if the stream fails, tell the parent to switch back to raw.
watch(connectionState, (state) => {
  if (state === 'failed') {
    emit('fallback-to-raw')
  }
})

function retry() {
  startStream()
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

/**
 * Terminal-created tabs do not participate in the Workspace outbox.  Their
 * structured composer intentionally mirrors a user operating that very same
 * terminal: images go through the established macOS pasteboard bridge, then
 * input is queued through TerminalView so hidden raw panes retain ownership.
 */
async function sendToTerminalTab(
  message: string,
  atts: WorkspaceAttachmentCreate[],
) {
  const sendKey = window.__claudeHub.sendTerminalKey
  if (!sendKey) {
    throw new Error('Terminal input is not ready. Switch to Terminal once, then retry.')
  }

  for (const att of atts) {
    const image = await fetch(att.data_url)
    if (!image.ok) throw new Error(`Unable to read ${att.filename}`)
    const form = new FormData()
    form.append('image', await image.blob(), att.filename)
    const response = await fetch('/api/clipboard/image', {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
    })
    if (!response.ok) {
      throw new Error(`Unable to prepare ${att.filename}: ${response.status}`)
    }
    sendKey('v', true)
  }

  for (const char of Array.from(message)) {
    sendKey(char === '\n' ? 'Enter' : char)
  }
  sendKey('Enter')
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
    if (isTerminalTabSource.value) {
      await sendToTerminalTab(message, atts)
    } else if (props.sessionId) {
      await workspaceStore.sendMessage(props.sessionId, message, atts)
    } else {
      throw new Error('Structured source is unavailable.')
    }
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

/* Paseo conversation presentation ------------------------------------------------
   The initial implementation was intentionally semantic but visually read like
   terminal lines.  Keep the same stream model while giving its two peers a
   deliberate, readable conversation surface. */
.structured-pane {
  background:
    radial-gradient(circle at 50% -22%, var(--ch-color-surface-soft), transparent 44%),
    var(--ch-color-app-bg);
}

.structured-timeline {
  padding: 34px 28px 26px;
}

.structured-timeline-content {
  width: 100%;
  max-width: 860px;
  min-height: 100%;
  margin: 0 auto;
}

.structured-empty {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 24px;
}

.structured-empty strong {
  color: var(--ch-color-text-muted);
  font-weight: 500;
}

.structured-empty p {
  max-width: 340px;
  margin: 0;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  line-height: 1.5;
}

.empty-orbit {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  color: var(--ch-color-accent);
  box-shadow: 0 8px 20px var(--ch-shadow-color-soft);
}

.structured-turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 22px;
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}

.conversation-row--user {
  justify-content: flex-end;
}

.conversation-bubble {
  max-width: min(85%, 680px);
  min-width: 0;
  padding: 9px 12px;
  border-radius: var(--ch-radius-md);
  overflow-wrap: anywhere;
}

.conversation-bubble--user {
  --paseo-user-bubble: #3268a8;

  background: var(--paseo-user-bubble);
  color: #fff;
  border-bottom-right-radius: var(--ch-radius-sm);
}

.conversation-bubble--user :deep(.markdown-content),
.conversation-bubble--user :deep(.markdown-content :where(h1, h2, h3, h4, a, blockquote)) {
  color: inherit;
}

.conversation-bubble--assistant {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-bottom-left-radius: var(--ch-radius-sm);
  box-shadow: 0 6px 18px var(--ch-shadow-color-soft);
}

.conversation-avatar {
  width: 23px;
  height: 23px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 600;
}

.conversation-avatar--tool {
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
}

.thinking-card {
  align-self: flex-start;
  width: min(85%, 680px);
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  padding: 7px 10px;
}

.thinking-card summary,
.tool-card summary {
  cursor: pointer;
  list-style: none;
}

.thinking-card summary::-webkit-details-marker,
.tool-card summary::-webkit-details-marker {
  display: none;
}

.thinking-card summary {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  font-weight: 600;
}

.thinking-card summary:focus-visible,
.tool-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.thinking-indicator {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.thinking-body {
  margin-top: 8px;
  color: var(--ch-color-text-muted);
}

.tool-card {
  width: min(85%, 680px);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  overflow: hidden;
}

.tool-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
}

.tool-name {
  min-width: 0;
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}

.tool-status {
  flex: 0 0 auto;
  padding: 2px 8px;
}

.tool-block {
  padding: 0 10px 10px;
}

.tool-block > span {
  display: block;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.tool-block pre {
  max-height: 220px;
  margin: 5px 0 0;
  padding: 8px;
  overflow: auto;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.event-error {
  align-self: flex-start;
  width: min(85%, 680px);
  margin: 0;
}

.event-status {
  justify-content: center;
  font-style: italic;
  text-align: center;
}

.structured-composer {
  padding: 12px 28px;
  background: color-mix(in srgb, var(--ch-color-surface) 94%, transparent);
}

.composer-shell {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
}

.composer-row {
  padding: 5px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: calc(var(--ch-radius-md) + 2px);
  background: var(--ch-color-app-bg);
  box-shadow: 0 8px 24px var(--ch-shadow-color-soft);
}

.composer-textarea {
  min-height: 34px;
  padding: 7px 6px;
  border: 0;
  background: transparent;
}

.composer-textarea:focus-visible {
  border-color: transparent;
  box-shadow: none;
}

.composer-attach-btn {
  border-color: transparent;
}

.composer-send-btn {
  height: 34px;
  border-radius: var(--ch-radius-sm);
}

/* Drag-over highlight */
.structured-pane.is-dragging .structured-timeline {
  outline: 2px dashed var(--ch-color-accent);
  outline-offset: -4px;
}

/* Narrow viewport: tighten composer and timeline padding */
@media (max-width: 640px) {
  .structured-timeline {
    padding: 18px 12px;
  }

  .structured-composer {
    padding: 8px 12px;
  }

  .composer-send-btn {
    padding: 0 10px;
  }

  .attachment-chip {
    width: 40px;
    height: 40px;
  }

  .conversation-bubble,
  .thinking-card,
  .tool-card,
  .event-error {
    width: min(92%, 680px);
    max-width: 92%;
  }
}
</style>
