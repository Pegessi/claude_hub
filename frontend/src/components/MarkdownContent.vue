<!-- eslint-disable vue/no-v-html -->
<template>
  <div
    ref="contentRoot"
    class="markdown-content"
    :class="{ compact }"
    @click="handleClick"
    @error.capture="handleError"
    @scroll.capture="handleCodeScroll"
  >
    <template
      v-for="block in blocks"
      :key="`${renderScope}:${block.key}`"
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
import { computed, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
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

// B6: image URLs already known to fail in this component instance. v-html
// re-creates <img> nodes on every streaming delta; without this set each
// re-render would re-request a dead URL and loop. Failed URLs render the
// placeholder directly until the user explicitly retries one.
const failedImageUrls = new Set<string>()
// COEP blocks a no-cors image from CORS-only CDNs. Remember the anonymous
// retry mode across streamed DOM replacements, without changing signed URLs.
const corsImageUrls = new Set<string>()
const autoCorsImages = new WeakSet<HTMLImageElement>()
const renderScope = computed(() => JSON.stringify([props.tabId, props.documentPath]))
watch([() => props.tabId, () => props.documentPath], () => {
  failedImageUrls.clear()
  corsImageUrls.clear()
}, { flush: 'sync' })

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
  if (!root) return
  syncCodeScrollState(root)
  // Failed images need recovery placeholders even in prose-only messages.
  neutralizeKnownFailures(root)
  if (!blockCache.hasMermaid) return
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

// Code blocks scroll horizontally (``white-space: pre``), and a clipped line is
// indistinguishable from a short one. The block is marked only when its content
// really is wider than the box, so the fade and the scrollbar never suggest a
// remainder that does not exist.
function syncCodeScrollState(root: HTMLElement) {
  const blocks = Array.from(root.querySelectorAll<HTMLElement>('.markdown-code-block'))
  if (blocks.length === 0) return
  // Measure every block before writing any attribute: interleaving reads and
  // writes would force a layout per block on every streamed delta.
  const measured = blocks.map((block) => {
    const pre = block.querySelector('pre')
    if (!pre) return { overflowing: false, atEnd: false }
    return {
      overflowing: pre.scrollWidth > pre.clientWidth + 1,
      // Recomputed every sync: a block that grew wider (streamed code, a resize,
      // enlarged text) is no longer at its end even though it was when the reader
      // last scrolled, and the fade has to come back.
      atEnd: pre.scrollLeft + pre.clientWidth >= pre.scrollWidth - 1,
    }
  })
  blocks.forEach((block, index) => {
    block.toggleAttribute('data-overflow-x', measured[index].overflowing)
    block.toggleAttribute('data-scroll-end', measured[index].overflowing && measured[index].atEnd)
  })
}

// The fade withdraws once the reader reaches the end of the line: an edge that
// dims the last characters would hide the very content it points at. Scroll does
// not bubble, so this is a capture-phase listener on the content root.
function handleCodeScroll(event: Event) {
  const target = event.target
  if (!(target instanceof HTMLElement) || !target.matches('.markdown-code-block > pre')) return
  const block = target.closest<HTMLElement>('.markdown-code-block')
  if (!block) return
  const atEnd = target.scrollLeft + target.clientWidth >= target.scrollWidth - 1
  block.toggleAttribute('data-scroll-end', atEnd)
}

// Text changes arrive through ``blocks``; width changes (window resize, zoom,
// enlarged text) arrive here, and a block that fits at one width can overflow at
// another.
let codeScrollObserver: ResizeObserver | null = null
onMounted(() => {
  const root = contentRoot.value
  if (!root || typeof ResizeObserver === 'undefined') return
  codeScrollObserver = new ResizeObserver(() => syncCodeScrollState(root))
  codeScrollObserver.observe(root)
})
onBeforeUnmount(() => {
  codeScrollObserver?.disconnect()
  codeScrollObserver = null
})

// A proxied image that fails to load (404 / expired temporary file) is
// replaced in place by a restrained text placeholder — never a broken-image
// icon or a large error frame. The ``error`` event does not bubble, so the
// capture-phase listener on the root is what sees it. The placeholder keeps
// an explicit, accessible Retry that rebuilds the same authorized image URL
// (with a cache-busting nonce) without re-running any agent tool.
function withFreshNonce(src: string): string {
  // One stable identity: always strip any previous retry param before
  // appending, so repeated retries never accumulate query parameters.
  const identity = src.replace(/([?&])retry=\d+(?:&|$)/, (_, prefix: string, suffix: string) =>
    prefix === '?' && suffix === '&' ? '?' : '',
  ).replace(/[?&]$/, '')
  const separator = identity.includes('?') ? '&' : '?'
  return `${identity}${separator}retry=${Date.now()}`
}

function imageIdentity(img: HTMLImageElement): string {
  const src = img.getAttribute('src') || ''
  return img.dataset.remoteImg ? src : src.replace(/[?&]retry=\d+$/, '')
}

function handleError(event: Event) {
  const target = event.target
  if (!(target instanceof HTMLImageElement) || !contentRoot.value?.contains(target)) return
  if (!target.dataset.quotedImg && !target.dataset.localImg && !target.dataset.remoteImg) return
  if (target.closest('.quoted-img-missing')) return
  const identityUrl = imageIdentity(target)
  if (target.dataset.remoteImg && target.crossOrigin === null && !failedImageUrls.has(identityUrl)) {
    // A single anonymous retry allows CORS-only hosts under require-corp.
    // Successful CORP-only images never enter this path. Only an explicit
    // Retry may renegotiate from no-cors; automatic recovery never loops back.
    corsImageUrls.add(identityUrl)
    autoCorsImages.add(target)
    target.crossOrigin = 'anonymous'
    return
  }

  // Preserve the original image's rendering contract (lazy load, async
  // decode, dimensions, markers) and its preview wrapper: a Markdown image
  // lives inside button[data-image-preview], and recovery must keep click and
  // keyboard preview working (D1).
  const original = {
    src: target.getAttribute('src') || '',
    alt: target.alt,
    quoted: target.dataset.quotedImg || '',
    local: target.dataset.localImg || '',
    remote: target.dataset.remoteImg || '',
    // An explicit user retry may recover a transient CORP-only failure. Use
    // the authored mode again, rather than permanently forcing failed CORS.
    crossOrigin: autoCorsImages.has(target) ? null : target.crossOrigin,
    srcset: target.getAttribute('srcset'),
    sizes: target.getAttribute('sizes'),
    className: target.className,
    width: target.getAttribute('width'),
    height: target.getAttribute('height'),
    loading: target.getAttribute('loading'),
    decoding: target.getAttribute('decoding'),
  }
  const previewButton = target.closest('button[data-image-preview]')
  // Remember the authorized URL (without the retry nonce) so later v-html
  // re-renders during streaming go straight to the placeholder instead of
  // issuing another failing request — including re-creation of the same block.
  failedImageUrls.add(identityUrl)

  const buildImage = () => {
    const img = document.createElement('img')
    if (original.className) img.className = original.className
    if (original.width) img.setAttribute('width', original.width)
    if (original.height) img.setAttribute('height', original.height)
    img.alt = original.alt
    if (original.quoted) img.dataset.quotedImg = original.quoted
    if (original.local) img.dataset.localImg = original.local
    if (original.remote) img.dataset.remoteImg = original.remote
    if (original.crossOrigin !== null) img.crossOrigin = original.crossOrigin
    if (original.sizes) img.setAttribute('sizes', original.sizes)
    if (original.srcset) img.setAttribute('srcset', original.srcset)
    if (original.loading) img.setAttribute('loading', original.loading)
    if (original.decoding) img.setAttribute('decoding', original.decoding)
    // External queries/fragments may be signed. Retry exactly the same URL.
    img.src = original.remote ? original.src : withFreshNonce(original.src)
    return img
  }

  // Rebuild the original preview wrapper so a recovered image keeps the same
  // click/keyboard lightbox behavior as a never-failed image. The root-level
  // delegated handleClick picks up the replacement automatically.
  const buildPreviewButton = () => {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = previewButton?.className || 'markdown-image-preview'
    button.dataset.imagePreview = '1'
    button.setAttribute(
      'aria-label',
      original.alt ? `Open image ${original.alt}` : 'Open image',
    )
    button.append(buildImage())
    return button
  }

  const buildPlaceholder = () => {
    const placeholder = document.createElement('span')
    placeholder.className = 'quoted-img-missing'

    const label = document.createElement('span')
    label.className = 'quoted-img-missing-text'
    label.textContent = target.alt ? `${target.alt} (image unavailable)` : '[image unavailable]'
    placeholder.append(label)

    const retry = document.createElement('button')
    retry.type = 'button'
    retry.className = 'quoted-img-retry'
    retry.textContent = 'Retry'
    retry.setAttribute(
      'aria-label',
      target.alt ? `Retry loading image ${target.alt}` : 'Retry loading image',
    )
    retry.addEventListener('click', (clickEvent) => {
      clickEvent.preventDefault()
      clickEvent.stopPropagation()
      // Manual retry: stop treating the URL as failed and rebuild the image
      // (inside its preview wrapper when the original had one). One request
      // for local images; remote images may need one anonymous fallback.
      failedImageUrls.delete(identityUrl)
      corsImageUrls.delete(identityUrl)
      placeholder.replaceWith(previewButton ? buildPreviewButton() : buildImage())
    })
    placeholder.append(retry)
    return placeholder
  }

  // Replace the whole original node (img or preview button) with the
  // placeholder; recovery restores the same wrapper structure.
  const originalNode = previewButton ?? target
  originalNode.replaceWith(buildPlaceholder())
}

// B6: after every v-html render, neutralize images whose URL already failed
// in this component instance, so streaming deltas cannot re-request them.
function neutralizeKnownFailures(root: HTMLElement) {
  if (failedImageUrls.size === 0 && corsImageUrls.size === 0) return
  root.querySelectorAll<HTMLImageElement>('img[data-quoted-img], img[data-local-img], img[data-remote-img]').forEach((img) => {
    const src = imageIdentity(img)
    if (img.dataset.remoteImg && corsImageUrls.has(src) && img.crossOrigin === null) {
      autoCorsImages.add(img)
      img.crossOrigin = 'anonymous'
    }
    if (!failedImageUrls.has(src) || img.closest('.quoted-img-missing')) return
    const synthetic = new Event('error', { bubbles: false, cancelable: true })
    Object.defineProperty(synthetic, 'target', { value: img })
    handleError(synthetic)
  })
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
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 6px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  font-size: 0.9em;
  line-height: 1.4;
}

.markdown-block :deep(.quoted-img-retry) {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  font: inherit;
  padding: 0 6px;
  cursor: pointer;
}

.markdown-block :deep(.quoted-img-retry:hover) {
  border-color: var(--ch-color-accent);
}

.markdown-block :deep(.quoted-img-retry:focus-visible) {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 1px;
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

/* Header and code share one grid column so the scroll hint can sit exactly
   over the code area, below a header whose height depends on the theme. */
.markdown-block :deep(.markdown-code-block) {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto auto;
  overflow: hidden;
  margin-top: 8px;
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-canvas);
}

.markdown-block :deep(.markdown-code-header) {
  grid-row: 1;
  grid-column: 1;
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

/* An unknown info string can be arbitrarily long; the chip owns the space it
   needs and the Copy button keeps its size. */
.markdown-block :deep(.markdown-code-header .markdown-code-language) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  grid-row: 2;
  grid-column: 1;
  min-width: 0;
  margin: 0;
  border: 0;
  border-radius: 0;
  /* Horizontal only: a vertical scrollbar inside a code block would trap the
     reader's wheel in a few lines instead of scrolling the message. */
  overflow-x: auto;
  overflow-y: hidden;
  /* The scrollbar stays out of the way until the reader shows interest in the
     block; the fade already says there is more to the right. The standard
     properties cover Chrome and Firefox, the pseudo-element rules below cover
     Safari, which only has that form. */
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
}

.markdown-block :deep(.markdown-code-block pre::-webkit-scrollbar) {
  height: 8px;
}

.markdown-block :deep(.markdown-code-block pre::-webkit-scrollbar-track) {
  background: transparent;
}

.markdown-block :deep(.markdown-code-block pre::-webkit-scrollbar-thumb) {
  border-radius: var(--ch-radius-sm);
  background: transparent;
}

/* Hovering the block, or focusing its Copy button, reveals the scrollbar — the
   same two signals a reader already uses on the block. */
.markdown-block :deep(.markdown-code-block:hover pre),
.markdown-block :deep(.markdown-code-block:focus-within pre) {
  scrollbar-color: var(--ch-color-border-strong) transparent;
}

.markdown-block :deep(.markdown-code-block:hover pre::-webkit-scrollbar-thumb),
.markdown-block :deep(.markdown-code-block:focus-within pre::-webkit-scrollbar-thumb) {
  background: var(--ch-color-border-strong);
}

/* The fade sits over the right edge of the code area and fades the line into
   the block background, so a cut-off line looks cut off instead of finished. */
.markdown-block :deep(.markdown-code-scroll-hint) {
  grid-row: 2;
  grid-column: 1;
  justify-self: end;
  align-self: stretch;
  width: 28px;
  /* The fade sits over the block's own background, so it blends into that and
     not into the canvas: a canvas/surface mismatch would draw a visible band at
     the right edge of the code. */
  background: linear-gradient(to left, var(--ch-color-surface), transparent);
  pointer-events: none;
  opacity: 0;
  transition: opacity 120ms ease-out;
}

.markdown-block :deep(.markdown-code-block[data-overflow-x]:not([data-scroll-end]) .markdown-code-scroll-hint) {
  opacity: 1;
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

/* The fade is a hint, not information: dropping its animation loses nothing. */
@media (prefers-reduced-motion: reduce) {
  .markdown-block :deep(.markdown-code-scroll-hint) {
    transition: none;
  }
}
</style>
