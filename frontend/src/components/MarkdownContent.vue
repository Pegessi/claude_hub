<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
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
import { computed, onMounted } from 'vue'
import { MarkdownBlockCache } from '@/utils/markdownBlocks'
import { ensureHighlighter, highlightReady } from '@/utils/codeHighlight'

const props = withDefaults(defineProps<{
  text?: string | null
  compact?: boolean
  linkMarkdownPaths?: boolean
  /** When true, the final block is also cached (stream has ended). */
  complete?: boolean
}>(), {
  text: '',
  compact: false,
  linkMarkdownPaths: false,
  complete: false,
})

const emit = defineEmits<{
  markdownPathClick: [path: string]
}>()

// Per-instance block cache. Each completed markdown block is parsed,
// sanitized, and (optionally) link-wrapped exactly once; only the live
// tail (the still-growing last block) is re-rendered on each delta.
const blockCache = new MarkdownBlockCache()

// Kick off lazy loading of the syntax highlighter.  When the chunk
// finishes loading, ``highlightReady`` flips and the ``blocks`` computed
// re-evaluates (it depends on ``highlightReady``), causing the cache to
// invalidate and code blocks to re-render with real highlighting.
onMounted(() => {
  // Degrade gracefully on chunk-load failure: the block renders as plain
  // text and ``ensureHighlighter`` resets its state so the next mount
  // retries the dynamic import.  The ``.catch`` prevents an unhandled
  // rejection from bubbling out of the mount hook.
  ensureHighlighter().catch(() => {})
})

const blocks = computed(() => {
  // Depend on highlightReady so the computed re-evaluates when the
  // lazy-loaded highlighter finishes loading.
  void highlightReady.value

  const source = props.text?.trim() || ''
  if (!source) {
    blockCache.clear()
    return []
  }

  return blockCache.render(source, {
    complete: props.complete,
    linkMarkdownPaths: props.linkMarkdownPaths,
  })
})

function handleClick(event: MouseEvent) {
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

.markdown-block :deep(table) {
  display: block;
  overflow-x: auto;
  border-collapse: collapse;
}

.markdown-block :deep(th),
.markdown-block :deep(td) {
  border: 1px solid var(--ch-color-border-strong);
  padding: 6px 8px;
}
</style>
