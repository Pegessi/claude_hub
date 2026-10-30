<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    ref="contentRoot"
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
    @error.capture="handleError"
  >
    <template
      v-for="block in blocks"
      :key="block.key"
    >
      <!-- Non-list blocks: a single element whose innerHTML is the rendered
           block. Completed blocks are cached and never re-rendered; only the
           live tail block's innerHTML is updated on each delta. -->
      <div
        v-if="'html' in block"
        class="markdown-block"
        v-html="block.html"
      />
      <!-- List blocks: one stable <ul>/<ol> with keyed <li> children. Each
           completed <li> is cached and left untouched by Vue; only the final
           (still-growing) <li>'s innerHTML is replaced on each delta. This
           bounds the DOM work for long streamed lists to the final item. -->
      <ol
        v-else-if="block.list.ordered"
        class="markdown-block markdown-list"
        :start="block.list.start"
      >
        <li
          v-for="item in block.list.items"
          :key="item.key"
          v-html="item.html"
        />
      </ol>
      <ul
        v-else
        class="markdown-block markdown-list"
      >
        <li
          v-for="item in block.list.items"
          :key="item.key"
          v-html="item.html"
        />
      </ul>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, watchEffect } from 'vue'
import { MarkdownBlockCache } from '@/utils/markdownBlocks'
import { ensureHighlighter, highlightReady } from '@/utils/codeHighlight'
import { ensureMathRenderer, mathVersion } from '@/utils/mathRendering'
import { writeClipboard } from '@/utils/clipboard'
import { useAttachmentOpener } from '@/composables/useAttachmentPreview'
import { attachmentTargetFromLink } from '@/utils/markdownPreview'

const openAttachment = useAttachmentOpener()

const props = withDefaults(defineProps<{
  text?: string | null
  compact?: boolean
  linkMarkdownPaths?: boolean
  /** When true, the final block is also cached (stream has ended). */
  complete?: boolean
  /** Owning tab id; enables local/provider image src rewriting. */
  tabId?: string
  /** Absolute source path used by the standalone document reader. */
  documentPath?: string
}>(), {
  text: '',
  compact: false,
  linkMarkdownPaths: false,
  complete: false,
  tabId: '',
  documentPath: '',
})

const emit = defineEmits<{
  markdownPathClick: [path: string]
  imageClick: [url: string, alt: string, event: MouseEvent]
}>()

// Per-instance block cache. Each completed markdown block is parsed,
// sanitized, and (optionally) link-wrapped exactly once; only the live
// tail (the still-growing last block) is re-rendered on each delta.
const blockCache = new MarkdownBlockCache()
const contentRoot = ref<HTMLElement | null>(null)

const blocks = computed(() => {
  // Depend on highlightReady so the computed re-evaluates when the
  // lazy-loaded highlighter finishes loading.
  void highlightReady.value
  void mathVersion.value

  const source = props.text?.trim() || ''
  if (!source) {
    blockCache.clear()
    return []
  }

  return blockCache.render(source, {
    complete: props.complete,
    linkMarkdownPaths: props.linkMarkdownPaths,
    quotedTabId: props.tabId,
    documentPath: props.documentPath,
  })
})

// Prose-only messages need neither renderer. Completed block caches are
// invalidated once when a requested chunk arrives; failed loads keep source.
watchEffect(() => {
  void blocks.value
  if (blockCache.hasCode) ensureHighlighter().catch(() => {})
  if (blockCache.hasMath) ensureMathRenderer().catch(() => {})
})

// Render only after Vue has applied v-html. Cancellation discards async results
// after streaming replacement/unmount; completed DOM nodes retain their diagrams.
watch([blocks, contentRoot], (_, __, onCleanup) => {
  const root = contentRoot.value
  if (!root || !blockCache.hasMermaid) return
  let current = true
  let timer: ReturnType<typeof setTimeout>
  const render = () => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      import('@/utils/mermaidRendering').then(module => {
        if (current) return module.renderMermaidBlocks(root, () => current)
      }).catch(() => {
        if (!current) return
        root.querySelectorAll('.markdown-code-block:has(code.language-mermaid)').forEach(block => {
          if (block.querySelector('.markdown-mermaid-error')) return
          const hint = document.createElement('div')
          hint.className = 'markdown-mermaid-error'
          hint.setAttribute('role', 'status')
          hint.textContent = 'Diagram unavailable. Showing source.'
          block.append(hint)
        })
      })
    }, 120)
  }
  const observer = new MutationObserver(render)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  render()
  onCleanup(() => {
    current = false
    clearTimeout(timer)
    observer.disconnect()
  })
}, { flush: 'post' })

