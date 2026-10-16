import { ref } from 'vue'
import type { MarkedExtension, Tokens } from 'marked'

/** Only messages containing math request the renderer, styles and local fonts. */
export const mathVersion = ref(0)
let renderToString: typeof import('katex').renderToString | null = null
let loading: Promise<void> | null = null
const MAX_SOURCE_LENGTH = 16_384
const dollarTextRules = new WeakMap<RegExp, RegExp>()

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

function findUnescaped(source: string, delimiter: string, start: number): number {
  let index = source.indexOf(delimiter, start)
  while (index !== -1 && escaped(source, index)) index = source.indexOf(delimiter, index + 1)
  return index
}

/** Parse before Markdown can turn \\[ / \\( into literal punctuation. */
function readMath(source: string): MathToken | undefined {
  const opener = ['$$', '\\[', '\\(', '$'].find((item) => source.startsWith(item))
  if (!opener) return
  const display = opener === '$$' || opener === '\\['
  const closer = opener === '\\[' ? '\\]' : opener === '\\(' ? '\\)' : opener
  if (opener === '$' && (!source[1] || /\s|\$/.test(source[1]))) return
  // A new unescaped \\( / \\[ cannot be nested inside the same delimiter.
  // Stop at that opener instead of rescanning an arbitrarily long suffix for
  // every incomplete formula. Escaped row-spacing \\\\[ remains valid TeX.
  const nested = opener[0] === '\\' ? findUnescaped(source, opener, opener.length) : -1
  const candidate = nested === -1 ? source : source.slice(0, nested)
  const end = findUnescaped(candidate, closer, opener.length)
  if (end === -1) return
  const tex = source.slice(opener.length, end)
  if (!tex.trim()) return
  if (opener === '$') {
    // Conservative dollar rules keep prices, $HOME and escaped dollars literal.
    if (/\n|\r/.test(tex) || /\s/.test(tex[tex.length - 1]) || /[A-Za-z0-9_{\\]/.test(source[end + 1] || '')) return
    // A price must not consume the opening delimiter of a later formula or
    // the dollar inside a code span. Use explicit \\(…\\) for ambiguous text.
    if (tex.includes('`') || (tex.includes('"') && !/\\(?:text|mbox)\s*\{/.test(tex))) return
    // "$5.50 ($\\frac{x}{y}$)" and "$5-$-3$" contain a price followed
    // by a real formula. The numeric prefix ends in an unfinished operator
    // or opening delimiter; do not steal that formula's opening dollar.
    if (/^[+-]?(?:\d|\.\d)/.test(tex) && !/[\p{L}\p{N}}\])!%|'′″]$/u.test(tex)) return
  }
  return { type: 'mathInline', raw: source.slice(0, end + closer.length), tex, display }
}

function readBlockMath(source: string): MathToken | undefined {
  const token = readMath(source)
  if (!token?.display || !/^[ \t]*(?:\n|$)/.test(source.slice(token.raw.length))) return
  return { ...token, type: 'mathBlock' }
}

function startBlockMath(source: string): number | undefined {
  for (const match of source.matchAll(/(?:^|\n)[ \t]*(\$\$|\\\[)/g)) {
    const index = match.index! + match[0].length - match[1].length
    if (readBlockMath(source.slice(index))) return index
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
  tokenizer: {
    inlineText(source) {
      // Keep Marked's native boundaries (code, emphasis, links, line breaks),
      // adding '$' to its stop lookaheads. Matching the full native suffix and
      // THEN cutting at '$' makes many incomplete dollar formulas quadratic.
      // Clone the public rule; never mutate Marked's shared rules or globals.
      const nativeRule = this.rules.inline.text
      let rule = dollarTextRules.get(nativeRule)
      if (!rule) {
        rule = new RegExp(nativeRule.source.replace(/\(\?=/g, '(?=\\$|'), nativeRule.flags)
        dollarTextRules.set(nativeRule, rule)
      }
      const match = rule.exec(source)
      if (!match) return false
      return { type: 'text', raw: match[0], text: match[0], escaped: this.lexer.state.inRawBlock }
    },
  },
  extensions: [
    {
      name: 'mathBlock',
      level: 'block',
      start: startBlockMath,
      tokenizer: readBlockMath,
      renderer: (token) => renderMath(token as MathToken),
    },
    {
      name: 'mathInline',
      level: 'inline',
      tokenizer(source) {
        if (this.lexer.state.inRawBlock) return
        return readMath(source)
      },
      renderer: (token) => renderMath(token as MathToken),
    },
  ],
}
