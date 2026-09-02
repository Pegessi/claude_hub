<template>
  <div
    class="structured-pane"
    :class="{ 'is-dragging': isDragOver }"
    @dragover.prevent="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <!-- Chat sessions fail closed on this surface. A stream failure never
         mounts a hidden raw terminal; users can retry or create a Terminal. -->
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
        <span class="banner-guidance">Create a Terminal session for native TUI access.</span>
      </template>
    </div>

    <!-- Timeline -->
    <div
      ref="timelineEl"
      class="structured-timeline"
      :class="{ 'is-timeline-hidden': timelinePhase !== 'revealed' }"
      role="log"
      aria-live="polite"
      aria-label="Chat conversation"
      @scroll.passive="handleTimelineScroll"
    >
      <div
        ref="timelineContentEl"
        class="structured-timeline-content"
      >
        <div
          v-if="turns.length === 0 && pendingDirectTurns.length === 0 && connectionState === 'live'"
          class="structured-empty"
        >
          <span
            class="empty-orbit"
            aria-hidden="true"
          >✦</span>
          <strong>Ready when you are</strong>
          <p>Send a message below to start this chat.</p>
        </div>

        <div
          v-for="turn in turns"
          :key="turn.key"
          v-memo="[turn.renderRevision, erroredAttachments.size]"
          class="structured-turn"
        >
          <!-- A right-aligned user bubble and a left-aligned assistant bubble make
               this the same conversation as the terminal, not terminal text
               pasted into a second surface. -->
          <div
            v-if="turn.userText || turn.attachments?.length"
            class="conversation-row conversation-row--user"
          >
            <div class="conversation-bubble conversation-bubble--user">
              <MarkdownContent
                v-if="turn.userText"
                :text="turn.userText"
                compact
              />
              <div
                v-if="turn.attachments?.length"
                class="turn-attachments"
              >
                <template
                  v-for="(att, i) in turn.attachments"
                  :key="att.id ?? `null-${turn.key}-${i}`"
                >
                  <!-- Keep conversation density high: render a bounded
                       thumbnail and open the full preview in a lightbox. -->
                  <button
                    v-if="att.id !== null && !erroredAttachments.has(att.id)"
                    type="button"
                    class="turn-attachment-button"
                    aria-label="Open attached image preview"
                    @click="openImageLightbox(attachmentUrl(att.id), 'attached image', $event)"
                  >
                    <img
                      :src="attachmentUrl(att.id)"
                      class="turn-attachment-img"
                      alt="attached image"
                      @error="onAttachmentError($event, att)"
                    >
                  </button>
                  <!-- Placeholder for no-preview (id is null) or evicted
                       preview (fetch returned 404/410). -->
                  <div
                    v-else
                    class="turn-attachment-placeholder"
                  >
                    <span>{{ att.id === null ? 'Preview unavailable' : 'Preview expired' }}</span>
                  </div>
                </template>
              </div>
            </div>
          </div>

          <div
            v-if="turn.awaitingAgentActivity"
            class="conversation-row conversation-row--assistant"
            role="status"
            aria-live="polite"
          >
            <span
              class="conversation-avatar conversation-avatar--waiting"
              aria-hidden="true"
            >✦</span>
            <div class="agent-waiting-card">
              <span
                class="agent-waiting-pulse"
                aria-hidden="true"
              >
                <i />
                <i />
                <i />
              </span>
              <span>Waiting for response…</span>
            </div>
          </div>

          <!-- Ordered parts: thinking, text, tool, error, and status render in
               the exact order the provider emitted them. Paseo does not defer
               protocol errors to the turn end, so neither do we. -->
          <template
            v-for="part in turn.parts"
            :key="part.key"
          >
            <details
              v-if="part.kind === 'thinking'"
              class="thinking-card"
            >
              <summary>
                <span
                  class="thinking-indicator"
                  aria-hidden="true"
                />
                Thinking
              </summary>
              <!-- Thinking is rendered as plain text (no marked/DOMPurify) to
                   avoid re-parsing multi-kilobyte reasoning streams on every
                   delta. Whitespace is preserved with pre-wrap. -->
              <pre class="thinking-body">{{ part.text }}</pre>
            </details>

            <div
              v-else-if="part.kind === 'text'"
              class="conversation-row conversation-row--assistant"
            >
              <span
                class="conversation-avatar"
                aria-hidden="true"
              >✦</span>
              <div class="conversation-bubble conversation-bubble--assistant">
                <MarkdownContent
                  :text="part.text"
                  compact
                  :complete="turn.completed"
                />
              </div>
            </div>

            <div
              v-else-if="part.kind === 'tool'"
              class="conversation-row conversation-row--assistant"
            >
              <span
                class="conversation-avatar conversation-avatar--tool"
                aria-hidden="true"
              >⌘</span>
              <details class="tool-card">
                <summary class="tool-header">
                  <span class="tool-name">{{ part.tool.name }}</span>
                  <span
                    class="tool-status"
                    :class="part.tool.status"
                  >{{ part.tool.status }}</span>
                </summary>
                <div
                  v-if="part.tool.argsText"
                  class="tool-block"
                >
                  <span>Input</span>
                  <pre>{{ part.tool.argsText }}</pre>
                </div>
                <div
                  v-if="part.tool.resultText"
                  class="tool-block"
                >
                  <span>Result</span>
                  <pre>{{ part.tool.resultText }}</pre>
                </div>
              </details>
            </div>

            <div
              v-else-if="part.kind === 'error'"
              class="event-error"
              role="alert"
            >
              <span
                class="error-icon"
                aria-hidden="true"
              >⚠</span>
              <span>{{ part.message }}</span>
            </div>

            <div
              v-else-if="part.kind === 'status'"
              class="event-status"
            >
              <span>{{ part.text }}</span>
            </div>
          </template>
        </div>

        <!-- Optimistic turns are reconciled by client_turn_id, never by text. -->
        <div
          v-for="turn in pendingTurns"
          :key="turn.key"
          class="structured-turn structured-turn--pending"
        >
          <div class="conversation-row conversation-row--user">
            <div class="conversation-bubble conversation-bubble--user">
              <MarkdownContent
                v-if="turn.userText"
                :text="turn.userText"
                compact
              />
              <div
                v-if="turn.attachments?.length"
                class="turn-attachments"
              >
                <button
                  v-for="(att, i) in turn.attachments"
                  :key="i"
                  type="button"
                  class="turn-attachment-button"
                  aria-label="Open attached image preview"
                  @click="openImageLightbox(att.preview_url, 'attached image', $event)"
                >
                  <img
                    :src="att.preview_url"
                    class="turn-attachment-img"
                    alt="attached image"
                  >
                </button>
              </div>
            </div>
          </div>
          <div class="event-status event-status--pending">
            <span>Waiting for model activity…</span>
          </div>
        </div>
      </div>
    </div>

    <button
      v-if="!isFollowingLatest"
      type="button"
      class="structured-jump-latest"
      aria-label="Scroll to latest message"
      @click="jumpToLatest"
    >
      <span aria-hidden="true">↓</span>
      Latest
    </button>

    <!-- Composer -->
    <div class="structured-composer">
      <div class="composer-shell">
        <div
          v-if="modeChangeError"
          class="composer-mode-error"
          role="alert"
        >
          {{ modeChangeError }}
        </div>

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
          <div class="composer-tools">
            <button
              type="button"
              class="composer-attach-btn"
              aria-label="Attach image"
              :title="supportsImages ? 'Attach image' : 'This chat does not support image attachments'"
              :disabled="!supportsImages || isSending || isPreparingAttachments"
              @click="triggerFilePicker"
            >
              <span aria-hidden="true">📎</span>
            </button>
            <input
              ref="fileInputEl"
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              class="composer-file-input"
              @change="handleFilePick"
            >
            <div
              v-if="modeOptions.length > 0"
              ref="modePickerEl"
              class="composer-mode-picker"
            >
              <button
                ref="modeTriggerEl"
                type="button"
                class="composer-mode-trigger"
                aria-haspopup="menu"
                :aria-expanded="isModeMenuOpen"
                :aria-label="`Chat mode: ${currentModeLabel}`"
                :title="currentModeOption?.description || `${currentModeLabel} mode`"
                :disabled="modeInteractionLocked || isUpdatingMode"
                @click="toggleModeMenu"
              >
                <span class="composer-mode-trigger-label">{{ currentModeLabel }}</span>
                <span
                  class="composer-mode-chevron"
                  aria-hidden="true"
                >▴</span>
              </button>
              <div
                v-if="isModeMenuOpen"
                ref="modeMenuEl"
                class="composer-mode-menu"
                role="menu"
                aria-label="Chat mode"
              >
                <button
                  v-for="option in modeOptions"
                  :key="option.id"
                  type="button"
                  class="composer-mode-menu-item"
                  role="menuitemradio"
                  :aria-checked="currentModeId === option.id"
                  :title="option.description || `${option.label} mode`"
                  @click="selectMode(option.id)"
                >
                  <span>{{ option.label }}</span>
                  <span
                    v-if="currentModeId === option.id"
                    class="composer-mode-check"
                    aria-hidden="true"
                  >✓</span>
                </button>
              </div>
            </div>
          </div>
          <textarea
            v-model="draftMessage"
            class="composer-textarea"
            placeholder="Send a message…"
            rows="1"
            :disabled="isSending || connectionState !== 'live'"
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

    <Teleport to="body">
      <div
        v-if="imageLightboxUrl"
        class="structured-image-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="Image preview"
        @click.self="closeImageLightbox"
      >
        <button
          ref="lightboxCloseEl"
          type="button"
          class="structured-image-lightbox-close"
          aria-label="Close image preview"
          @click="closeImageLightbox"
        >
          ×
        </button>
        <img
          class="structured-image-lightbox-img"
          :src="imageLightboxUrl"
          :alt="imageLightboxAlt"
          @click.stop
          @error="closeImageLightbox"
        >
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useAgentStream, validateImageAttachment, fileToDataUrl, generatePreviewDataUrl } from '@/composables/useAgentStream'
import { IncrementalTimelineReducer, type TimelineAttachment } from '@/utils/agentStreamTimeline'
import { isTimelineNearBottom } from '@/utils/timelineFollow'
import { createTimelineActivation, type TimelinePhase } from '@/utils/timelineActivation'
import { getAvailableChatModes, getCurrentChatModeId } from '@/utils/chatModePolicy'
import { hasChatStatusRefreshBoundary, isChatModeLocked } from '@/utils/chatTurnLifecycle'
import { useTerminalStore } from '@/stores/terminalStore'
import MarkdownContent from '@/components/MarkdownContent.vue'
import type { WorkspaceAttachmentCreate } from '@/types'

