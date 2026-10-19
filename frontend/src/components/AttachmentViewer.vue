<!-- eslint-disable vue/no-v-html -->
<template>
  <section class="attachment-viewer">
    <header class="attachment-header">
      <div class="attachment-title">
        <span class="attachment-label">{{ isImage ? 'Image' : file?.kind || 'Attachment' }} preview</span>
        <h1
          ref="title"
          tabindex="-1"
        >
          {{ filename }}
        </h1>
        <p class="attachment-path">
          {{ file?.path || target.path || target.name }}
        </p>
      </div>
      <button
        v-if="closable"
        type="button"
        class="ch-btn attachment-close"
        aria-label="Close preview"
        autofocus
        @click="$emit('close')"
      >
        ×
      </button>
      <nav
        class="attachment-actions"
        aria-label="Attachment actions"
      >
        <button
          v-if="canGoBack"
          type="button"
          class="ch-btn"
          @click="$emit('back')"
        >
          Back
        </button>
        <template v-if="isImage">
          <span v-if="dimensions">{{ dimensions }}</span>
          <button
            type="button"
            class="ch-btn"
            :aria-pressed="actualSize"
            @click="actualSize = !actualSize"
          >
            {{ actualSize ? 'Fit image' : 'Actual size' }}
          </button>
          <a
            :href="imageUrl"
            class="ch-btn"
            target="_blank"
            rel="noopener noreferrer"
          >Open original</a>
        </template>
        <template v-else>
          <button
            v-if="file?.kind === 'markdown'"
            type="button"
            class="ch-btn"
            :aria-pressed="showSource"
            @click="showSource = !showSource"
          >
            {{ showSource ? 'Show preview' : 'Show source' }}
          </button>
          <button
            v-if="file && (file.kind !== 'markdown' || showSource)"
            type="button"
            class="ch-btn"
            :aria-pressed="wrap"
            @click="wrap = !wrap"
          >
            Wrap lines
          </button>
          <button
            v-if="file"
            type="button"
            class="ch-btn"
            @click="copyText"
          >
            {{ file.truncated ? 'Copy shown text' : 'Copy' }}
          </button>
          <button
            v-if="kind !== 'unsupported'"
            type="button"
            class="ch-btn"
            :disabled="loading"
            @click="load"
          >
            Refresh
          </button>
          <a
            v-if="separateUrl && closable"
            :href="separateUrl"
            class="ch-btn"
            target="_blank"
            rel="noopener noreferrer"
          >Open separately</a>
        </template>
        <slot name="actions" />
      </nav>
      <span
        v-if="copyStatus"
        class="attachment-copy-status"
        role="status"
      >{{ copyStatus }}</span>
    </header>
    <div
      ref="body"
      class="attachment-body"
      :class="{ 'structured-image-lightbox--actual': actualSize }"
    >
      <p
        v-if="loading"
        class="attachment-notice"
        role="status"
      >
        Loading attachment…
      </p>
      <section
        v-else-if="error"
        class="attachment-error"
        role="alert"
      >
        <h2>Unable to preview this file</h2>
        <p>{{ error }}</p>
        <a
          v-if="needsLogin"
          class="ch-btn"
          href="/api/auth/login"
          target="_blank"
          rel="noopener noreferrer"
        >Sign in to Hub</a>
        <button
          v-if="kind !== 'unsupported'"
          type="button"
          class="ch-btn"
          @click="load"
        >
          Retry
        </button>
      </section>
      <img
        v-else-if="isImage"
        :key="imageUrl + imageAttempt"
        class="structured-image-lightbox-img"
        :src="imageUrl"
        :alt="filename"
        @load="imageLoaded"
        @error="error = 'The image is unavailable. It may have expired, moved, or been deleted.'"
      >
      <template v-else-if="file">
        <p
          v-if="file.truncated"
          class="attachment-notice"
          role="status"
        >
          Only the first 512 KiB is shown. Copy includes only this preview, not the complete file.
        </p>
        <p
          v-if="!file.content"
          class="attachment-notice"
        >
          This file is empty.
        </p>
        <article
          v-if="file.kind === 'markdown'"
          v-show="!showSource"
          class="attachment-article"
        >
          <MarkdownContent
            :text="file.content"
            :tab-id="target.scope"
            :document-path="file.path"
            complete
          />
        </article>
        <pre
          v-if="file.kind !== 'markdown' || showSource"
          class="attachment-source"
          :class="{ 'attachment-source--wrap': wrap }"
          tabindex="0"
          aria-label="File source"
        ><code v-html="sourceHtml" /></pre>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import DOMPurify from 'dompurify'
