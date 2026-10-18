<template>
  <main class="markdown-reader">
    <header class="markdown-reader-header">
      <div class="markdown-reader-title">
        <span class="markdown-reader-label">Markdown preview</span>
        <h1>{{ filename }}</h1>
        <p class="markdown-reader-path">
          {{ document?.path || path }}
        </p>
      </div>
      <nav aria-label="Document actions">
        <a
          :href="chatUrl"
          class="ch-btn ch-btn--ghost markdown-reader-back"
        >
          <span aria-hidden="true">←</span> Back to Hub
        </a>
        <button
          v-if="document"
          type="button"
          class="ch-btn markdown-reader-view-toggle"
          :aria-pressed="showSource"
          @click="showSource = !showSource"
        >
          {{ showSource ? 'Show preview' : 'Show source' }}
        </button>
        <button
          type="button"
          class="ch-btn ch-btn--ghost markdown-reader-refresh"
          :disabled="loading"
          @click="loadDocument"
        >
          Refresh
        </button>
      </nav>
    </header>
    <p
      v-if="loading"
      class="markdown-reader-loading"
      role="status"
    >
      Loading Markdown…
    </p>
    <section
      v-else-if="error"
      class="markdown-reader-error"
      role="alert"
    >
      <h2>Unable to preview this document</h2>
      <p>{{ error }}</p>
      <div class="markdown-reader-error-actions">
        <a
          v-if="needsLogin"
          class="ch-btn ch-btn--ghost"
          href="/api/auth/login"
          target="_blank"
          rel="noopener noreferrer"
        >Sign in to Hub</a>
        <button
          type="button"
          class="ch-btn"
          @click="loadDocument"
        >
          Retry
        </button>
      </div>
    </section>
    <template v-else-if="document">
      <p
        v-if="document.truncated"
        class="markdown-reader-notice"
        role="status"
      >
        This document is too large to show in full. Only the first part is displayed.
      </p>
      <pre
        v-if="showSource"
        class="markdown-reader-source"
        tabindex="0"
      >{{ document.content }}</pre>
      <!-- Preserve parsed blocks and DOM when switching back from source. -->
      <article
        v-show="!showSource"
        ref="article"
        class="markdown-reader-article"
      >
        <MarkdownContent
          :text="document.content"
          :tab-id="scope"
          :document-path="document.path"
          complete
          @image-click="openImage"
        />
        <p
          v-if="!document.content"
          class="markdown-reader-notice"
        >
          This document is empty.
        </p>
      </article>
    </template>
  </main>
</template>

<script setup lang="ts">
import '@/features/app-shell/styles/theme.css'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { markdownStreamBase } from '@/utils/markdownPreview'
import { useAppStore } from '@/stores/appStore'

interface MarkdownDocument {
  path: string
  content: string
  size_bytes: number
  truncated: boolean
}

const query = new URLSearchParams(window.location.search)
const scope = query.get('scope') || ''
const path = query.get('markdown') || ''
const document = ref<MarkdownDocument | null>(null)
const article = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const needsLogin = ref(false)
const showSource = ref(false)
const filename = computed(() => (document.value?.path || path).split('/').pop() || 'Markdown document')
const chatUrl = scope.startsWith('/api/') ? '/' : `/?tab=${encodeURIComponent(scope)}`
watch(filename, (name) => { window.document.title = `${name} · Claude Hub` }, { immediate: true })
const appStore = useAppStore()
let request: AbortController | null = null

watch(() => appStore.colorScheme, (theme) => {
  window.document.documentElement.dataset.theme = theme
}, { immediate: true })

function openImage(url: string) {
  window.open(url, '_blank', 'noopener,noreferrer')
}