const props = defineProps<{
  /** A top-level Chat tab owns its transcript directly. */
  tabId: string
}>()

const terminalStore = useTerminalStore()

const {
  events,
  connectionState,
  errorMessage,
  capabilities,
  start,
  retry: retryStream,
  setMode,
  stop,
} = useAgentStream()

function startStream() {
  void start(props.tabId, 'terminal-tab')
}

// ── Timeline grouping ───────────────────────────────────────────────────────
// The flat event stream is grouped into turns by the
// ``IncrementalTimelineReducer`` (see agentStreamTimeline.ts).
//
// ``groupEventsIntoTurns`` re-scans the entire event list on every call. For
// a session with thousands of historical events, each incoming delta re-runs
// the full O(n) reduction and dominates the long-task budget. The incremental
// reducer keeps state across calls and only processes the unseen suffix, so
// each batch costs O(new events) regardless of history length.

const timelineReducer = new IncrementalTimelineReducer()
const authoritativeTurns = computed(() => timelineReducer.reduce(events.value))

// Assistant text streams directly from the batched event stream (backend
// 60ms coalescer + frontend rAF/48ms batcher). No second-stage character
// reveal: each committed batch updates the visible text once. On turn
// completion MarkdownContent caches the final block and exposes the exact
// final text synchronously.
const turns = computed(() => authoritativeTurns.value.map(turn => ({
  ...turn,
  awaitingAgentActivity: !turn.completed &&
    turn.parts.length === 0 &&
    turn.errors.length === 0 &&
    turn.statuses.length === 0,
})))