// A proxied image that fails to load (404 / expired temporary file) is
// replaced in place by a restrained text placeholder — never a broken-image
// icon or a large error frame. The ``error`` event does not bubble, so the
// capture-phase listener on the root is what sees it.
function handleError(event: Event) {
  const target = event.target
  if (!(target instanceof HTMLImageElement)) return
  if (!target.dataset.quotedImg && !target.dataset.localImg) return
  const placeholder = document.createElement('span')
  placeholder.className = 'quoted-img-missing'
  placeholder.textContent = target.alt ? `${target.alt} (image unavailable)` : '[image unavailable]'
  const button = target.closest('button[data-image-preview]')
  ;(button || target).replaceWith(placeholder)
}

async function handleClick(event: MouseEvent) {
  const copyButton = event.target instanceof Element
    ? event.target.closest<HTMLButtonElement>('button[data-copy-code]')
    : null
  if (copyButton) {
    event.preventDefault()
    const code = copyButton.closest('.markdown-code-block')?.querySelector('pre code')
    if (!code) return
    try {
      await writeClipboard(code.textContent || '')
      copyButton.textContent = 'Copied'
    } catch {
      copyButton.textContent = 'Copy failed'
    }
    return
  }
  const imageButton = event.target instanceof Element
    ? event.target.closest<HTMLButtonElement>('button[data-image-preview]')
    : null
  const image = imageButton?.querySelector('img')
  if (image) {
    event.preventDefault()
    if (openAttachment) openAttachment({ scope: props.tabId, path: '', imageUrl: image.src, name: image.alt }, event)
    else emit('imageClick', image.src, image.alt, event)
    return
  }
  const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null
  if (anchor && openAttachment && props.tabId && event.button === 0
    && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    const target = attachmentTargetFromLink(anchor.getAttribute('href') || '', props.documentPath)
    if (target) {
      event.preventDefault()
      openAttachment({ ...target, scope: props.tabId }, event)
      return
    }
    // Document headings scroll inside the viewer without changing Chat history.
    const href = anchor.getAttribute('href') || ''
    if (props.documentPath && href.startsWith('#')) {
      event.preventDefault()
      let id: string
      try { id = decodeURIComponent(href.slice(1)) } catch { return }
      const root = anchor.closest('.markdown-content')
      Array.from(root?.querySelectorAll('[id]') || []).find(element => element.id === id)?.scrollIntoView()
      return
    }
  }
  const target = event.target instanceof Element
    ? event.target.closest<HTMLAnchorElement>('a[data-markdown-path]')
    : null
  if (!target) return
  event.preventDefault()
  emit('markdownPathClick', target.dataset.markdownPath || target.textContent || '')
}
</script>

