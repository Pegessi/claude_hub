import mermaid from 'mermaid'
import DOMPurify from 'dompurify'

let nextId = 0
// Mermaid has global configuration and render state. Serialize across messages.
let pending: Promise<void> = Promise.resolve()
const rendered = new WeakMap<Element, string>()

/** Resource-bearing syntax and author configuration are intentionally unsupported.
 * Reject before Mermaid creates its temporary DOM, not just after rendering.
 * Ordinary diagram labels, edges and links can still be displayed as text. */
function checkSource(source: string): void {
  if (source.length > 50_000) throw new Error('Diagram is too large')
  if (/^\s*---|%%\s*\{|@\s*\{|<\/?[a-z!]|\b(?:img|image|icon)["']?\s*:|\b(?:style|classDef|linkStyle)\s|\burl\s*\(|@import|\\/im.test(source)) {
    throw new Error('Diagram configuration, HTML, custom styles and resources are disabled')
  }
}

/** Defense in depth: no active content, resource elements, resource-bearing CSS or nonlocal URLs
 * from the returned SVG enters the Chat DOM. Generated CSS stays scoped to
 * this diagram and is parsed separately to discard resource references. */
function sanitizeDiagram(svg: string): string {
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['a', 'image', 'foreignObject', 'script', 'use', 'animate', 'animateMotion', 'animateTransform', 'set'],
    FORBID_ATTR: ['href', 'xlink:href', 'tabindex'],
  })
  const template = document.createElement('template')
  template.innerHTML = clean
  const diagram = template.content.querySelector('svg')
  if (!diagram) throw new Error('No diagram returned')
  // Constructed stylesheets parse CSS without loading @imports. Retain only
  // Mermaid's SVG-scoped style rules and reject resource-bearing declarations.
  // Keeping layout styles matters for labels and non-flowchart diagram types.
  for (const style of diagram.querySelectorAll('style')) {
    const sheet = new CSSStyleSheet()
    sheet.replaceSync(style.textContent || '')
    const rules: string[] = []
    for (const rule of Array.from(sheet.cssRules)) {
      if (!(rule instanceof CSSStyleRule)) continue
      if (!rule.selectorText.split(',').every(selector => selector.trim().startsWith(`#${diagram.id}`))) continue
      for (const property of Array.from(rule.style)) {
        const value = rule.style.getPropertyValue(property)
        if (/url\s*\(|image-set\s*\(|\\/i.test(value)) rule.style.removeProperty(property)
      }
      rules.push(rule.cssText)
    }
    style.textContent = rules.join('\n')
  }
  for (const element of [diagram, ...diagram.querySelectorAll<SVGElement>('*')]) {
    for (const property of Array.from(element.style)) {
      if (/url\s*\(|image-set\s*\(|\\/i.test(element.style.getPropertyValue(property))) {
        element.style.removeProperty(property)
      }
    }
    for (const attribute of Array.from(element.attributes)) {
      // Local marker/clip references are required for arrows and chart geometry.
      if (/url\s*\(/i.test(attribute.value) && !/^url\(#[\w-]+\)$/.test(attribute.value)) {
        element.removeAttribute(attribute.name)
      }
    }
  }
  diagram.setAttribute('role', 'img')
  diagram.setAttribute('aria-label', 'Mermaid diagram')
  return diagram.outerHTML
}

export function renderMermaidBlocks(root: HTMLElement, isCurrent: () => boolean): Promise<void> {
  const blocks = Array.from(root.querySelectorAll<HTMLElement>('.markdown-code-block'))
    .filter(block => block.querySelector('code')?.classList.contains('language-mermaid'))
  const theme = getComputedStyle(root)
  const color = (name: string) => theme.getPropertyValue(name).trim()
  const signature = [color('--ch-color-text'), color('--ch-color-canvas')].join(':')
  const run = async () => {
    for (const block of blocks) {
      if (!isCurrent()) return
      const source = block.querySelector('pre code')?.textContent || ''
      const key = `${signature}:${source}`
      if (rendered.get(block) === key) continue
      // Cache errors too: an unrelated streamed paragraph must not retry them.
      try {
        checkSource(source)
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          suppressErrorRendering: true,
          maxTextSize: 50_000,
          maxEdges: 500,
          htmlLabels: false,
          fontFamily: 'sans-serif',
          theme: 'base',
          themeVariables: {
            darkMode: document.documentElement.dataset.theme !== 'light',
            background: color('--ch-color-canvas'),
            primaryColor: color('--ch-color-surface'),
            secondaryColor: color('--ch-color-surface-raised'),
            tertiaryColor: color('--ch-color-surface-soft'),
            textColor: color('--ch-color-text'),
            primaryTextColor: color('--ch-color-text'),
            primaryBorderColor: color('--ch-color-border-hover'),
            lineColor: color('--ch-color-text-muted'),
          },
          flowchart: { htmlLabels: false },
        })
        const id = `hub-mermaid-${++nextId}`
        const { svg } = await mermaid.render(id, source)
        if (!isCurrent() || !root.contains(block)) return
        const diagram = document.createElement('div')
        diagram.className = 'markdown-mermaid-diagram'
        diagram.tabIndex = 0
        diagram.dataset.theme = document.documentElement.dataset.theme || 'dark'
        diagram.setAttribute('role', 'region')
        diagram.setAttribute('aria-label', 'Scrollable Mermaid diagram')
        diagram.innerHTML = sanitizeDiagram(svg)
        const viewBox = diagram.querySelector('svg')?.getAttribute('viewBox')?.split(/\s+/).map(Number)
        if (viewBox?.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0) {
          diagram.style.setProperty('--mermaid-width', `${viewBox[2]}px`)
        }
        block.querySelector('.markdown-mermaid-diagram, .markdown-mermaid-error')?.remove()
        block.append(diagram)
        block.classList.add('mermaid-rendered')
        rendered.set(block, key)
        // bindFunctions is deliberately never called (no Mermaid interaction).
      } catch {
        if (!isCurrent() || !root.contains(block)) return
        const error = document.createElement('div')
        error.className = 'markdown-mermaid-error'
        error.textContent = 'Diagram unavailable. Showing source.'
        error.setAttribute('role', 'status')
        block.querySelector('.markdown-mermaid-diagram, .markdown-mermaid-error')?.remove()
        block.classList.remove('mermaid-rendered')
        block.append(error)
        rendered.set(block, key)
      }
    }
  }
  const result = pending.then(run)
  pending = result.catch(() => {})
  return result
}