type PendingTurn = {
  key: string
  turnId: string
  userText: string
  attachments: { preview_url: string; mime_type: string }[]
}

const pendingDirectTurns = ref<PendingTurn[]>([])
const isUpdatingMode = ref(false)
const modeChangeError = ref<string | null>(null)
const isModeMenuOpen = ref(false)
const modeOptions = computed(() => getAvailableChatModes(capabilities.value))
const currentModeId = computed(() => getCurrentChatModeId(capabilities.value))
const currentModeOption = computed(() => modeOptions.value.find(option => option.id === currentModeId.value))
const currentModeLabel = computed(() => currentModeOption.value?.label ?? 'Mode')

const pendingTurns = computed(() => {
  const observedTurnIds = new Set(authoritativeTurns.value.map(turn => turn.turnId).filter(Boolean))
  return pendingDirectTurns.value.filter(turn => !observedTurnIds.has(turn.turnId))
})

// Reconcile optimistic (pending) turns against authoritative turns as they
// arrive. No text-reveal state is kept: assistant text is rendered directly
// from the batched event stream.
watch(
  authoritativeTurns,
  (latest) => {
    const observed = new Set(latest.map(turn => turn.turnId).filter(Boolean))
    pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => !observed.has(turn.turnId))
  },
  { immediate: true },
)

// Chat lifecycle edges are authoritative status boundaries. Refresh the tab
// status exactly once when a committed batch introduces turn_started,
// turn_completed, or error; text/thinking deltas never trigger this watcher.
watch(events, (latest, previous) => {
  if (hasChatStatusRefreshBoundary(previous, latest)) {
    void terminalStore.fetchAgentStatuses()
  }
})

// ── Stream lifecycle ────────────────────────────────────────────────────────

onMounted(() => {
  startStream()
  document.addEventListener('keydown', handleDocumentKeydown)
  document.addEventListener('pointerdown', handleModeOutsidePointer)
})

watch(
  () => props.tabId,
  () => {
    // Advance the preparation epoch so any in-flight attachment batch from
    // the previous source fails its post-await epoch check and aborts
    // instead of appending into the new source's composer.
    preparationEpoch.value++
    // A new source means a fresh composer: the previous source's
    // preparation (if any) is no longer relevant, so re-enable Send.
    isPreparingAttachments.value = false
    pendingDirectTurns.value = []
    draftMessage.value = ''
    attachments.value = []
    composerError.value = null
    isUpdatingMode.value = false
    modeChangeError.value = null
    isModeMenuOpen.value = false
    dismissImageLightbox(false)
    startStream()
  },
)

