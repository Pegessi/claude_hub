import { attachmentKind } from './markdownPreview'

/**
 * Codex inline directives in assistant text.
 *
 * Codex writes rich-content requests inline, delimited by private-use
 * characters: ``U+E200 name U+E202 {json} U+E201`` (e.g. the visualize skill's
 * ``visualize`` directive with ``{"path": "…/chart.html", "mode": "wide"}``).
 * The Codex app renders them as widgets; left alone they leak into the Chat as
 * tofu glyphs and raw JSON. A directive that points at a local file becomes a
 * card linking to the in-page attachment preview; anything else is dropped.
 */

const OPEN = '\uE200'
const CLOSE = '\uE201'
const DIRECTIVE = /\uE200([^\uE200-\uE202]{1,64})\uE202([^\uE201]*)\uE201/g
const STRAY_MARKERS = /[\uE200-\uE202]/g

const LABELS: Record<string, string> = {
  visualize: 'Open visualization',
}

export interface AgentDirective {
  name: string
  path: string
  title: string
}

export interface DirectiveRenderOptions {
  /** The text is final: an unclosed directive is malformed, not still
   *  streaming, so its text is kept (markers stripped) instead of hidden. */
  complete?: boolean
}

const TITLE_LIMIT = 80
const FENCE = /^ {0,3}(```|~~~)[^\n]*\n[\s\S]*?(?:^ {0,3}\1[^\n]*$|(?![\s\S]))/gm

function parsePayload(raw: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(raw)
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
  } catch {
    return null
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Directive with an absolute local path the in-page preview can open, or
 *  null for anything else (relative, ``~``, scheme URLs, unknown types). */
export function parseAgentDirective(name: string, payload: string): AgentDirective | null {
  const args = parsePayload(payload)
  const path = typeof args?.path === 'string' ? args.path.trim() : ''
  if (!path.startsWith('/') || path.startsWith('//') || path.length > 4096) return null
  if ([...path].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return null
  if (!attachmentKind(path) || attachmentKind(path) === 'unsupported') return null
  const rawTitle = typeof args?.title === 'string' ? args.title.replace(/\s+/g, ' ').trim() : ''
  const title = rawTitle.length > TITLE_LIMIT ? `${rawTitle.slice(0, TITLE_LIMIT - 1)}…` : rawTitle
  return { name: name.trim(), path, title }
}

/** Card markup for a directive. Raw HTML is used so the card keeps a class
 *  through Markdown; it is still sanitized and its href is rewritten to the
 *  scoped attachment preview like any other local link. Each segment is
 *  percent-encoded so ``#`` and ``?`` in a file name survive the round trip. */
function directiveCard(directive: AgentDirective): string {
  const file = directive.path.split('/').filter(Boolean).pop() || directive.path
  const label = directive.title || (LABELS[directive.name.toLowerCase()] ?? directive.name)
  const href = directive.path.split('/').map(encodeURIComponent).join('/')
  return `\n\n<a class="agent-directive-card" href="${escapeHtml(href)}">`
    + `<span class="agent-directive-label">${escapeHtml(label)}</span>`
    + `<span class="agent-directive-file">${escapeHtml(file)}</span></a>\n\n`
}

/** Apply ``transform`` outside fenced code blocks only: a directive quoted in
 *  code is documentation, shown as readable text without its markers. */
function outsideFences(text: string, transform: (prose: string) => string): string {
  let output = ''
  let last = 0
  for (const match of text.matchAll(FENCE)) {
    output += transform(text.slice(last, match.index)) + match[0].replace(STRAY_MARKERS, '')
    last = match.index + match[0].length
  }
  return output + transform(text.slice(last))
}

/**
 * Replace complete directives with cards, hide a directive that is still
 * streaming in, and remove any stray marker characters.
 */
export function renderAgentDirectives(text: string, options: DirectiveRenderOptions = {}): string {
  if (!text || !/[\uE200-\uE202]/.test(text)) return text
  // Only the tail can still be streaming; hide it before splitting on fences.
  let source = text
  const open = source.lastIndexOf(OPEN)
  if (!options.complete && open >= 0 && source.indexOf(CLOSE, open) < 0) source = source.slice(0, open)
  return outsideFences(source, prose => prose
    .replace(DIRECTIVE, (_match, name: string, payload: string) => {
      const directive = parseAgentDirective(name, payload)
      return directive ? directiveCard(directive) : ''
    })
    .replace(STRAY_MARKERS, ''))
}

/** Plain-text form for copying: a directive becomes its file path. */
export function plainAgentDirectives(text: string): string {
  if (!text || !/[\uE200-\uE202]/.test(text)) return text
  return outsideFences(text, prose => prose
    .replace(DIRECTIVE, (_match, name: string, payload: string) => parseAgentDirective(name, payload)?.path ?? '')
    .replace(STRAY_MARKERS, ''))
}
