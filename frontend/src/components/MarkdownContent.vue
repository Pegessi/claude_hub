<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
    v-html="safeHtml"
  />
</template>

<script lang="ts">
// True module-scope declarations. Evaluated ONCE at module load, not per
// component instance, so htmlCache is shared across every MarkdownContent
// instance on the page. FIFO eviction at HTML_CACHE_CAP.
const HTML_CACHE_CAP = 200
const MARKED_OPTS = { async: false as const, breaks: true, gfm: true }
const htmlCache = new Map<string, string>()
</script>

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
  const raw = props.text ?? ''
  const source = raw.trim()
  if (!source) return ''

  const key = raw + '\0' + (props.linkMarkdownPaths ? '1' : '0')
  const cached = htmlCache.get(key)
  if (cached !== undefined) return cached

  const html = marked.parse(source, MARKED_OPTS)
  const sanitized = DOMPurify.sanitize(html)
  const result = props.linkMarkdownPaths ? linkPathMentions(sanitized) : sanitized

  htmlCache.set(key, result)
  if (htmlCache.size > HTML_CACHE_CAP) {
    htmlCache.delete(htmlCache.keys().next().value as string)
  }
  return result
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
/*
 * Markdown content — DOMPurify + marked renderer for reports/messages.
 *
 * Styling consumes the global design-token scale (--ch-font-*, --ch-space-*,
 * --ch-leading-*) where values match exactly. Per the conservative mapping
 * rule for this pass, off-scale hardcoded px are LEFT LITERAL with a short
 * comment when the nearest token would introduce a visible change:
 *   • h2 16px / h3-h4 14px (no matching font token; lg=15 / md=13 would
 *     visibly shrink headings)
 *   • Heading margin 14px 0 6px (neither 12/16 nor 4/8 is a clean match
 *     for the established heading rhythm)
 *   • pre padding 10px / blockquote padding-left 10px (nearest tokens 8/12
 *     would shift code-block/quote density)
 *   • ul/ol padding-left 20px (list indent distance; no token)
 *   • th/td padding 6px 8px (cell density; 6px has no clean token)
 *   • Body line-height 1.55 (slightly above --ch-leading-normal=1.5) and
 *     compact line-height 1.45 (slightly below) — prose-reading tweaks
 *   • 1/2/3px borders, underline offset, and accent bar (stroke/affordance
 *     constants, not spacing)
 *   • em-based code font-size 0.92em (relative sizing; per task instruction)
 */

.markdown-content {
  color: var(--ch-color-text);
  font-size: var(--ch-font-md);
  line-height: 1.55; /* prose reading; slightly above --ch-leading-normal */
  overflow-wrap: anywhere;
  word-break: break-word;
}

.markdown-content.compact {
  font-size: var(--ch-font-sm);
  line-height: 1.45; /* compact prose; slightly below --ch-leading-normal */
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
  margin: var(--ch-space-2) 0 0;
}

.markdown-content :deep(:first-child) {
  margin-top: 0;
}

.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3),
.markdown-content :deep(h4) {
  margin: 14px 0 6px; /* off-scale; nearest tokens 12/16 and 4/8 shift heading rhythm */
  color: var(--ch-color-text);
  line-height: var(--ch-leading-tight);
}

.markdown-content :deep(h1) {
  font-size: var(--ch-font-xl);
}

.markdown-content :deep(h2) {
  font-size: 16px; /* no token; lg=15px would visibly shrink h2 */
}

.markdown-content :deep(h3),
.markdown-content :deep(h4) {
  font-size: 14px; /* no token; md=13px would visibly shrink sub-headings */
}

.markdown-content :deep(a) {
  color: var(--ch-color-accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.markdown-content :deep(code) {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  padding: 1px var(--ch-space-1);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.92em; /* em-based; keep per task instruction */
}

.markdown-content :deep(pre) {
  overflow-x: auto;
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-canvas);
  padding: 10px; /* off-scale; nearest 8/12 shifts code-block density */
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
  padding-left: 10px; /* off-scale; nearest 8/12 shifts quote indent */
}

.markdown-content :deep(ul),
.markdown-content :deep(ol) {
  padding-left: 20px; /* list indent distance; no token */
}

.markdown-content :deep(li + li) {
  margin-top: var(--ch-space-1);
}

.markdown-content :deep(table) {
  display: block;
  overflow-x: auto;
  border-collapse: collapse;
}

.markdown-content :deep(th),
.markdown-content :deep(td) {
  border: 1px solid var(--ch-color-border-strong);
  padding: 6px 8px; /* 6px off-scale; keep to preserve cell density */
}
</style>