onUnmounted(() => {
  // Bump the epoch on unmount so any in-flight preparation batch aborts
  // instead of mutating state after the component is gone.
  preparationEpoch.value++
  document.removeEventListener('keydown', handleDocumentKeydown)
  document.removeEventListener('pointerdown', handleModeOutsidePointer)
  dismissImageLightbox(false)
  stop()
})

function retry() {
  void retryStream(props.tabId, 'terminal-tab')
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
const isPreparingAttachments = ref(false)
const modeInteractionLocked = computed(() => isSending.value || isChatModeLocked(
  pendingDirectTurns.value.length > 0,
  authoritativeTurns.value,
))
/** Monotonically increasing epoch that advances on every source switch
 *  (session/tab change) and on unmount. Captured at the start of an
 *  attachment preparation batch and re-checked after every await; an exact
 *  match is required so a stale batch from a previous source visit cannot
 *  append into a later visit of the same source (the ABA problem: switch
 *  A→B→A while FileReader/canvas awaits, and the stale A batch would
 *  otherwise pass a source-string equality check). */
const preparationEpoch = ref(0)
const isDragOver = ref(false)
const fileInputEl = ref<HTMLInputElement | null>(null)
const timelineEl = ref<HTMLElement | null>(null)
const timelineContentEl = ref<HTMLElement | null>(null)
const modePickerEl = ref<HTMLElement | null>(null)
const modeTriggerEl = ref<HTMLButtonElement | null>(null)
const modeMenuEl = ref<HTMLElement | null>(null)
/** Attachment ids whose preview fetch returned 404/410 (evicted or never
 *  cached). Rendered as a visible "Preview expired" placeholder. */
const erroredAttachments = ref<Set<string>>(new Set())
const imageLightboxUrl = ref<string | null>(null)
const imageLightboxAlt = ref('')
const lightboxCloseEl = ref<HTMLButtonElement | null>(null)
let imageLightboxTrigger: HTMLElement | null = null

function openImageLightbox(url: string, alt: string, event: MouseEvent) {
  imageLightboxUrl.value = url
  imageLightboxAlt.value = alt
  imageLightboxTrigger = event.currentTarget instanceof HTMLElement
    ? event.currentTarget
    : null
  void nextTick(() => lightboxCloseEl.value?.focus())
}

function dismissImageLightbox(restoreFocus: boolean) {
  if (!imageLightboxUrl.value) return
  const trigger = imageLightboxTrigger
  imageLightboxUrl.value = null
  imageLightboxAlt.value = ''
  imageLightboxTrigger = null
  if (restoreFocus) void nextTick(() => trigger?.focus())
}

function closeImageLightbox() {
  dismissImageLightbox(true)
}

function handleDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && isModeMenuOpen.value) {
    event.preventDefault()
    closeModeMenu(true)
    return
  }
  if (event.key === 'Escape' && imageLightboxUrl.value) {
    event.preventDefault()
    closeImageLightbox()
  }
}

function handleModeOutsidePointer(event: PointerEvent) {
  if (!isModeMenuOpen.value || modePickerEl.value?.contains(event.target as Node)) return
  closeModeMenu(false)
}

// Activation gate: the timeline is not revealed until authoritative history
// has been hydrated and the tail has been synchronously pinned. This prevents
// the visible "scroll replay" where history paints at the top and then jumps
// to the bottom. See timelineActivation.ts for the state machine contract.
const activation = createTimelineActivation()
const timelinePhase = ref<TimelinePhase>(activation.phase)
const isFollowingLatest = ref(activation.followOutput)

function syncActivation() {
  timelinePhase.value = activation.phase
  isFollowingLatest.value = activation.followOutput
}

function markHistoryReady() {
  activation.markHistoryReady()
  syncActivation()
}

function confirmTailPinned() {
  activation.confirmTailPinned()
  syncActivation()
}

function detachFromTail() {
  activation.detachFromTail()
  syncActivation()
}

function rearmFollow() {
  activation.rearmFollow()
  syncActivation()
}

function resetActivation() {
  activation.reset()
  syncActivation()
}

let timelineResizeObserver: ResizeObserver | null = null
let timelineVerificationFrame: number | null = null
let timelineDisposed = false

const canSend = computed(() => connectionState.value === 'live' &&
  !isPreparingAttachments.value &&
  !isUpdatingMode.value &&
  (draftMessage.value.trim().length > 0 || attachments.value.length > 0))

const supportsImages = computed(() => capabilities.value?.supports_images ?? false)

function closeModeMenu(restoreFocus: boolean) {
  if (!isModeMenuOpen.value) return
  isModeMenuOpen.value = false
  if (restoreFocus) void nextTick(() => modeTriggerEl.value?.focus())
}

function toggleModeMenu() {
  if (modeInteractionLocked.value || isUpdatingMode.value) return
  if (isModeMenuOpen.value) {
    closeModeMenu(false)
    return
  }
  isModeMenuOpen.value = true
  void nextTick(() => {
    const currentItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[aria-checked="true"]')
    const firstItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[role="menuitemradio"]')
    const focusTarget = currentItem ?? firstItem
    focusTarget?.focus()
  })
}

