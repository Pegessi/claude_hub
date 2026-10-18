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
        <a :href="chatUrl">Back to Hub</a>
        <button
          type="button"
          :disabled="loading"
          @click="loadDocument"
        >
          Refresh
        </button>
        <button
          v-if="document"
          type="button"
          :aria-pressed="showSource"
          @click="showSource = !showSource"
        >
          {{ showSource ? 'Show preview' : 'Show source' }}
        </button>
      </nav>
    </header>
    <p
      v-if="loading"
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
      <a
        v-if="needsLogin"
        href="/api/auth/login"
        target="_blank"
        rel="noopener noreferrer"
      >Sign in to Hub</a>
      <button
        type="button"
        @click="loadDocument"
      >
        Retry
      </button>
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
      <article
        v-else
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
  if (!article.value) return
  let id: string
  try { id = decodeURIComponent(window.location.hash.slice(1)) } catch { return }
  if (id) window.document.getElementById(id)?.scrollIntoView()
})

onMounted(loadDocument)
onUnmounted(() => request?.abort())
</script>

<style scoped>
.markdown-reader {
  box-sizing: border-box;
  height: 100dvh;
  overflow: auto;
  padding: 28px max(20px, calc((100% - 1060px) / 2));
  background: var(--ch-color-main-bg);
  color: var(--ch-color-text);
}
.markdown-reader-header { display: flex; flex-wrap: wrap; gap: 16px; justify-content: space-between; border-bottom: 1px solid var(--ch-color-border); padding-bottom: 20px; margin-bottom: 24px; }
.markdown-reader-title { min-width: 0; flex: 1 1 360px; }
.markdown-reader-label, .markdown-reader-path, .markdown-reader-notice { color: var(--ch-color-text-muted); }
.markdown-reader h1 { font-size: 24px; margin: 6px 0; overflow-wrap: anywhere; }
.markdown-reader-path { font-size: 13px; overflow-wrap: anywhere; margin: 0; }
.markdown-reader nav { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.markdown-reader a { color: var(--ch-color-accent); }
.markdown-reader button { padding: 8px 12px; color: var(--ch-color-text); background: var(--ch-color-surface); border: 1px solid var(--ch-color-border); border-radius: 6px; cursor: pointer; }
.markdown-reader button:disabled { opacity: .5; cursor: default; }
.markdown-reader-article { min-width: 0; overflow-wrap: anywhere; }
.markdown-reader-source { overflow: auto; padding: 16px; background: var(--ch-color-surface); font-size: 14px; line-height: 1.6; }
.markdown-reader-error { padding: 20px; border: 1px solid var(--ch-color-border); border-radius: 8px; }
.markdown-reader-error a { margin-right: 16px; }
@media (max-width: 600px) { .markdown-reader { padding: 16px; } }
</style>