import MarkdownContent from '@/components/MarkdownContent.vue'
import type { AttachmentTarget } from '@/composables/useAttachmentPreview'
import { attachmentKind, attachmentPreviewUrl, markdownStreamBase } from '@/utils/markdownPreview'
import { ensureHighlighter, highlightCode, highlightReady } from '@/utils/codeHighlight'
import { writeClipboard } from '@/utils/clipboard'

interface AttachmentDocument {
  path: string
  filename: string
  content: string
  size_bytes: number
  truncated: boolean
  kind: 'markdown' | 'code' | 'data' | 'text'
  language: string
}
const props = withDefaults(defineProps<{ target: AttachmentTarget; canGoBack?: boolean; closable?: boolean }>(), {
  canGoBack: false, closable: false,
})
defineEmits<{ close: []; back: [] }>()
const body = ref<HTMLElement | null>(null)
const title = ref<HTMLElement | null>(null)
const file = ref<AttachmentDocument | null>(null)
const loading = ref(false)
const error = ref('')
const needsLogin = ref(false)
const showSource = ref(false)
const wrap = ref(false)
const copyStatus = ref('')
const actualSize = ref(false)
const dimensions = ref('')
const imageAttempt = ref(0)
const kind = computed(() => props.target.imageUrl ? 'image' : attachmentKind(props.target.path))
const isImage = computed(() => kind.value === 'image')
const filename = computed(() => props.target.name || (file.value?.path || props.target.path).split('/').pop() || 'Attachment')
const imageUrl = computed(() => props.target.imageUrl || `${markdownStreamBase(props.target.scope)}/agent-image?${new URLSearchParams({ path: props.target.path })}`)
const separateUrl = computed(() => attachmentPreviewUrl(props.target.scope, `${encodeURIComponent(props.target.path)}${props.target.hash || ''}`))
// Large inputs stay plain text; highlighter work must not freeze the mobile UI.
const sourceHtml = computed(() => {
  void highlightReady.value
  const content = file.value?.content || ''
  return DOMPurify.sanitize(highlightCode(content, content.length <= 65536 ? file.value?.language || 'plaintext' : 'plaintext'), {
    ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'],
  })
})
let request: AbortController | null = null
let copyEpoch = 0
interface ViewState { top: number; left: number; source: boolean; wrap: boolean; actualSize: boolean }
const viewStates = new WeakMap<AttachmentTarget, ViewState>()
let pendingScroll: ViewState | null = null

function restoreScroll() {
  if (!body.value || !pendingScroll) return false
  body.value.scrollTo({ top: pendingScroll.top, left: pendingScroll.left })
  pendingScroll = null
  return true
}

async function copyText() {
  const epoch = ++copyEpoch
  try {
    await writeClipboard(file.value?.content || '')
    if (epoch === copyEpoch) copyStatus.value = 'Copied'
  } catch {
    if (epoch === copyEpoch) copyStatus.value = 'Copy failed. Select the text to copy it manually.'
  }
}
function imageLoaded(event: Event) {
  const image = event.target as HTMLImageElement
  dimensions.value = `${image.naturalWidth} × ${image.naturalHeight}`
  void nextTick(restoreScroll)
}
async function load() {
  request?.abort()
  const current = new AbortController()
  request = current
  file.value = null
  error.value = ''
  needsLogin.value = false
  copyStatus.value = ''
  copyEpoch++
  loading.value = false
  if (isImage.value) { imageAttempt.value++; return }
  if (kind.value === 'unsupported' || !kind.value) {
    error.value = 'This file type has no built-in preview. Open it with a suitable application; Hub will not execute it.'
    return
  }
  const base = markdownStreamBase(props.target.scope)
  if (!base || !props.target.path) {
    error.value = 'This preview link is incomplete. Open the file from its Chat message again.'
    return
  }
  loading.value = true
  try {
    const response = await fetch(`${base}/attachment?${new URLSearchParams({ path: props.target.path })}`, {
      credentials: 'include', cache: 'no-store', signal: current.signal,
    })
    if (request !== current || current.signal.aborted) return
    if (!response.ok) {
      needsLogin.value = response.status === 401
      const messages: Record<number, string> = {
        401: 'Sign in to Hub, then retry this preview.',
        403: 'This file is outside the Chat’s allowed file locations.',
        404: 'The file or its Chat is unavailable, or this location or file type is not allowed.',
        415: 'This file is not UTF-8 text. Binary files cannot be shown as source.',
        422: 'This file path is invalid.',
      }
      error.value = messages[response.status] || 'The file could not be loaded. Please retry.'
      return
    }
    const result: AttachmentDocument = await response.json()
    if (request !== current || current.signal.aborted) return
    file.value = result
    if (result.content.length <= 65536 && result.kind !== 'markdown') void ensureHighlighter().catch(() => {})
  } catch {
    if (request === current && !current.signal.aborted) error.value = 'The file could not be loaded. Check your connection and retry.'
  } finally {
    if (request === current) loading.value = false
  }
}
watch(() => props.target, (target, previous) => {
  if (previous && body.value) {
    viewStates.set(previous, {
      top: body.value.scrollTop, left: body.value.scrollLeft,
      source: showSource.value, wrap: wrap.value, actualSize: actualSize.value,
    })
  }
  pendingScroll = viewStates.get(target) || null
  showSource.value = pendingScroll?.source || false
  wrap.value = pendingScroll?.wrap || false
  actualSize.value = pendingScroll?.actualSize || false
  dimensions.value = ''
  if (body.value) { body.value.scrollTop = 0; body.value.scrollLeft = 0 }
  void load()
  // Nested navigation can remove the focused link or Back button. Keep keyboard
  // focus on the new file's title instead of letting it fall into the Chat.
  if (previous) void nextTick(() => {
    if (props.target === target) title.value?.focus({ preventScroll: true })
  })
}, { immediate: true })
watch([file, showSource, loading], async () => {
  const target = props.target
  await nextTick()
  if (target !== props.target || loading.value || !file.value || !body.value) return
  if (restoreScroll() || showSource.value || !target.hash) return
  let id: string
  try { id = decodeURIComponent(target.hash.slice(1)) } catch { return }
  Array.from(body.value.querySelectorAll('[id]')).find(element => element.id === id)?.scrollIntoView()
})
onUnmounted(() => { request?.abort(); copyEpoch++ })
</script>