<style scoped>
.markdown-content {
  color: var(--ch-color-text);
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.markdown-content.compact {
  font-size: 12px;
  line-height: 1.45;
}

/* Each block is its own element so Vue can leave completed blocks' DOM
   untouched when only the live tail changes. Collapse margins between
   blocks so spacing matches the single-v-html layout. */
.markdown-block {
  display: block;
}

.markdown-block :deep(*) {
  max-width: 100%;
}

.markdown-block :deep(p),
.markdown-block :deep(ul),
.markdown-block :deep(ol),
.markdown-block :deep(blockquote),
.markdown-block :deep(pre),
.markdown-block :deep(table) {
  margin: 8px 0 0;
}

/* Lists are now rendered as direct .markdown-block elements (not nested
   inside a div), so reset their margin directly. */
.markdown-list {
  margin: 8px 0 0;
  padding-left: 20px;
}

/* The first child of the first block should have no top margin. Since
   blocks are separate elements, target the first block's first child. */
.markdown-block:first-child :deep(:first-child) {
  margin-top: 0;
}

/* First block is a list: remove its top margin directly. */
.markdown-list:first-child {
  margin-top: 0;
}

.markdown-block :deep(h1),
.markdown-block :deep(h2),
.markdown-block :deep(h3),
.markdown-block :deep(h4) {
  margin: 14px 0 6px;
  color: var(--ch-color-text);
  line-height: 1.25;
}

.markdown-block :deep(h1) {
  font-size: 18px;
}

.markdown-block :deep(h2) {
  font-size: 16px;
}

.markdown-block :deep(h3),
.markdown-block :deep(h4) {
  font-size: 14px;
}

.markdown-block :deep(a) {
  color: var(--ch-color-accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-block :deep(code) {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  padding: 1px 5px;
  font-family: var(--ch-font-mono);
  font-size: 0.92em;
}

.markdown-block :deep(pre) {
  overflow-x: auto;
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-canvas);
  padding: 12px;
  font-family: var(--ch-font-mono);
  font-size: 0.9em;
  line-height: 1.5;
}

.markdown-block :deep(pre code) {
  border: 0;
  background: transparent;
  padding: 0;
  white-space: pre;
}

/* ---- Syntax highlighting (highlight.js token → CSS variable) ---- */

.markdown-block :deep(.hljs-keyword),
.markdown-block :deep(.hljs-selector-tag),
.markdown-block :deep(.hljs-built_in),
.markdown-block :deep(.hljs-name),
.markdown-block :deep(.hljs-tag) {
  color: var(--ch-code-keyword);
}

.markdown-block :deep(.hljs-string),
.markdown-block :deep(.hljs-regexp),
.markdown-block :deep(.hljs-code),
.markdown-block :deep(.hljs-template-variable) {
  color: var(--ch-code-string);
}

.markdown-block :deep(.hljs-title),
.markdown-block :deep(.hljs-title.function_),
.markdown-block :deep(.hljs-section),
.markdown-block :deep(.hljs-formula) {
  color: var(--ch-code-function);
}

.markdown-block :deep(.hljs-number),
.markdown-block :deep(.hljs-literal),
.markdown-block :deep(.hljs-symbol),
.markdown-block :deep(.hljs-bullet) {
  color: var(--ch-code-number);
}

.markdown-block :deep(.hljs-comment),
.markdown-block :deep(.hljs-quote),
.markdown-block :deep(.hljs-link) {
  color: var(--ch-code-comment);
}

.markdown-block :deep(.hljs-type),
.markdown-block :deep(.hljs-class .hljs-title) {
  color: var(--ch-code-builtin);
}

.markdown-block :deep(.hljs-attr),
.markdown-block :deep(.hljs-attribute),
.markdown-block :deep(.hljs-variable) {
  color: var(--ch-code-attr);
}

.markdown-block :deep(.hljs-selector-id),
.markdown-block :deep(.hljs-selector-class),
.markdown-block :deep(.hljs-selector-attr),
.markdown-block :deep(.hljs-selector-pseudo) {
  color: var(--ch-code-selector);
}

.markdown-block :deep(.hljs-meta),
.markdown-block :deep(.hljs-meta .hljs-keyword) {
  color: var(--ch-code-meta);
}

.markdown-block :deep(.hljs-deletion) {
  color: var(--ch-code-deletion);
}

.markdown-block :deep(.hljs-addition) {
  color: var(--ch-code-addition);
}

.markdown-block :deep(.hljs-emphasis) {
  color: var(--ch-code-emphasis);
  font-style: italic;
}

.markdown-block :deep(.hljs-strong) {
  color: var(--ch-code-strong);
  font-weight: 700;
}

.markdown-block :deep(blockquote) {
  border-left: 3px solid var(--ch-color-border-hover);
  color: var(--ch-color-text-muted);
  padding-left: 10px;
}

.markdown-block :deep(li + li) {
  margin-top: 4px;
}

/* Proxied local/provider images stay within the chat width on mobile too. */
.markdown-block :deep(.markdown-image-preview) {
  display: block;
  max-width: min(420px, 100%);
  margin: 8px 0;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  line-height: 0;
  cursor: zoom-in;
}

.markdown-block :deep(.markdown-image-preview:focus-visible) {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}

.markdown-block :deep(img[data-quoted-img]),
.markdown-block :deep(img[data-local-img]) {
  max-width: 100%;
  max-height: 260px;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
}

/* Restrained fallback shown when a proxied quoted image cannot be loaded —
   muted inline text, not a broken-image frame. */
.markdown-block :deep(.quoted-img-missing) {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  font-size: 0.9em;
}

.markdown-block :deep(table) {
  display: table;
  width: 100%;
  border-collapse: collapse;
}

.markdown-block :deep(.markdown-table-scroll) {
  max-width: 100%;
  overflow-x: auto;
  margin-top: 8px;
  border-radius: var(--ch-radius-sm);
}

.markdown-block :deep(.markdown-table-scroll table) {
  margin: 0;
}

.markdown-block :deep(th),
.markdown-block :deep(td) {
  border: 1px solid var(--ch-color-border-strong);
  padding: 6px 8px;
  min-width: 88px;
  vertical-align: top;
}

.markdown-block :deep(th:not([align])),
.markdown-block :deep(td:not([align])) {
  text-align: left;
}

.markdown-block :deep(th[align="right"]),
.markdown-block :deep(td[align="right"]) {
  text-align: right;
}

.markdown-block :deep(th[align="center"]),
.markdown-block :deep(td[align="center"]) {
  text-align: center;
}

.markdown-block :deep(th[align="left"]),
.markdown-block :deep(td[align="left"]) {
  text-align: left;
}

.markdown-block :deep(th) {
  background: var(--ch-color-canvas);
  font-weight: 600;
}

.markdown-block :deep(.markdown-code-block) {
  overflow: hidden;
  margin-top: 8px;
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-canvas);
}

.markdown-block :deep(.markdown-code-header) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--ch-color-border);
  color: var(--ch-color-text-muted);
  font-family: var(--ch-font-mono);
  font-size: 11px;
}