async function selectMode(modeId: string) {
  closeModeMenu(true)
  await changeMode(modeId)
}

async function changeMode(modeId: string) {
  if (modeInteractionLocked.value || isUpdatingMode.value || currentModeId.value === modeId) return
  const epoch = preparationEpoch.value
  isUpdatingMode.value = true
  modeChangeError.value = null
  try {
    await setMode(modeId)
  } catch (err) {
    if (preparationEpoch.value !== epoch) return
    modeChangeError.value = err instanceof Error ? err.message : 'Failed to update Chat mode.'
  } finally {
    if (preparationEpoch.value === epoch) isUpdatingMode.value = false
  }
}

watch([modeInteractionLocked, isUpdatingMode], ([locked, updating]) => {
  if (locked || updating) closeModeMenu(false)
})

function triggerFilePicker() {
  if (isSending.value) return
  fileInputEl.value?.click()
}

const MAX_ATTACHMENTS = 10
// Backend enforces a 40 MiB total decoded cap per send request (originals +
// previews). We enforce it conservatively client-side: sum of original file
// sizes plus 512 KiB reserved per selected preview must stay <= 40 MiB. The
// backend remains authoritative.
const MAX_TOTAL_REQUEST_BYTES = 40 * 1024 * 1024
const PREVIEW_RESERVED_BYTES = 512 * 1024