<style scoped>
.attachment-viewer { display: flex; flex-direction: column; min-width: 0; height: 100%; color: var(--ch-color-text); background: var(--ch-color-main-bg); }
.attachment-header { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 12px; padding: 20px 24px 16px; border-bottom: 1px solid var(--ch-color-border); flex: none; }
.attachment-title { flex: 1; min-width: 0; }
.attachment-label { font-size: 12px; color: var(--ch-color-text-muted); text-transform: capitalize; }
.attachment-title h1 { font-size: 20px; margin: 4px 0; line-height: 1.3; overflow-wrap: anywhere; }
.attachment-path { margin: 0; font: 12px/1.5 var(--ch-font-mono); overflow-wrap: anywhere; max-height: 3em; overflow: auto; }
.attachment-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; width: 100%; font-size: 12px; }
.attachment-viewer .ch-btn { min-height: 44px; height: auto; padding: 8px 12px; text-decoration: none; }
.attachment-viewer .attachment-close { width: 44px; flex: none; font-size: 24px; padding: 0; }
.attachment-viewer :is(button, a, pre):focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
.attachment-copy-status { font-size: 12px; }
.attachment-body { padding: 24px; flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.attachment-notice { margin: 0 0 16px; font-size: 14px; line-height: 1.6; color: var(--ch-color-text-muted); }
.attachment-error { max-width: 560px; padding: 20px; border: 1px solid var(--ch-color-border); border-radius: 12px; }
.attachment-error h2 { font-size: 18px; }
.attachment-error p { margin: 12px 0; line-height: 1.6; }
.attachment-error .ch-btn { margin: 4px; }
.attachment-article { max-width: 960px; margin: auto; overflow-wrap: anywhere; }
.attachment-article :deep(.markdown-content) { font-size: 15px; line-height: 1.7; }
.attachment-source { margin: 0; font: 13px/1.65 var(--ch-font-mono); tab-size: 4; white-space: pre; width: max-content; min-width: 100%; }
.attachment-source--wrap { white-space: pre-wrap; overflow-wrap: anywhere; width: 100%; }
.attachment-source :deep(.hljs-keyword), .attachment-source :deep(.hljs-built_in) { color: var(--ch-code-keyword); }
.attachment-source :deep(.hljs-string) { color: var(--ch-code-string); }
.attachment-source :deep(.hljs-number), .attachment-source :deep(.hljs-literal) { color: var(--ch-code-number); }
.attachment-source :deep(.hljs-comment) { color: var(--ch-code-comment); }
.attachment-source :deep(.hljs-title) { color: var(--ch-code-function); }
.attachment-source :deep(.hljs-attr) { color: var(--ch-code-attr); }
.structured-image-lightbox-img { display: block; margin: auto; max-width: 100%; max-height: 100%; object-fit: contain; }
.structured-image-lightbox--actual .structured-image-lightbox-img { max-width: none; max-height: none; }
@media (max-width: 600px) {
  .attachment-header { padding: max(12px, env(safe-area-inset-top)) 12px 12px; gap: 8px; }
  .attachment-body { padding: 16px 12px max(16px, env(safe-area-inset-bottom)); }
  .attachment-title h1 { font-size: 18px; }
  .attachment-actions { gap: 6px; }
}
</style>
