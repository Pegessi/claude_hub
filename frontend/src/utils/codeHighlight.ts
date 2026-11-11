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

/** Language of a file path, for surfaces that show code they did not author.
 *
 *  A diff row has no info string — the only language hint is the file name —
 *  so the extension is the contract here. Unknown or extension-less paths
 *  return ``''``, which every caller must treat as "render as plain text" rather
 *  than guessing. */
export function languageForPath(path: string): string {
  const name = (path || '').split(/[?#]/)[0].split('/').pop() ?? ''
  const raw = name.includes('.') ? name.split('.').pop() ?? '' : ''
  if (!raw) return ''
  const candidate = normalizeLang(raw)
  return LANG_ALIASES[raw.toLowerCase()] ?? candidate
}

/** Display name for a language identifier: ``python`` -> ``Python``.
 *
 *  Shown as a chip on a code block header, where the fence's ``py`` has to read
 *  as the language a reader recognises, not as the info-string they typed. */
const LANGUAGE_LABELS: Record<string, string> = {
  javascript: 'JavaScript',
  typescript: 'TypeScript',
  python: 'Python',
  bash: 'Bash',
  shell: 'Bash',
  json: 'JSON',
  yaml: 'YAML',
  toml: 'TOML',
  xml: 'XML',
  html: 'HTML',
  vue: 'Vue',
  svg: 'SVG',
  css: 'CSS',
  scss: 'SCSS',
  less: 'LESS',
  markdown: 'Markdown',
  sql: 'SQL',
  go: 'Go',
  rust: 'Rust',
  java: 'Java',
  kotlin: 'Kotlin',
  swift: 'Swift',
  ruby: 'Ruby',
  php: 'PHP',
  cpp: 'C++',
  c: 'C',
  csharp: 'C#',
  objectivec: 'Objective-C',
  protobuf: 'Protobuf',
  graphql: 'GraphQL',
  dockerfile: 'Dockerfile',
  nginx: 'Nginx',
  diff: 'Diff',
  plaintext: 'Text',
}

/** Whether ``lang`` is a language we can name without loading the highlighter.
 *
 *  A label is cheap and needs no chunk (`py` → `Python`), while whether the rows
 *  can actually be *coloured* depends on the highlighter being loaded. Surfaces
 *  that intentionally render plain text (a diff over the size ceiling) still want
 *  the name — and must not subscribe to the chunk just to get it. */
export function isKnownLanguage(lang: string): boolean {
  const token = (lang || '').toLowerCase().trim()
  if (!token) return false
  if (Object.prototype.hasOwnProperty.call(LANGUAGE_LABELS, token)) return true
  return Object.prototype.hasOwnProperty.call(LANGUAGE_LABELS, normalizeLang(token))
}

export function languageLabel(lang: string): string {
  const token = (lang || '').toLowerCase().trim()
  if (!token) return ''
  // Several tokens share one grammar (``vue``/``svg``/``xml`` all highlight as
  // XML), so the label reads the token the fence or file actually named: a
  // ``.vue`` file is not an HTML file, even though the same highlighter paints it.
  const own = LANGUAGE_LABELS[token]
  if (own) return own
  const canonical = normalizeLang(token)
  return LANGUAGE_LABELS[canonical] ?? canonical.replace(/^./, first => first.toUpperCase())
}

/** Highlight ONE line of code for an inline surface (a diff row).
 *
 *  ``highlightCode`` is built for fenced blocks where the whole snippet is at
 *  hand. A diff row is a single line lifted out of its file, so it is
 *  highlighted on its own: grammar state from the surrounding lines is missing,
 *  which costs precision inside multi-line strings and block comments, but every
 *  row stays independent — no span can cross a row boundary and swallow the
 *  next row's tint or line number.
 *
 *  Returns HTML-escaped text when the highlighter has not loaded or the
 *  language is unknown, exactly like ``highlightCode``. */
export function highlightLine(text: string, lang: string): string {
  if (!text || !hljsInstance) return escapeHtml(text)
  const normalized = normalizeLang(lang || '')
  if (!normalized || !hljsInstance.getLanguage(normalized)) return escapeHtml(text)
  try {
    return hljsInstance.highlight(text, { language: normalized, ignoreIllegals: true }).value
  } catch {
    return escapeHtml(text)
  }
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