async function addFiles(files: FileList | File[]) {
  // Capture the epoch BEFORE any state mutation. If the source switches
  // (or the component unmounts) during this batch, the epoch advances and
  // every post-await check fails, so this stale batch never touches the
  // new source's composer state.
  const epoch = preparationEpoch.value

  composerError.value = null
  if (!supportsImages.value) {
    composerError.value = 'This chat does not support image attachments.'
    return
  }
  // Serialize preparation batches: reject new input while a previous batch is
  // still reading/generating previews. Without this, two concurrent batches
  // both set isPreparingAttachments=true; the first to finish clears it and
  // re-enables Send while the second is still mid-await.
  if (isPreparingAttachments.value) return
  // Block new attachments while a send is in flight: an async continuation
  // must never append into a composer that has already been cleared and sent.
  if (isSending.value) return

  const list = Array.from(files)
  // Enforce the max-attachment count client-side.
  const remaining = MAX_ATTACHMENTS - attachments.value.length
  if (remaining <= 0) {
    composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
    return
  }
  const accepted = list.slice(0, remaining)
  if (accepted.length < list.length) {
    composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
  }

  // Enforce the total request byte cap before reading: sum of existing draft
  // originals + new originals + 512 KiB per preview must stay <= 40 MiB.
  const existingBytes = attachments.value.reduce((sum, a) => sum + (a.size_bytes || 0), 0)
  const newBytes = accepted.reduce((sum, f) => sum + f.size, 0)
  const totalPreviews = attachments.value.length + accepted.length
  const projected = existingBytes + newBytes + totalPreviews * PREVIEW_RESERVED_BYTES
  if (projected > MAX_TOTAL_REQUEST_BYTES) {
    const mb = (MAX_TOTAL_REQUEST_BYTES / 1024 / 1024).toFixed(0)
    composerError.value = `Total attachment size exceeds the ${mb} MiB request limit.`
    return
  }

  // Validate all accepted files before starting any async work.
  for (const file of accepted) {
    const err = validateImageAttachment(file)
    if (err) {
      composerError.value = err
      return
    }
  }

  // Mark the entire preparation batch active BEFORE the first await so Send
  // stays disabled for the whole read+preview-generation window.
  isPreparingAttachments.value = true

  try {
    for (const file of accepted) {
      // Read the original once. The provisional thumbnail uses the original
      // data URL so it appears instantly; the bounded preview is generated
      // from the same data URL (no second read).
      let dataUrl: string
      try {
        dataUrl = await fileToDataUrl(file)
      } catch (e) {
        // Only surface the error if this batch still owns the composer.
        if (preparationEpoch.value === epoch) {
          composerError.value = e instanceof Error
            ? `Failed to read ${file.name}: ${e.message}`
            : `Failed to read ${file.name}.`
        }
        continue
      }
      // Stale batch (source switched or send started): bail out WITHOUT
      // clearing isPreparingAttachments — the new source's batch may own it.
      if (preparationEpoch.value !== epoch || isSending.value) return

      const provisionalId = `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      const provisional: DraftAttachment = {
        id: provisionalId,
        filename: file.name,
        mime_type: file.type,
        data_url: dataUrl,
        // Provisional thumbnail uses the original data URL.
        preview_url: dataUrl,
        size_bytes: file.size,
      }
      attachments.value.push(provisional)

      try {
        // Pass the already-read data URL so we do not read the file twice.
        const previewDataUrl = await generatePreviewDataUrl(file, dataUrl)
        // Stale batch: bail out without clearing the flag.
        if (preparationEpoch.value !== epoch || isSending.value) return
        const idx = attachments.value.findIndex((a) => a.id === provisionalId)
        if (idx < 0) continue // removed by the user while preparing
        attachments.value[idx] = {
          ...provisional,
          preview_data_url: previewDataUrl,
          // Replace the provisional thumbnail with the bounded preview.
          preview_url: previewDataUrl,
        }
      } catch (e) {
        // Preview generation failed. Only mutate state if this batch still
        // owns the composer; otherwise leave the new source's state alone.
        if (preparationEpoch.value !== epoch) return
        attachments.value = attachments.value.filter((a) => a.id !== provisionalId)
        composerError.value = e instanceof Error
          ? `Failed to prepare ${file.name}: ${e.message}`
          : `Failed to prepare ${file.name}.`
      }
    }
  } finally {
    // Only the owning batch may clear the preparing flag. A stale batch
    // (epoch advanced) must not unlock a newer batch that set the flag.
    if (preparationEpoch.value === epoch) {
      isPreparingAttachments.value = false
    }
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
  if (isSending.value) return
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
  if (isSending.value) return
  const files = event.dataTransfer?.files
  if (files && files.length > 0) {
    void addFiles(files)
  }
}

function removeAttachment(att: DraftAttachment) {
  if (isSending.value) return
  const idx = attachments.value.findIndex((a) => a.id === att.id)
  if (idx >= 0) attachments.value.splice(idx, 1)
}

/**
 * Build the scoped preview URL for an authoritative attachment id.
 *
 * The endpoint is session/tab-scoped so a leaked id from another session
 * cannot be used to fetch previews. The backend validates the id against the
 * session/tab manifest before serving.
 */
function attachmentUrl(attachmentId: string): string {
  const encId = encodeURIComponent(attachmentId)
  return `/api/workspaces/tabs/${encodeURIComponent(props.tabId)}/stream/attachments/${encId}`
}

/**
 * Record an attachment whose preview fetch failed (404/410 or network error).
 *
 * The template renders a visible "Preview expired" placeholder for ids in
 * ``erroredAttachments`` instead of a broken image icon. The set is keyed by
 * attachment id; a re-render of the turn (e.g. on reconnect) does not clear
 * it, so the placeholder stays stable.
 */
function onAttachmentError(_event: Event, att: TimelineAttachment): void {
  if (att.id === null) return
  erroredAttachments.value = new Set(erroredAttachments.value).add(att.id)
}

/**
 * Deliver composer input to the native provider transport via ``/stream/send``.
 *
 * StructuredPane is mounted only for top-level Chat tabs. Text and images are
 * staged and submitted together through the native transport's atomic
 * send_message boundary.
 */
async function sendToStream(
  message: string,
  atts: WorkspaceAttachmentCreate[],
  clientTurnId: string,
) {
  const base = `/api/workspaces/tabs/${props.tabId}/stream/send`
  const res = await fetch(base, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      client_turn_id: clientTurnId,
      text: message,
      attachments: atts.map(({ filename, mime_type, data_url, preview_data_url }) => ({
        filename,
        mime_type,
        data_url,
        preview_data_url,
      })),
    }),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail)
  }
}

async function submit() {
  if (!canSend.value || isSending.value) return
  isSending.value = true
  composerError.value = null
  const message = draftMessage.value
  // Snapshot the full draft attachments (including id, preview_url,
  // preview_data_url, size_bytes) so we can restore the composer exactly on
  // send failure — no reconstructed ids, no zeroed sizes.
  const draftAtts: DraftAttachment[] = [...attachments.value]
  // Wire payload carries the original data_url (for the provider) and the
  // bounded preview_data_url (for the cache) separately.
  const atts: WorkspaceAttachmentCreate[] = draftAtts.map(
    ({ filename, mime_type, data_url, preview_data_url }) => ({
      filename,
      mime_type,
      data_url,
      preview_data_url,
    }),
  )
  // Optimistic bubble thumbnails come from the bounded preview.
  const pendingAtts = draftAtts.map(({ preview_url, mime_type }) => ({
    preview_url,
    mime_type,
  }))
  const clientTurnId = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
  try {
    // Clear the composer synchronously BEFORE the send promise settles so the
    // thumbnail disappears immediately on Send. On rejection we restore the
    // exact snapshot below.
    draftMessage.value = ''
    attachments.value = []
    // Show the user's turn immediately; the stream will replace it with the
    // authoritative transcript line once the provider echoes it back.
    pendingDirectTurns.value = [
      ...pendingDirectTurns.value,
      {
        key: `pending-${clientTurnId}`,
        turnId: clientTurnId,
        userText: message,
        attachments: pendingAtts,
      },
    ]
    requestLatestAnchor(true)
    await sendToStream(message, atts, clientTurnId)
    // The POST acknowledgement means provider dispatch has begun. Refresh the
    // backend-native tab status now rather than waiting for the 5s poll phase;
    // turn_started/completed/error boundaries above provide subsequent edges.
    void terminalStore.fetchAgentStatuses()
    // Success: composer already cleared; nothing more to do.
  } catch (err) {
    pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => turn.turnId !== clientTurnId)
    // Restore the exact draft text and attachments so the user can retry
    // without losing their input.
    draftMessage.value = message
    attachments.value = draftAtts
    composerError.value = err instanceof Error ? err.message : 'Failed to send message.'
  } finally {
    isSending.value = false
  }
}

function cancelScheduledTimelineScroll() {
  if (timelineVerificationFrame !== null) {
    cancelAnimationFrame(timelineVerificationFrame)
    timelineVerificationFrame = null
  }
}

/**
 * Pin the timeline to the latest turn.
 *
 * The initial reveal is handled by the activation gate (``markHistoryReady``
 * → synchronous ``scrollTop = scrollHeight`` in ``nextTick`` →
 * ``confirmTailPinned``). This function only drives live updates once the
 * timeline is revealed and the user has not detached.
 *
 * The scroll is applied synchronously inside ``nextTick`` (after Vue's DOM
 * commit, before the browser paints) so a growing assistant message never
 * paints above the fold and then jumps down. A single verification rAF
 * re-sticks if Markdown / image / font layout changed the scroll height.
 */
function requestLatestAnchor(force = false) {
  if (force) rearmFollow()
  if (!isFollowingLatest.value || timelineDisposed) return
  // While hidden or pinning, the activation gate owns the scroll position.
  if (timelinePhase.value !== 'revealed') return
  cancelScheduledTimelineScroll()
  void nextTick(() => {
    if (timelineDisposed) return
    const el = timelineEl.value
    if (!el || !isFollowingLatest.value || timelineDisposed) return
    el.scrollTop = el.scrollHeight
    timelineVerificationFrame = requestAnimationFrame(() => {
      timelineVerificationFrame = null
      const current = timelineEl.value
      if (!current || !isFollowingLatest.value || timelineDisposed) return
      if (!isTimelineNearBottom(current)) current.scrollTop = current.scrollHeight
    })
  })
}

function handleTimelineScroll() {
  const el = timelineEl.value
  if (!el) return
  // Scroll events during hydration are not user-driven and must not detach.
  if (timelinePhase.value !== 'revealed') return
  if (isTimelineNearBottom(el)) {
    if (!isFollowingLatest.value) rearmFollow()
  } else {
    if (isFollowingLatest.value) detachFromTail()
  }
}

function jumpToLatest() {
  rearmFollow()
  requestLatestAnchor(true)
}

function observeTimelineGeometry() {
  timelineResizeObserver?.disconnect()
  if (typeof ResizeObserver === 'undefined') return
  const viewport = timelineEl.value
  const content = timelineContentEl.value
  if (!viewport || !content) return
  timelineResizeObserver = new ResizeObserver(() => {
    // Resize only re-sticks while the user is following the tail. A detached
    // viewport must never be hijacked by a layout change.
    if (activation.shouldHandleResize()) requestLatestAnchor()
  })
  timelineResizeObserver.observe(viewport)
  timelineResizeObserver.observe(content)
}

watch(
  () => [events.value.length, pendingTurns.value.length],
  () => requestLatestAnchor(),
)

// Activation gate: when the stream goes live, authoritative history is in the
// DOM. Pin the tail synchronously (after Vue's DOM commit, before paint) and
// only then reveal the timeline. This is the Paseo ``isAuthoritativeHistoryReady``
// pattern — the first painted frame is already at the tail.
//
// ``immediate`` covers the cached-stream case: if the composable is already
// ``live`` when this pane mounts (quick tab switch-back), we still run the
// pin-and-reveal sequence instead of leaving the timeline permanently hidden.
watch(connectionState, (state) => {
  if (state === 'live') {
    markHistoryReady()
    void nextTick(() => {
      if (timelineDisposed) return
      const el = timelineEl.value
      if (el) el.scrollTop = el.scrollHeight
      confirmTailPinned()
    })
  } else if (state === 'hydrating') {
    resetActivation()
  }
  // 'failed' and 'idle' leave the timeline hidden; the banner (Retry) is
  // shown because ``connectionState !== 'live'``. Retry re-enters
  // 'hydrating' and the gate runs again.
}, { immediate: true })

watch(
  () => props.tabId,
  () => {
    resetActivation()
    // Attachment ids are scoped to a session/tab; switching source invalidates
    // all previously-recorded 404/410 error state.
    erroredAttachments.value = new Set()
    requestLatestAnchor(true)
  },
)

onMounted(() => {
  timelineDisposed = false
  void nextTick(() => {
    if (timelineDisposed) return
    observeTimelineGeometry()
  })
})

onUnmounted(() => {
  timelineDisposed = true
  timelineResizeObserver?.disconnect()
  timelineResizeObserver = null
  cancelScheduledTimelineScroll()
})
</script>

<style scoped>
.structured-pane {
  position: relative;
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

.banner-guidance {
  margin-left: auto;
  color: var(--ch-color-text-subtle);
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

/* Activation gate: while authoritative history is being hydrated, the timeline
   content is hidden and its scroll container is clipped. The first painted
   frame after reveal is already pinned to the tail, so the user never sees
   history paint at the top and then jump down. */
.structured-timeline.is-timeline-hidden {
  visibility: hidden;
  overflow: hidden;
}

.structured-jump-latest {
  position: absolute;
  right: 28px;
  bottom: 92px;
  z-index: 3;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-md, 0 8px 24px rgb(0 0 0 / 24%));
  cursor: pointer;
}

.structured-jump-latest:hover {
  border-color: var(--ch-color-accent);
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

.structured-turn--pending {
  opacity: 0.82;
}

.pending-attachment {
  display: block;
  margin-top: 7px;
  padding-top: 7px;
  border-top: 1px solid color-mix(in srgb, currentColor 24%, transparent);
  font-size: 11px;
  font-weight: 600;
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

.event-status--pending {
  color: var(--ch-color-text-muted);
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

.composer-tools {
  display: inline-flex;
  align-items: flex-end;
  gap: 3px;
  flex: 0 0 auto;
}

.composer-mode-picker {
  position: relative;
  flex: 0 0 auto;
}

.composer-mode-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: auto;
  max-width: 120px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.composer-mode-trigger:hover:not(:disabled),
.composer-mode-trigger[aria-expanded='true'] {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-trigger-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-mode-chevron {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-size: 9px;
  line-height: 1;
}

.composer-mode-trigger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-mode-trigger:focus-visible,
.composer-mode-menu-item:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-mode-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 7px);
  z-index: 8;
  width: max-content;
  min-width: max(132px, 100%);
  max-width: min(240px, calc(100vw - 32px));
  padding: 4px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-md, 0 10px 30px rgb(0 0 0 / 26%));
}

.composer-mode-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  min-height: 34px;
  padding: 6px 9px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.composer-mode-menu-item:hover,
.composer-mode-menu-item:focus-visible {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-menu-item[aria-checked='true'] {
  color: var(--ch-color-accent);
}

.composer-mode-check {
  flex: 0 0 auto;
  font-size: 11px;
}

.composer-mode-error {
  margin: 0 0 7px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 11px;
  line-height: 1.35;
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

.composer-attach-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
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
  min-width: 0;
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

.turn-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.turn-attachment-button {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: block;
  padding: 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  box-shadow: 0 2px 8px rgb(0 0 0 / 18%);
  cursor: zoom-in;
}

.turn-attachment-button:hover {
  border-color: color-mix(in srgb, #fff 70%, transparent);
}

.turn-attachment-button:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}

.turn-attachment-img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  transition: transform 140ms ease;
}

.turn-attachment-button:hover .turn-attachment-img {
  transform: scale(1.025);
}

.turn-attachment-placeholder {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: grid;
  place-items: center;
  padding: 10px;
  border: 1px dashed color-mix(in srgb, #fff 36%, transparent);
  border-radius: var(--ch-radius-sm);
  color: color-mix(in srgb, #fff 78%, transparent);
  font-size: 11px;
  text-align: center;
}

.structured-image-lightbox {
  position: fixed;
  inset: 0;
  z-index: 1600;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgb(0 0 0 / 78%);
  backdrop-filter: blur(4px);
}

.structured-image-lightbox-img {
  max-width: min(1200px, calc(100vw - 48px));
  max-height: calc(100dvh - 48px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
  box-shadow: 0 18px 64px rgb(0 0 0 / 52%);
  cursor: zoom-out;
}

.structured-image-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid rgb(255 255 255 / 28%);
  border-radius: 50%;
  background: rgb(24 24 24 / 88%);
  color: #fff;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}

.structured-image-lightbox-close:hover,
.structured-image-lightbox-close:focus-visible {
  border-color: rgb(255 255 255 / 72%);
  background: rgb(42 42 42 / 96%);
  outline: none;
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

.conversation-avatar--waiting {
  color: var(--ch-color-text-subtle);
  background: var(--ch-color-surface-control);
}

.agent-waiting-card {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 7px 11px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  border-bottom-left-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.agent-waiting-pulse {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-waiting-pulse i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentcolor;
  animation: agent-waiting-dot 1.2s ease-in-out infinite;
}

.agent-waiting-pulse i:nth-child(2) {
  animation-delay: 0.15s;
}

.agent-waiting-pulse i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes agent-waiting-dot {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }

  30% {
    opacity: 1;
    transform: translateY(-2px);
  }
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
  margin: 8px 0 0;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
  word-break: break-word;
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

  .composer-row {
    gap: 4px;
  }

  .composer-tools {
    gap: 2px;
  }

  .composer-attach-btn,
  .composer-mode-trigger {
    min-height: 44px;
    height: 44px;
  }

  .composer-attach-btn {
    width: 44px;
  }

  .composer-mode-trigger {
    max-width: 104px;
    padding: 0 7px;
  }

  .composer-mode-menu {
    max-width: min(220px, calc(100vw - 24px));
  }

  .composer-mode-menu-item {
    min-height: 44px;
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

@media (prefers-reduced-motion: reduce) {
  .agent-waiting-pulse i,
  .banner-spinner,
  .thinking-indicator {
    animation: none;
  }

  .turn-attachment-img {
    transition: none;
  }
}
</style>