.markdown-block :deep(.markdown-code-header button) {
  flex-shrink: 0;
  padding: 4px 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  font: inherit;
  cursor: pointer;
}

.markdown-block :deep(.markdown-code-block pre) {
  margin: 0;
  border: 0;
  border-radius: 0;
}

.markdown-block :deep(.markdown-table-scroll:focus-visible),
.markdown-block :deep(.markdown-math-display:focus-visible),
.markdown-block :deep(.markdown-code-header button:focus-visible) {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: -2px;
}

.markdown-block :deep(.markdown-math) {
  overflow-wrap: normal;
  word-break: normal;
}

.markdown-block :deep(.markdown-math-display) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  padding: 10px 2px;
}

.markdown-block :deep(.markdown-math:not(.markdown-math-display)) {
  display: inline-block;
  max-width: 100%;
  overflow-x: auto;
  vertical-align: middle;
}

/* KaTeX sizes inner boxes itself. A global max-width would squash fractions. */
.markdown-block :deep(.katex *) {
  max-width: none;
}

.markdown-block :deep(.katex) {
  font-size: 1.12em;
}

.markdown-block :deep(.katex-display) {
  margin: 0;
}

.markdown-block :deep(.katex-display > .katex) {
  text-align: left;
}

.markdown-block :deep(.math-source) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: var(--ch-font-mono);
  font-size: 0.92em;
}

.markdown-block :deep(.mermaid-rendered > pre) {
  display: none;
}

.markdown-block :deep(.markdown-mermaid-diagram) {
  overflow: auto;
  max-height: 75vh;
  padding: 16px;
  background: var(--ch-color-canvas);
}

.markdown-block :deep(.markdown-mermaid-diagram > svg) {
  display: block;
  width: var(--mermaid-width, 100%);
  max-width: none;
  height: auto;
  color: var(--ch-color-text);
  font-family: var(--ch-font-sans, sans-serif);
  font-size: 16px;
}

.markdown-block :deep(.markdown-mermaid-error) {
  padding: 6px 12px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}
</style>
