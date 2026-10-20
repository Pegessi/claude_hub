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
}

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

/** Directive with a usable local path, or null for anything else. */
export function parseAgentDirective(name: string, payload: string): AgentDirective | null {
  const args = parsePayload(payload)
  const path = typeof args?.path === 'string' ? args.path.trim() : ''
  if (!path || path.length > 4096) return null
  if ([...path].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) return null
  return { name: name.trim(), path }
}

/** Card markup for a directive. Raw HTML is used so the card keeps a class
 *  through Markdown; it is still sanitized and its href is rewritten to the
 *  scoped attachment preview like any other local link. */
function directiveCard(directive: AgentDirective): string {
  const file = directive.path.split('/').filter(Boolean).pop() || directive.path
  const label = LABELS[directive.name.toLowerCase()] ?? directive.name
  return `\n\n<a class="agent-directive-card" href="${escapeHtml(encodeURI(directive.path))}">`
    + `<span class="agent-directive-label">${escapeHtml(label)}</span>`
    + `<span class="agent-directive-file">${escapeHtml(file)}</span></a>\n\n`
}

/**
 * Replace complete directives with cards, hide a directive that is still
 * streaming in, and remove any stray marker characters.
 */
export function renderAgentDirectives(text: string): string {
  if (!text || !/[\uE200-\uE202]/.test(text)) return text
  let output = text.replace(DIRECTIVE, (_match, name: string, payload: string) => {
    const directive = parseAgentDirective(name, payload)
    return directive ? directiveCard(directive) : ''
  })
  const open = output.lastIndexOf(OPEN)
  if (open >= 0 && output.indexOf(CLOSE, open) < 0) output = output.slice(0, open)
  return output.replace(STRAY_MARKERS, '')
}

/** Plain-text form for copying: a directive becomes its file path. */
export function plainAgentDirectives(text: string): string {
  if (!text || !/[\uE200-\uE202]/.test(text)) return text
  return text
    .replace(DIRECTIVE, (_match, name: string, payload: string) => parseAgentDirective(name, payload)?.path ?? '')
    .replace(new RegExp(`${OPEN}[^${CLOSE}]*$`), '')
    .replace(STRAY_MARKERS, '')
}

