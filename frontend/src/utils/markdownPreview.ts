/** Local document links keep their Chat scope instead of becoming web paths. */
export function markdownStreamBase(scope: string): string | null {
  if (/^\/api\/workspaces\/(?:tabs|sessions)\/[^/?#]+\/stream$/.test(scope)) return scope
  if (!scope || /[/\\?#]/.test(scope)) return null
  return `/api/workspaces/tabs/${encodeURIComponent(scope)}/stream`
}

const MARKDOWN = /\.(?:md|markdown|mdown|mkd)$/i
const TEXT = /\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|c|h|cc|cpp|cxx|hh|hpp|hxx|cu|cuh|go|rs|java|kt|kts|swift|rb|php|sh|bash|zsh|fish|sql|css|scss|less|html|htm|vue|svelte|json|jsonc|jsonl|ndjson|yaml|yml|toml|xml|csv|tsv|ini|ipynb|txt|log|conf|diff|patch|rst)$/i
const NAMED_TEXT = /^(?:dockerfile|makefile|cmakelists\.txt|readme|license)$/i
const IMAGE = /\.(?:png|jpe?g|gif|webp)$/i
// Known unsupported files get an explanation, never an unrestricted file request.
const BINARY = /\.(?:bmp|svg|pdf|zip|gz|tar|7z|rar|xlsx?|docx?|pptx?|parquet|npy|npz|bin|exe|wasm)$/i

export type AttachmentKind = 'markdown' | 'text' | 'image' | 'unsupported'
export function attachmentKind(path: string): AttachmentKind | null {
  if (MARKDOWN.test(path)) return 'markdown'
  if (TEXT.test(path) || NAMED_TEXT.test(path.split('/').pop() || '')) return 'text'
  if (IMAGE.test(path)) return 'image'
  if (BINARY.test(path)) return 'unsupported'
  return null
}

export function localAttachmentTarget(href: string, documentPath = ''): { path: string; hash: string } | null {
  let value = href.trim()
  if (/^file:\/\/(?:localhost)?\//i.test(value)) value = value.replace(/^file:\/\/(?:localhost)?/i, '')
  if (!value || /^[#?]|^&(?:num|quest);|^[a-z][a-z\d+.-]*:|^\/\//i.test(value)) return null
  if (/^\/(?:api|assets|static)\//.test(value)) return null
  const hashIndex = value.indexOf('#')
  const hash = hashIndex < 0 ? '' : value.slice(hashIndex)
  value = value.split(/[?#]/, 1)[0]
  let path: string
  try { path = decodeURIComponent(value) } catch { return null }
  path = path.replace(/:\d+(?::\d+)?$/, '')
  if (!attachmentKind(path)) return null
  if ([...path].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return null
  if (path.includes('\\') || /^[a-z][a-z\d+.-]*:|^\/\//i.test(path)) return null
  // Reject encoded web-resource prefixes too.
  if (/^\/(?:api|assets|static)\//.test(path)) return null
  if (documentPath && !path.startsWith('/') && !path.startsWith('~/')) {
    path = documentPath.slice(0, documentPath.lastIndexOf('/') + 1) + path
  }
  if (path.length > 4096) return null
  return { path, hash }
}

export function localMarkdownTarget(href: string, documentPath = ''): { path: string; hash: string } | null {
  const target = localAttachmentTarget(href, documentPath)
  return target && MARKDOWN.test(target.path) ? target : null
}

export function markdownPreviewUrl(scope: string, href: string, documentPath = ''): string | null {
  const target = localMarkdownTarget(href, documentPath)
  if (!target || !markdownStreamBase(scope)) return null
  return `/?${new URLSearchParams({ markdown: target.path, scope })}${target.hash}`
}

export function attachmentPreviewUrl(scope: string, href: string, documentPath = ''): string | null {
  const target = localAttachmentTarget(href, documentPath)
  if (!target || !markdownStreamBase(scope)) return null
  if (MARKDOWN.test(target.path)) return markdownPreviewUrl(scope, href, documentPath)
  return `/?${new URLSearchParams({ attachment: target.path, scope })}${target.hash}`
}

/** Parse only our own local reader URLs, always using the caller's scope. */
export function attachmentTargetFromLink(href: string, documentPath = ''): { path: string; hash: string } | null {
  if (href.startsWith('/?')) {
    const url = new URL(href, 'http://attachment.local')
    const path = url.searchParams.get('attachment') ?? url.searchParams.get('markdown')
    if (path !== null) return localAttachmentTarget(`${encodeURIComponent(path)}${url.hash}`, documentPath)
  }
  return localAttachmentTarget(href, documentPath)
}

/** Only decorate sanitized anchors; code samples and external URLs stay unchanged. */
export function rewriteMarkdownLinks(html: string, scope: string, documentPath = ''): string {
  if (!scope || typeof document === 'undefined' || !/<a[\s>]/i.test(html)) return html
  const template = document.createElement('template')
  template.innerHTML = html
  template.content.querySelectorAll('a[href]').forEach((link) => {
    const target = attachmentTargetFromLink(link.getAttribute('href') || '', documentPath)
    if (!target) return
    const url = attachmentPreviewUrl(scope, `${encodeURIComponent(target.path)}${target.hash}`)
    if (!url) return
    // The href remains usable by modified clicks and by the standalone reader.
    link.setAttribute('href', url)
    link.setAttribute('target', documentPath ? '_self' : '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
    link.removeAttribute('data-markdown-path')
  })
  return template.innerHTML
}
