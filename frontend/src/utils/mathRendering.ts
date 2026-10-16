import { ref } from 'vue'
import type { MarkedExtension, Tokens } from 'marked'

/** Only messages containing math request the renderer, styles and local fonts. */
export const mathVersion = ref(0)
let renderToString: typeof import('katex').renderToString | null = null
let loading: Promise<void> | null = null
const MAX_SOURCE_LENGTH = 16_384

export function ensureMathRenderer(): Promise<void> {
  if (renderToString) return Promise.resolve()
  if (!loading) {
    loading = import('./katexRenderer')
      .then((katex) => {
        renderToString = katex.renderToString
        mathVersion.value++
      })
      .catch((error) => {
        loading = null
        throw error
      })
  }
  return loading
}

interface MathToken extends Tokens.Generic {
  type: 'mathBlock' | 'mathInline'
  tex: string
  display: boolean
}

function escaped(source: string, index: number): boolean {
  let count = 0
  while (index > 0 && source[--index] === '\\') count++
  return count % 2 === 1
}

/** Parse before Markdown can turn \\[ / \\( into literal punctuation. */
function readMath(source: string): MathToken | undefined {
  const opener = ['$$', '\\[', '\\(', '$'].find((item) => source.startsWith(item))
  if (!opener) return
  const display = opener === '$$' || opener === '\\['
  const closer = opener === '\\[' ? '\\]' : opener === '\\(' ? '\\)' : opener
  if (opener === '$' && (!source[1] || /\s|\$/.test(source[1]))) return
  let end = source.indexOf(closer, opener.length)
  while (end !== -1 && escaped(source, end)) end = source.indexOf(closer, end + 1)
  if (end === -1) return
  const tex = source.slice(opener.length, end)
  if (!tex.trim()) return
  if (opener === '$') {
    // Conservative dollar rules keep prices, $HOME and escaped dollars literal.
    if (/\n|\r/.test(tex) || /\s/.test(tex[tex.length - 1]) || /[A-Za-z0-9_]/.test(source[end + 1] || '')) return
    // A price must not consume the opening delimiter of a later formula or
    // the dollar inside a code span. Use explicit \\(…\\) for ambiguous text.
    if (tex.includes('`')) return
    // "$5.50 ($\\frac{x}{y}$)" and "$5-$-3$" contain a price followed
    // by a real formula. The numeric prefix ends in an unfinished operator
    // or opening delimiter; do not steal that formula's opening dollar.
    if (/^[+-]?(?:\d|\.\d)/.test(tex) && /[-+*/=,;:([{]$/.test(tex)) return
  }
  return { type: 'mathInline', raw: source.slice(0, end + closer.length), tex, display }
}

function startMath(source: string): number | undefined {
  const candidates = /\$|\\[[(]/g
  const lastDollar = source.lastIndexOf('$')
  const lastBracket = source.lastIndexOf('\\]')
  const lastParen = source.lastIndexOf('\\)')
  for (const match of source.matchAll(candidates)) {
    const index = match.index!
    const lastCloser = match[0] === '$' ? lastDollar : match[0] === '\\[' ? lastBracket : lastParen
    // Incomplete streaming input may contain many openers and no closer.
    // Avoid rescanning the remaining suffix once for each such opener.
    if (index >= lastCloser) continue
    if (!escaped(source, index) && readMath(source.slice(index))) return index
  }
}

function escapeHtml(source: string): string {
  return source.replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]!)
}

function renderMath(token: MathToken): string {
  let html = escapeHtml(token.raw)
  let fallback = true
  if (renderToString && token.tex.length <= MAX_SOURCE_LENGTH) {
    try {
      html = renderToString(token.tex, {
        displayMode: token.display,
        output: 'htmlAndMathml',
        throwOnError: true,
        trust: false,
        strict: 'ignore',
        maxExpand: 1_000,
        maxSize: 20,
        // A fresh macro scope prevents one message changing later formulas.
        macros: {},
      })
      fallback = false
    } catch {
      // Incomplete/unsupported formulas remain readable and copyable as source.
    }
  }
  const tag = token.type === 'mathBlock' ? 'div' : 'span'
  const classes = `markdown-math${token.display ? ' markdown-math-display' : ''}${fallback ? ' math-source' : ''}`
  const scroll = token.display ? ' tabindex="0"' : ''
  return `<${tag} class="${classes}"${scroll}>${html}</${tag}>`
}

/** A private Marked instance owns these extensions; the global parser is untouched. */
export const mathExtension: MarkedExtension = {
  extensions: [
    {
      name: 'mathBlock',
      level: 'block',
      start: (source) => source.match(/(?:^|\n)[ \t]*(?:\$\$|\\\[)/)?.index,
      tokenizer(source) {
        const token = readMath(source)
        if (!token?.display) return
        const tail = source.slice(token.raw.length)
        if (!/^[ \t]*(?:\n|$)/.test(tail)) return
        return { ...token, type: 'mathBlock' }
      },
      renderer: (token) => renderMath(token as MathToken),
    },
    {
      name: 'mathInline',
      level: 'inline',
      start: startMath,
      tokenizer: readMath,
      renderer: (token) => renderMath(token as MathToken),
    },
  ],
}
