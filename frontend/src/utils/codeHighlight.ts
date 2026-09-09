/**
 * Lazy-loaded syntax highlighting for markdown code blocks.
 *
 * highlight.js is loaded on demand via dynamic ``import()`` so it never
 * contributes to the first-load bundle.  The ``common`` bundle ships ~40
 * of the most frequently used languages (JavaScript, TypeScript, Python,
 * Bash, JSON, YAML, CSS, XML, SQL, Go, Rust, Java, C/C++, Diff, …) —
 * enough for the vast majority of code blocks without the weight of the
 * full 190+ language distribution.  (Some languages people often reach
 * for — Dockerfile, HCL/Terraform — are NOT in ``common``; info strings
 * for those degrade gracefully to plain text.)
 *
 * Rendering flow
 * --------------
 * 1. Code blocks are rendered *before* the highlighter finishes loading.
 *    ``highlightCode`` returns HTML-escaped plain text (no spans).
 * 2. ``ensureHighlighter()`` is called (from ``MarkdownContent.onMounted``).
 * 3. When the chunk loads, ``highlightReady`` flips to ``true`` and
 *    ``highlightVersion`` is bumped.
 * 4. Components that depend on ``highlightReady`` re-render; the
 *    ``MarkdownBlockCache`` is invalidated (its version guard detects the
 *    bump) and code blocks are re-rendered with real highlighting.
 *
 * DOMPurify compatibility
 * ------------------------
 * highlight.js emits ``<span class="hljs-…">`` elements.  DOMPurify's
 * default config allows ``span`` with ``class`` attributes, so the
 * highlighted markup survives sanitization unchanged.
 */
import { ref } from 'vue'
import type { HLJSApi } from 'highlight.js'

/** The ready-to-use hljs API (core + common languages registered). */
type Hljs = HLJSApi

let hljsInstance: Hljs | null = null
let loadPromise: Promise<Hljs> | null = null

/** Reactive flag — ``true`` once the highlighter chunk has loaded. */
export const highlightReady = ref(false)

/**
 * Monotonically increasing version counter.  Bumped exactly once (when
 * the highlighter finishes loading) so render caches can detect the
 * transition and invalidate.
 */
export const highlightVersion = ref(0)

/**
 * Load the highlighter chunk.  Idempotent — safe to call from every
 * ``MarkdownContent`` instance; only the first call triggers the
 * dynamic import.
 */
export function ensureHighlighter(): Promise<Hljs> {
  if (hljsInstance) return Promise.resolve(hljsInstance)
  if (!loadPromise) {
    loadPromise = import('highlight.js/lib/common').then((mod) => {
      hljsInstance = mod.default as HLJSApi
      highlightReady.value = true
      highlightVersion.value++
      return hljsInstance
    }).catch((err) => {
      // Reset so the NEXT call retries the dynamic import.  Without this a
      // single failed chunk load would leave ``loadPromise`` permanently
      // rejected and wedge the highlighter forever (no retry, and every
      // caller inherits the same rejection).
      loadPromise = null
      hljsInstance = null
      throw err
    })
  }
  return loadPromise
}

// ---------------------------------------------------------------------------
// Language alias normalisation
// ---------------------------------------------------------------------------

/** Map common language aliases to the canonical highlight.js name. */
const LANG_ALIASES: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  python3: 'python',
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  bashrc: 'bash',
  yml: 'yaml',
  html: 'xml',
  htm: 'xml',
  vue: 'xml',
  svg: 'xml',
  md: 'markdown',
  'c++': 'cpp',
  cc: 'cpp',
  'c#': 'csharp',
  cs: 'csharp',
  golang: 'go',
  rs: 'rust',
  gql: 'graphql',
  'objective-c': 'objectivec',
  objc: 'objectivec',
  'objective-c++': 'objectivec',
  plain: 'plaintext',
  text: 'plaintext',
  txt: 'plaintext',
}

/** Normalise a language identifier (from the fenced-code info string) to
 *  the canonical highlight.js language name. */
function normalizeLang(lang: string): string {
  const l = lang.toLowerCase().trim()
  return LANG_ALIASES[l] ?? l
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Highlight ``code`` written in ``lang``.
 *
 * Returns an HTML string of ``<span class="hljs-…">`` elements.  If the
 * highlighter has not loaded yet, or the language is not registered,
 * returns HTML-escaped plain text (graceful degradation).
 */
export function highlightCode(code: string, lang: string): string {
  if (!hljsInstance) return escapeHtml(code)
  const normalized = normalizeLang(lang)
  if (!hljsInstance.getLanguage(normalized)) {
    return escapeHtml(code)
  }
  try {
    return hljsInstance.highlight(code, {
      language: normalized,
      ignoreIllegals: true,
    }).value
  } catch {
    return escapeHtml(code)
  }
}

/**
 * Build the full ``<pre><code>`` HTML for a fenced code block.
 *
 * This replaces marked's own code-block renderer so that highlighting is
 * applied *before* sanitisation (the hljs spans are part of the HTML
 * string that DOMPurify sees, rather than being injected after).
 *
 * @param text - the raw code text (already unescaped by marked).
 * @param lang - the info-string language (may be empty).
 */
export function renderCodeBlockHtml(text: string, lang: string): string {
  // marked extracts only the FIRST non-space word of the info string for the
  // ``language-…`` class — its renderer does ``(infoString || '').match(/^\S*/)?.[0]``.
  // Match that here so multi-word info strings (e.g. ```js title=foo) produce
  // ``class="language-js"`` and highlight as JavaScript rather than failing to
  // normalise the full "js title=foo" and never highlighting.
  const firstWord = (lang || '').match(/^\S*/)?.[0] || ''
  const langAttr = firstWord ? ` class="language-${escapeAttr(firstWord)}"` : ''
  // Match marked's own code renderer: normalise the code to end with exactly
  // one trailing newline and emit a newline after ``</pre>`` so the joined
  // block HTML is byte-identical to ``marked.parse`` for the same source.
  const normalized = text.replace(/\n$/, '') + '\n'
  return `<pre><code${langAttr}>${highlightCode(normalized, firstWord)}</code></pre>\n`
}

/** ``true`` if ``lang`` is registered with the loaded highlighter. */
export function isLanguageRegistered(lang: string): boolean {
  if (!hljsInstance) return false
  return !!hljsInstance.getLanguage(normalizeLang(lang))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    // marked escapes apostrophes as the DECIMAL entity ``&#39;`` (not the hex
    // ``&#x27;``); emit the same so the fallback path byte-matches marked.parse.
    .replace(/'/g, '&#39;')
}

function escapeAttr(s: string): string {
  return escapeHtml(s)
}
