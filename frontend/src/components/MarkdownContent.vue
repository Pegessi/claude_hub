<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
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
  linkMarkdownPaths?: boolean
}>(), {
  text: '',
  compact: false,
  linkMarkdownPaths: false,
})

const emit = defineEmits<{
  markdownPathClick: [path: string]
}>()

const markdownPathPattern = /((?:~|\.{1,2}|\/|[\w.-]+\/)?[\w./~@:+-]+\.(?:md|markdown|mdown|mkd)(?::\d+)?(?:[?#][^\s`"'<>)]*)?)/gi

const safeHtml = computed(() => {
  const source = props.text?.trim() || ''
  if (!source) return ''

  const html = marked.parse(source, {
    async: false,
    breaks: true,
    gfm: true,
  })

  const sanitized = DOMPurify.sanitize(html)
  return props.linkMarkdownPaths ? linkPathMentions(sanitized) : sanitized
})

function linkPathMentions(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT)
  const textNodes: Text[] = []
  let node = walker.nextNode()
  while (node) {
    if (node instanceof Text && !hasLinkExcludedParent(node)) {
      textNodes.push(node)
    }
    node = walker.nextNode()
  }

  textNodes.forEach(textNode => {
    const text = textNode.nodeValue || ''
    const matches = Array.from(text.matchAll(markdownPathPattern))
    if (matches.length === 0) return

    const fragment = document.createDocumentFragment()
    let offset = 0
    matches.forEach(match => {
      const path = match[0]
      const index = match.index ?? 0
      if (index > offset) {
        fragment.append(document.createTextNode(text.slice(offset, index)))
      }
      const link = document.createElement('a')
      link.href = '#'
      link.dataset.markdownPath = path
      link.textContent = path
      link.className = 'markdown-path-link'
      fragment.append(link)
      offset = index + path.length
    })
    if (offset < text.length) {
      fragment.append(document.createTextNode(text.slice(offset)))
    }
    textNode.replaceWith(fragment)
  })

  return template.innerHTML
}

function hasLinkExcludedParent(node: Node): boolean {
  let parent = node.parentElement
  while (parent) {
    if (['A', 'CODE', 'PRE', 'KBD', 'SAMP'].includes(parent.tagName)) return true
    parent = parent.parentElement
  }
  return false
}

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
  color: var(--ch-color-text);
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
  color: var(--ch-color-accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-content :deep(code) {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.92em;
}

.markdown-content :deep(pre) {
  overflow-x: auto;
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: 6px;
  background: var(--ch-color-canvas);
  padding: 10px;
}

.markdown-content :deep(pre code) {
  border: 0;
  background: transparent;
  padding: 0;
  white-space: pre;
}

.markdown-content :deep(blockquote) {
  border-left: 3px solid var(--ch-color-border-hover);
  color: var(--ch-color-text-muted);
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
  border: 1px solid var(--ch-color-border-strong);
  padding: 6px 8px;
}
</style>
