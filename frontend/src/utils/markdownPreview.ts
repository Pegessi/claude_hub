/** Local Markdown links carry their Chat scope instead of becoming web paths. */
export function markdownStreamBase(scope: string): string | null {
  if (/^\/api\/workspaces\/(?:tabs|sessions)\/[^/?#]+\/stream$/.test(scope)) return scope
  if (!scope || /[/\\?#]/.test(scope)) return null
  return `/api/workspaces/tabs/${encodeURIComponent(scope)}/stream`
}

export function localMarkdownTarget(href: string, documentPath = ''): { path: string; hash: string } | null {
  let value = href.trim()
  if (/^file:\/\/(?:localhost)?\//i.test(value)) value = value.replace(/^file:\/\/(?:localhost)?/i, '')
  if (!value || /^[#?]|^[a-z][a-z\d+.-]*:|^\/\//i.test(value)) return null
  if (/^\/(?:api|assets|static)\//.test(value)) return null
  const hashIndex = value.indexOf('#')
  const hash = hashIndex < 0 ? '' : value.slice(hashIndex)
  value = value.split(/[?#]/, 1)[0]
  let path: string
  try { path = decodeURIComponent(value) } catch { return null }
  if (!/\.(?:md|markdown|mdown|mkd)(?::\d+)?$/i.test(path)) return null
  path = path.replace(/:\d+$/, '')
  if ([...path].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return null
  if (path.includes('\\') || /^[a-z][a-z\d+.-]*:|^\/\//i.test(path)) return null
  if (documentPath && !path.startsWith('/') && !path.startsWith('~/')) {
    path = documentPath.slice(0, documentPath.lastIndexOf('/') + 1) + path
  }
  if (path.length > 4096) return null
  return { path, hash }
}

export function markdownPreviewUrl(scope: string, href: string, documentPath = ''): string | null {
  const target = localMarkdownTarget(href, documentPath)
  if (!target || !markdownStreamBase(scope)) return null
  const query = new URLSearchParams({ markdown: target.path, scope })
  return `/?${query}${target.hash}`
}

/** Only decorate sanitized anchors; code samples and external URLs stay unchanged. */
export function rewriteMarkdownLinks(html: string, scope: string, documentPath = ''): string {
  if (!scope || typeof document === 'undefined' || !/<a[\s>]/i.test(html)) return html
  const template = document.createElement('template')
  template.innerHTML = html
  template.content.querySelectorAll('a[href]').forEach((link) => {
    const href = link.getAttribute('href') || ''
    let url = markdownPreviewUrl(scope, href, documentPath)
    if (href.startsWith('/?')) {
      const query = new URL(href, 'http://markdown.local')
      const path = query.searchParams.get('markdown')
      if (path !== null) {
        url = markdownPreviewUrl(scope, `${encodeURIComponent(path)}${query.hash}`, documentPath)
      }
    }
    if (!url) return
    // Sanitized Markdown can contain SVG anchors with readonly animated properties.
    link.setAttribute('href', url)
    link.setAttribute('target', documentPath ? '_self' : '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
    link.removeAttribute('data-markdown-path')
  })
  return template.innerHTML
}