async function loadDocument() {
  request?.abort()
  const current = new AbortController()
  request = current
  loading.value = true
  error.value = ''
  needsLogin.value = false
  document.value = null
  const base = markdownStreamBase(scope)
  if (!base || !path) {
    error.value = 'This preview link is incomplete. Open the document from its Chat message again.'
    loading.value = false
    return
  }
  try {
    const response = await fetch(`${base}/markdown?${new URLSearchParams({ path })}`, {
      credentials: 'include', cache: 'no-store', signal: current.signal,
    })
    if (!response.ok) {
      needsLogin.value = response.status === 401
      const messages: Record<number, string> = {
        400: 'Only local Markdown documents can be previewed.',
        401: 'Sign in to Hub, then retry this preview.',
        403: 'This document is outside the Chat’s allowed file locations.',
        404: 'The document or its Chat is no longer available. It may have been moved or deleted.',
        415: 'This file is not a UTF-8 Markdown document.',
        422: 'This document path is invalid.',
      }
      error.value = messages[response.status] || 'The document could not be loaded. Please retry.'
      return
    }
    const result: MarkdownDocument = await response.json()
    if (!current.signal.aborted) document.value = result
  } catch {
    if (!current.signal.aborted) error.value = 'The document could not be loaded. Check your connection and retry.'
  } finally {
    if (request === current) loading.value = false
  }
}

// Follow a document fragment once the sanitized heading elements are mounted.
watch([document, showSource, loading], async () => {
  await nextTick()
  if (showSource.value || !article.value) return
  let id: string
  try { id = decodeURIComponent(window.location.hash.slice(1)) } catch { return }
  if (id) window.document.getElementById(id)?.scrollIntoView()
})

onMounted(loadDocument)
onUnmounted(() => request?.abort())
</script>

<style src="@/features/app-shell/styles/controls.css"></style>
<style scoped>
.markdown-reader {
  box-sizing: border-box;
  height: 100dvh;
  overflow: auto;
  padding: 40px max(16px, calc((100% - 960px) / 2)) 64px;
  background: var(--ch-color-main-bg);
  color: var(--ch-color-text);
}

.markdown-reader-header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding-bottom: 24px;
  margin-bottom: 36px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.markdown-reader-title { min-width: 0; flex: 1 1 360px; }
