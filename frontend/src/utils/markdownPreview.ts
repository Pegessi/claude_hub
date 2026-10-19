/** Local document links keep their Chat scope instead of becoming web paths. */
export function markdownStreamBase(scope: string): string | null {
  if (/^\/api\/workspaces\/(?:tabs|sessions)\/[^/?#]+\/stream$/.test(scope)) return scope
  if (!scope || /[/\\?#]/.test(scope)) return null
  return `/api/workspaces/tabs/${encodeURIComponent(scope)}/stream`
}

const MARKDOWN = /\.(?:md|markdown|mdown|mkd)$/i
const HTML = /\.html?$/i
const TEXT = /\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|c|h|cc|cpp|cxx|hh|hpp|hxx|cu|cuh|go|rs|java|kt|kts|swift|rb|php|sh|bash|zsh|fish|sql|css|scss|less|vue|svelte|json|jsonc|jsonl|ndjson|yaml|yml|toml|xml|csv|tsv|ini|ipynb|txt|log|conf|diff|patch|rst)$/i
const NAMED_TEXT = /^(?:dockerfile|makefile|cmakelists\.txt|readme|license)$/i
const IMAGE = /\.(?:png|jpe?g|gif|webp)$/i
// Known unsupported files get an explanation, never an unrestricted file request.
const BINARY = /\.(?:bmp|svg|pdf|zip|gz|tar|7z|rar|xlsx?|docx?|pptx?|parquet|npy|npz|bin|exe|wasm)$/i

export type AttachmentKind = 'markdown' | 'html' | 'text' | 'image' | 'unsupported'
export function attachmentKind(path: string): AttachmentKind | null {
  if (MARKDOWN.test(path)) return 'markdown'
  if (HTML.test(path)) return 'html'
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

interface AttachmentLinkTarget { path: string; hash: string; registered?: boolean }

export function rawHtmlPreviewPath(pathname: string): string {
  const target = localAttachmentTarget(pathname)
  return target && target.path !== '/index.html' && target.path.startsWith('/') && HTML.test(target.path) ? target.path : ''
}

export function registeredHtmlPreviewUrl(path: string, hash = ''): string | null {
  const target = localAttachmentTarget(encodeURIComponent(path))
  if (!target?.path.startsWith('/') || !HTML.test(target.path)) return null
  return `/?${new URLSearchParams({ attachment: target.path, registered: '1' })}${hash}`
}

export function attachmentPreviewUrl(scope: string, href: string, documentPath = ''): string | null {
  const target = attachmentTargetFromLink(href, documentPath)
  if (!target) return null
  if (target.registered) return registeredHtmlPreviewUrl(target.path, target.hash)
  if (!markdownStreamBase(scope)) return null
  const key = MARKDOWN.test(target.path) ? 'markdown' : 'attachment'
  return `/?${new URLSearchParams({ [key]: target.path, scope })}${target.hash}`
}

/** Local reader URLs retain Chat scope; published HTML URLs use their exact grant. */
export function attachmentTargetFromLink(href: string, documentPath = ''): AttachmentLinkTarget | null {
  let value = href
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value)
      if (typeof window === 'undefined' || url.origin !== window.location.origin || url.username || url.password) return null
      if (url.pathname !== '/') {
        const path = rawHtmlPreviewPath(url.pathname)
        return path ? { path, hash: url.hash, registered: true } : null
      }
      value = `${url.pathname}${url.search}${url.hash}`
    } catch { return null }
  }
  if (value.startsWith('/?')) {
    const url = new URL(value, 'http://attachment.local')
    const path = url.searchParams.get('attachment') ?? url.searchParams.get('markdown')
    if (path !== null) {
      const target = localAttachmentTarget(`${encodeURIComponent(path)}${url.hash}`, documentPath)
      if (url.searchParams.get('registered') === '1') {
        return target?.path.startsWith('/') && HTML.test(target.path) ? { ...target, registered: true } : null
      }
      return target
    }
  }
  return localAttachmentTarget(value, documentPath)
}

/** Only decorate sanitized anchors; code samples and external URLs stay unchanged. */
export function rewriteMarkdownLinks(html: string, scope: string, documentPath = ''): string {
  if (!scope || typeof document === 'undefined' || !/<a[\s>]/i.test(html)) return html
  const template = document.createElement('template')
  template.innerHTML = html
  template.content.querySelectorAll('a[href]').forEach((link) => {
    const url = attachmentPreviewUrl(scope, link.getAttribute('href') || '', documentPath)
    if (!url) return
    // The href remains usable by modified clicks and by the standalone reader.
    link.setAttribute('href', url)
    link.setAttribute('target', documentPath ? '_self' : '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
    link.removeAttribute('data-markdown-path')
  })
  return template.innerHTML
}
