<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
  >
    <div
      v-for="block in blocks"
      :key="block.key"
      class="markdown-block"
      v-html="block.html"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { MarkdownBlockCache } from '@/utils/markdownBlocks'

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

const blocks = computed(() => {
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

/* The first child of the first block should have no top margin. Since
   blocks are separate elements, target the first block's first child. */
.markdown-block:first-child :deep(:first-child) {
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

.markdown-block :deep(blockquote) {
  border-left: 3px solid var(--ch-color-border-hover);
  color: var(--ch-color-text-muted);
  padding-left: 10px;
}

.markdown-block :deep(ul),
.markdown-block :deep(ol) {
  padding-left: 20px;
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
