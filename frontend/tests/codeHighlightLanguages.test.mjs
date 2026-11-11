import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// ── Load the real codeHighlight.ts with a vue `ref` stub ──────────────────
// It is loaded with a stub so the helpers can be exercised in Node; the
// highlight.js chunk is imported dynamically and stays unloaded, which also
// covers the graceful-degradation path every caller must survive.
const source = await readFile(new URL('../src/utils/codeHighlight.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
globalThis.__vueRef = value => ({ value })
const mocked = outputText
  .replace(/import \{ ref \} from ['"]vue['"];?/, 'const ref = globalThis.__vueRef;')
  .replace(/import type [^\n]+/g, '')
const hl = await import(`data:text/javascript;base64,${Buffer.from(mocked).toString('base64')}`)

test('a diff row takes its language from the file it came from', () => {
  assert.equal(hl.languageForPath('xperf_evo/configs/accelerator_config.py'), 'python')
  assert.equal(hl.languageForPath('frontend/src/utils/codeHighlight.ts'), 'typescript')
  assert.equal(hl.languageForPath('scripts/verify.sh'), 'bash')
  assert.equal(hl.languageForPath('deploy.yaml'), 'yaml')
  assert.equal(hl.languageForPath('Cargo.toml'), 'toml')
  // Ambiguous and unknown extensions stay empty: a caller must render plain
  // text rather than guess a grammar and paint the wrong tokens.
  assert.equal(hl.languageForPath('notes.md'), 'markdown')
  assert.equal(hl.languageForPath('run'), '', 'no extension, no guess')
  assert.equal(hl.languageForPath('Makefile'), '', 'a bare name is not a language')
  // An unknown extension comes back as its own token rather than as a guess:
  // callers ask the highlighter whether it exists and fall back to plain text,
  // which is why `highlightLine` can be called with anything.
  assert.equal(hl.languageForPath('archive.log'), 'log')
  assert.equal(hl.isLanguageRegistered('log'), false, 'and the highlighter rejects it')
  // A path with a query string or fragment still resolves the file name.
  assert.equal(hl.languageForPath('/tmp/a.py?line=3'), 'python')
})

test('a language reads as a chip label a person recognises', () => {
  assert.equal(hl.languageLabel('py'), 'Python')
  assert.equal(hl.languageLabel('ts'), 'TypeScript')
  assert.equal(hl.languageLabel('cpp'), 'C++')
  assert.equal(hl.languageLabel('html'), 'HTML')
  assert.equal(hl.languageLabel(''), '')
  assert.equal(hl.languageLabel('zig'), 'Zig', 'an unmapped language still reads as itself, capitalised')
})

test('an unloaded highlighter degrades to escaped text, never raw markup', () => {
  // This is the pre-load state real users hit on first paint: the helpers must
  // return escaped HTML, so a diff row can never inject markup.
  assert.equal(hl.highlightLine('<script>alert(1)</script>', 'python'), '&lt;script&gt;alert(1)&lt;/script&gt;')
  assert.equal(hl.highlightLine('x = a & b', 'python'), 'x = a &amp; b')
  assert.equal(hl.highlightLine('', 'python'), '')
})