.markdown-reader-label { color: var(--ch-color-text-muted); font-size: 12px; line-height: 16px; }
.markdown-reader-title h1 {
  margin: 8px 0 10px;
  color: var(--ch-color-text-strong);
  font-size: 28px;
  font-weight: 650;
  line-height: 1.25;
  letter-spacing: -0.025em;
  overflow-wrap: anywhere;
}
.markdown-reader-path {
  font-family: var(--ch-font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ch-color-text-subtle);
  overflow-wrap: anywhere;
  user-select: text;
}
.markdown-reader nav { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding-top: 4px; }
.markdown-reader .ch-btn { height: 36px; text-decoration: none; }
.markdown-reader-back { margin-left: -10px; padding-inline: 10px; }
.markdown-reader-view-toggle { min-width: 120px; background: var(--ch-color-surface); }
.markdown-reader .ch-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
  box-shadow: none;
}
.markdown-reader-loading { padding: 24px 0; color: var(--ch-color-text-muted); font-size: 14px; line-height: 1.6; }
.markdown-reader-notice { margin: 16px 0; color: var(--ch-color-text-muted); font-size: 14px; line-height: 1.6; }
.markdown-reader-article { min-width: 0; overflow-wrap: anywhere; }
.markdown-reader-article :deep(.markdown-content) { font-size: 16px; line-height: 1.7; }
.markdown-reader-article :deep(p),
.markdown-reader-article :deep(ul),
.markdown-reader-article :deep(ol),
.markdown-reader-article :deep(blockquote),
.markdown-reader-article :deep(h1),
.markdown-reader-article :deep(h2),
.markdown-reader-article :deep(h3),
.markdown-reader-article :deep(h4) { max-width: 72ch; }
.markdown-reader-article :deep(p),
.markdown-reader-article :deep(ul),
.markdown-reader-article :deep(ol),
.markdown-reader-article :deep(blockquote) { margin-top: 12px; }
.markdown-reader-article :deep(ul),
.markdown-reader-article :deep(ol) { padding-left: 24px; }
.markdown-reader-article :deep(li + li) { margin-top: 6px; }
.markdown-reader-article :deep(h1),
.markdown-reader-article :deep(h2),
.markdown-reader-article :deep(h3),
.markdown-reader-article :deep(h4) {
  color: var(--ch-color-text-strong);
  font-weight: 650;
  letter-spacing: -0.02em;
  scroll-margin-top: 24px;
}
.markdown-reader-article :deep(h1) { font-size: 28px; line-height: 1.25; margin: 32px 0 12px; }
.markdown-reader-article :deep(h2) { font-size: 22px; line-height: 1.4; margin: 28px 0 10px; }
.markdown-reader-article :deep(h3) { font-size: 18px; line-height: 1.4; margin: 22px 0 8px; }
.markdown-reader-article :deep(h4) { font-size: 16px; line-height: 1.45; margin: 18px 0 6px; }
.markdown-reader-article :deep(.markdown-block:first-child > :first-child),
.markdown-reader-article :deep(.markdown-list:first-child) { margin-top: 0; }
.markdown-reader-article :deep(blockquote) {
  padding: 2px 0 2px 18px;
  border-left: 3px solid var(--ch-color-border-strong);
  color: var(--ch-color-text-muted);
}
.markdown-reader-article :deep(a) { text-decoration-thickness: 1px; text-underline-offset: 3px; }
.markdown-reader-article :deep(a:hover) { color: var(--ch-color-text-strong); }
.markdown-reader-article :deep(a:focus-visible) { outline: 2px solid var(--ch-color-accent); outline-offset: 3px; }
.markdown-reader-article :deep(hr) { margin: 28px 0; border: 0; border-top: 1px solid var(--ch-color-border-muted); }
.markdown-reader-article :deep(.markdown-table-scroll),
.markdown-reader-article :deep(.markdown-code-block) { margin-top: 20px; border-radius: var(--ch-radius-lg); }
.markdown-reader-article :deep(table) { font-size: 14px; line-height: 1.5; }
.markdown-reader-article :deep(th),
.markdown-reader-article :deep(td) { padding: 9px 12px; border-color: var(--ch-color-border); }
.markdown-reader-article :deep(pre) { padding: 16px; font-size: 14px; line-height: 1.6; }
.markdown-reader-article :deep(.markdown-code-header) { padding: 8px 12px; }
.markdown-reader-article :deep(.markdown-image-preview) { max-width: 100%; margin: 20px 0; border-radius: var(--ch-radius-lg); }
.markdown-reader-article :deep(img[data-local-img]),
.markdown-reader-article :deep(img[data-quoted-img]) { max-height: min(70dvh, 720px); }
.markdown-reader-source {
  overflow: auto;
  padding: 20px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  font-family: var(--ch-font-mono);
  font-size: 14px;
  line-height: 1.65;
  tab-size: 4;
}
.markdown-reader-source:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
.markdown-reader-error {
  display: grid;
  gap: 12px;
  max-width: 560px;
  padding: 24px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
}
.markdown-reader-error h2 { font-size: 20px; line-height: 1.3; font-weight: 600; }
.markdown-reader-error p { color: var(--ch-color-text-muted); font-size: 15px; line-height: 1.6; }
.markdown-reader-error-actions { display: flex; flex-wrap: wrap; gap: 8px; padding-top: 4px; }
@media (max-width: 640px) {
  .markdown-reader { padding: 24px 16px 40px; }
  .markdown-reader-header { gap: 20px; margin-bottom: 28px; }
  .markdown-reader-title { flex-basis: 100%; }
  .markdown-reader-title h1 { font-size: 24px; }
  .markdown-reader nav { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; width: 100%; padding-top: 0; }
  .markdown-reader .ch-btn { height: 44px; padding-inline: 8px; }
  .markdown-reader-back { margin-left: -8px; }
  .markdown-reader-view-toggle { min-width: 0; }
  .markdown-reader-refresh { grid-column: 3; justify-self: end; }
  .markdown-reader-error { padding: 20px; }
}
@media (pointer: coarse) { .markdown-reader .ch-btn { min-height: 44px; } }
@media (prefers-reduced-motion: reduce) { .markdown-reader .ch-btn { transition: none; } }
</style>
