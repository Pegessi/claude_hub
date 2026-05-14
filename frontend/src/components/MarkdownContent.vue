<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    class="markdown-content"
    :class="{ compact }"
    v-html="safeHtml"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = withDefaults(defineProps<{
  text?: string | null
  compact?: boolean
}>(), {
  text: '',
  compact: false,
})

const safeHtml = computed(() => {
  const source = props.text?.trim() || ''
  if (!source) return ''

  const html = marked.parse(source, {
    async: false,
    breaks: true,
    gfm: true,
  })

  return DOMPurify.sanitize(html)
})
</script>

<style scoped>
.markdown-content {
  color: #d4d4d8;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.markdown-content.compact {
  font-size: 12px;
  line-height: 1.45;
}

.markdown-content :deep(*) {
  max-width: 100%;
}

.markdown-content :deep(p),
.markdown-content :deep(ul),
.markdown-content :deep(ol),
.markdown-content :deep(blockquote),
.markdown-content :deep(pre),
.markdown-content :deep(table) {
  margin: 8px 0 0;
}

.markdown-content :deep(:first-child) {
  margin-top: 0;
}

.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3),
.markdown-content :deep(h4) {
  margin: 14px 0 6px;
  color: #f4f4f5;
  line-height: 1.25;
}

.markdown-content :deep(h1) {
  font-size: 18px;
}

.markdown-content :deep(h2) {
  font-size: 16px;
}

.markdown-content :deep(h3),
.markdown-content :deep(h4) {
  font-size: 14px;
}

.markdown-content :deep(a) {
  color: #93c5fd;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-content :deep(code) {
  border: 1px solid #3f3f46;
  border-radius: 4px;
  background: #18181b;
  color: #e5e7eb;
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.92em;
}

.markdown-content :deep(pre) {
  overflow-x: auto;
  border: 1px solid #303030;
  border-radius: 6px;
  background: #18181b;
  padding: 10px;
}

.markdown-content :deep(pre code) {
  border: 0;
  background: transparent;
  padding: 0;
  white-space: pre;
}

.markdown-content :deep(blockquote) {
  border-left: 3px solid #52525b;
  color: #a1a1aa;
  padding-left: 10px;
}

.markdown-content :deep(ul),
.markdown-content :deep(ol) {
  padding-left: 20px;
}

.markdown-content :deep(li + li) {
  margin-top: 4px;
}

.markdown-content :deep(table) {
  display: block;
  overflow-x: auto;
  border-collapse: collapse;
}

.markdown-content :deep(th),
.markdown-content :deep(td) {
  border: 1px solid #3f3f46;
  padding: 6px 8px;
}
</style>
