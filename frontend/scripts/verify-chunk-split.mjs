#!/usr/bin/env node
/* global process */
/**
 * verify-chunk-split.mjs — CI guard for PR-14's agent-config chunk split.
 *
 * Audit finding A11 (round-2 perf audit) requested a build-time guard that
 * fails CI if EnvPresetManager.vue or AgentConfigFields.vue — the two heavy
 * agent-config modals deferred off the initial shell by PR-05/PR-11/PR-14
 * (~33 KB gz win) — either (a) leak their component MODULE CODE into the
 * initial index entry chunk, or (b) lose their dynamic-import trigger so the
 * chunk becomes an eagerly-fetched static dependency of the entry rather
 * than being loaded via defineAsyncComponent when the user opens a modal.
 *
 * Detection strategy (five always-on layers + one bonus manifest layer)
 * ---------------------------------------------
 * Layer 1 (chunk identity, always runs):
 *   Parse dist/index.html for the <script type=module> entry. Assert exactly
 *   one dist/assets/agent-config-*.js lazy chunk exists. This catches a
 *   deleted/renamed manualChunks rule (which would drop the shared chunk
 *   entirely and either split EPM/ACF into separate hash-named chunks or
 *   inline Vue runtime back into the entry, bloating index.js from ~29 KB gz
 *   to ~61 KB gz).
 *
 * Layer 2 (__name markers, always runs — mangling-resilient content check):
 *   Vue's SFC compiler preserves the string literal `__name:"ComponentName"`
 *   inside each compiled SFC module (used by devtools; baked into the
 *   production render setup). We assert:
 *     (a) the entry chunk contains NEITHER `__name:"EnvPresetManager"` nor
 *         `__name:"AgentConfigFields"` — component module code must not live
 *         in the initial bundle, and
 *     (b) the agent-config lazy chunk contains BOTH markers (sanity that
 *         the chunk named "agent-config" is in fact the chunk that holds
 *         the two target components).
 *   We deliberately do NOT grep for bare identifiers ('AgentConfigFields')
 *   — those strings legitimately appear in index.js inside the
 *   defineAsyncComponent dynamic import() glue and __vite__mapDeps CSS
 *   asset lists; would false-positive.
 *
 * Layer 3 (dynamic-import trigger, always runs — catches TabBar reverts):
 *   Requires ≥2 `import("./agent-config-HASH.js").then(` expressions in the
 *   entry chunk (one per defineAsyncComponent site). If a future change
 *   replaces TabBar's defineAsyncComponent wrappers with static imports, the
 *   dynamic sites disappear even though manualChunks still forces the chunk
 *   file to exist.
 *
 * Layer 4 (no eager static SFC import, always runs — catches the "added static
 *   import while TabBar dynamic sites remain" hybrid regression):
 *   Vue SFC compiler emits each compiled SFC as a setup function wrapped with
 *   _withScopeId (Qi/withScopeId) for scoped-CSS, yielding a wrapped-SFC var
 *   (e.g. so=Qi(pf,[["__scopeId","data-v-5a836610"]])). Rollup wraps that in a
 *   Module-namespace object (Object.freeze(Object.defineProperty({__proto__:
 *   null,default:so},Symbol.toStringTag,{value:"Module"}))) for cross-chunk
 *   dynamic-import access. In the HEALTHY split, only that Module-namespace
 *   wrapper is exported from agent-config; the raw wrapped-SFC var stays
 *   internal and is consumed inside the chunk by its own template render.
 *   When an entry-level module adds a static import of an SFC default (e.g.
 *   `import EPM from '@/components/EnvPresetManager.vue'` in App.vue while
 *   TabBar's defineAsyncComponent stays), Rollup adds a DIRECT export of the
 *   wrapped-SFC var itself to satisfy the static import binding — the
 *   namespace wrapper alone won't do, because a static `import X from '...'`
 *   expects the default export, not a namespace interop object. We detect
 *   this by (a) tracing each __name marker to its wrapped-SFC var, (b)
 *   parsing the export block of agent-config, and (c) asserting that each
 *   wrapped-SFC var is NOT directly present in the export list (it may only
 *   be referenced transitively via the Module-namespace wrapper).
 *
 * Layer 6 (no static facade edge, always runs — catches the RS-01 shared-runtime
 *   facade blind spot):
 *   Layers 3/4 only guard the two SFC DEFAULTS. They stay green when the entry
 *   statically re-imports shared runtime/helper symbols FROM agent-config via a
 *   cross-chunk facade (`import{…51 symbols…}from"./agent-config-*.js"`). That
 *   facade appears when Vue runtime + the plugin-vue export-helper + a shared
 *   composable (useLaunchEnvPresets) are statically imported by BOTH the entry
 *   SFCs and the agent-config SFCs and no dedicated vendor chunk exists, so
 *   Rollup parks them in agent-config and pins the chunk (and its CSS <link>)
 *   to the initial static payload. We assert the entry chunk contains NO static
 *   `…from"./agent-config-*.js"` re-export/import and NO bare side-effect
 *   `import"./agent-config-*.js"`; the healthy dynamic trigger
 *   `import("./agent-config-*.js").then(` has no `from` clause and is not
 *   matched. The RS-01 fix routes those shared modules into a `vendor` chunk,
 *   which removes the facade — so post-fix the healthy split legitimately has
 *   no static agent-config edge and this layer stays green.
 *
 * Layer 5 (manifest cross-check, bonus when --manifest was used):
 *   When dist/.vite/manifest.json exists (vite build --manifest), we add:
 *     (a) the agent-config chunk is flagged `isDynamicEntry: true`,
 *     (b) the entry (index.html) lists the agent-config chunk key in its
 *         `dynamicImports` array (i.e. there is at least one dynamic import
 *         edge from entry → agent-config, matching Layer 3's JS check),
 *     (c) any per-SFC source entries for EPM/ACF map to the same
 *         agent-config file (catches partial chunk duplication).
 *
 * Why not simpler checks
 * ----------------------
 *   * "No `import ... from './agent-config-*.js'` at top of index.js" USED to be
 *     a non-starter: pre-RS-01, Rollup emitted a cross-chunk re-export facade
 *     for shared Vue/runtime helpers that looked like a static import at the top
 *     level but carried no component code (the helpers lived in agent-config
 *     because manualChunks forced Vue+EPM+ACF together). RS-01 removed that
 *     facade by routing the shared runtime/helper/composable modules into a
 *     dedicated `vendor` chunk; the entry now re-imports those symbols from
 *     `vendor` (legitimately critical-path) and agent-config has NO static edge.
 *     Layer 6 therefore now DOES flag any static `…from"./agent-config-*.js"`
 *     facade as the RS-01 regression — while still ignoring the dynamic
 *     `import("./agent-config-*.js").then(` trigger, which has no `from` clause.
 *   * "Bare 'AgentConfigFields' string not in index.js" still false-positives
 *     on the legitimate dynamic import() glue and CSS asset paths.
 *   * "agent-config-*.js file exists" alone is insufficient — the file
 *     exists even when the lazy trigger is removed (Vue re-export facade
 *     keeps it as a static dep).
 *   * "Dynamic import().then() count ≥ 2" alone (v2) is also insufficient —
 *     a newly-added static import in a sibling entry module (App.vue,
 *     LayoutSelector.vue, …) adds an eager static edge while leaving TabBar's
 *     defineAsyncComponent dynamic sites intact, so Layer 3 still sees 2
 *     dynamic sites and false-passes (v2's blind spot, fixed by Layer 4).
 *
 * If a future Vue/Vite upgrade strips __name strings from prod output, the
 * positive assertion in Layer 2b fails loudly (exit 1) rather than silently
 * passing; if the dynamic-import syntactic shape changes (e.g. Rollup stops
 * emitting the literal `import("./chunk-HASH.js")` form), Layer 3 also fails
 * loudly; if the _withScopeId wrapping shape for SFC defaults changes (e.g.
 * Vue stops emitting the IDENT=HELPER(setup,[["__scopeId"… pattern), Layer
 * 4 fails loudly. All three failures prompt a script update rather than a
 * missed regression.
 *
 * Usage
 * -----
 *   node scripts/verify-chunk-split.mjs [--dist <path>]
 *
 * Exits 0 on success, 1 on regression.
 */

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const TARGET_COMPONENTS = ['EnvPresetManager', 'AgentConfigFields']
const TARGET_MODULE_SUBSTRINGS = [
  '/components/EnvPresetManager.vue',
  '/components/AgentConfigFields.vue',
]

const __dirname = dirname(fileURLToPath(import.meta.url))

function parseArgs(argv) {
  const args = { dist: null }
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--dist' && argv[i + 1]) {
      args.dist = argv[++i]
    }
  }
  return args
}

function findDist() {
  const candidates = [
    resolve(__dirname, '..', 'dist'), // script lives in frontend/scripts/
    resolve(process.cwd(), 'dist'),
    resolve(process.cwd(), 'frontend', 'dist'),
  ]
  for (const c of candidates) {
    try {
      readdirSync(resolve(c, 'assets'))
      return c
    } catch {
      // try next
    }
  }
  return null
}

let failures = []
function fail(msg) {
  failures.push(msg)
}
function ok(msg) {
  console.log(`\x1b[32m✓\x1b[0m ${msg}`)
}
function info(msg) {
  console.log(`  \x1b[90m${msg}\x1b[0m`)
}

function findEntryScript(html) {
  const m = html.match(/<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']*\/assets\/[^"']+\.js)["'][^>]*>/i)
  if (!m) fail('Could not locate <script type="module" src=".../assets/*.js"> in dist/index.html')
  return m ? m[1].replace(/^\//, '') : null
}

function findLazyChunks(distAssetsDir) {
  return readdirSync(distAssetsDir).filter((f) => /^agent-config-.*\.js$/.test(f))
}

// ---------------------------------------------------------------------------
// Layer 5 — manifest cross-check (when --manifest was used)
// ---------------------------------------------------------------------------
function checkManifest(distDir, acHash) {
  const manifestPath = resolve(distDir, '.vite', 'manifest.json')
  if (!existsSync(manifestPath)) {
    info('(manifest layer skipped — build without --manifest; markers + dynamic-import layers cover us)')
    return 'skipped'
  }
  let manifest
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  } catch (e) {
    fail(`Cannot parse ${manifestPath}: ${e.message}`)
    return 'failed'
  }

  // Find the agent-config chunk entry.
  const agentConfigKeys = Object.entries(manifest).filter(
    ([, v]) => v.name === 'agent-config' && typeof v.file === 'string' && v.file.startsWith('assets/agent-config-') && v.file.endsWith('.js'),
  )
  if (agentConfigKeys.length === 0) {
    fail('Manifest has no chunk named "agent-config" — manualChunks rule likely removed or renamed.')
    return 'failed'
  }
  if (agentConfigKeys.length > 1) {
    fail(`Manifest has ${agentConfigKeys.length} chunks named "agent-config" (${agentConfigKeys.map(([k]) => k).join(', ')}); expected exactly one.`)
    return 'failed'
  }
  const [acKey, acEntry] = agentConfigKeys[0]

  // Verify chunk file hash matches what we found on disk (sanity).
  if (acEntry.file !== `assets/${acHash}`) {
    fail(`Manifest agent-config file (${acEntry.file}) does not match on-disk chunk (assets/${acHash}).`)
    return 'failed'
  }

  // Must be a dynamic entry (isDynamicEntry), not a static entry.
  if (!acEntry.isDynamicEntry) {
    fail(`Manifest: ${acKey} is not flagged isDynamicEntry (got ${JSON.stringify(acEntry.isDynamicEntry)}).`)
    return 'failed'
  }
  if (acEntry.isEntry) {
    fail(`Manifest: ${acKey} is marked isEntry — it must NOT be a static entry point.`)
    return 'failed'
  }

  // Entry (index.html) must list the agent-config chunk in its dynamicImports.
  // This is the manifest-level equivalent of Layer 3: if ALL entry-side
  // defineAsyncComponent calls are reverted to static imports, the chunk
  // key disappears from dynamicImports and moves to imports only.
  const entry = manifest['index.html']
  if (!entry) {
    fail('Manifest is missing the index.html entry.')
    return 'failed'
  }
  const dyn = Array.isArray(entry.dynamicImports) ? entry.dynamicImports : []
  if (!dyn.includes(acKey)) {
    fail(
      `Manifest: entry (index.html) does NOT list ${acKey} in dynamicImports, but the chunk exists and is isDynamicEntry.\n` +
        `  This means no module in the entry chunk dynamically imports agent-config — all defineAsyncComponent\n` +
        `  wrappers for EnvPresetManager/AgentConfigFields in entry-level code (TabBar.vue) have been replaced\n` +
        `  with static imports. The chunk would be fetched as a static dependency even though the components\n` +
        `  only render behind modal v-if gates. Re-route the imports through defineAsyncComponent to restore\n` +
        `  on-demand loading. (Entry.dynamicImports was: ${JSON.stringify(dyn)})`,
    )
    return 'failed'
  }

  // RS-01 (manifest-level equivalent of Layer 6 / nofacade): the entry must NOT
  // list agent-config in its STATIC `imports` array. Rollup records a chunk in
  // `imports` when the entry statically re-imports symbols from it (the shared
  // Vue-runtime/helper facade). agent-config belongs in `dynamicImports` ONLY;
  // any presence in `imports` means the chunk is on the initial static payload
  // edge. This catches the RS-01 edge even in a source form where the raw JS
  // facade regex might miss it (e.g. a future Rollup output shape change).
  const staticImports = Array.isArray(entry.imports) ? entry.imports : []
  if (staticImports.includes(acKey)) {
    fail(
      `Manifest: entry (index.html) lists ${acKey} in STATIC imports (RS-01 static-payload edge).\n` +
        `  imports = ${JSON.stringify(staticImports)}\n` +
        `  agent-config must appear in entry.dynamicImports ONLY — a static \`imports\` entry means the entry\n` +
        `  chunk statically re-imports shared runtime/helper symbols from agent-config via a cross-chunk facade,\n` +
        `  pinning the chunk (and its CSS) to the initial critical path. Route the shared Vue runtime +\n` +
        `  plugin-vue export-helper + any composable shared between the entry and agent-config SFCs into a\n` +
        `  dedicated \`vendor\` chunk via manualChunks so agent-config stays purely dynamic.`,
    )
    return 'failed'
  }

  // If per-SFC source entries exist for EPM/ACF, they must map to the same file.
  for (const sub of TARGET_MODULE_SUBSTRINGS) {
    const matches = Object.entries(manifest).filter(([, v]) => v.src && v.src.includes(sub) && v.file && v.file.endsWith('.js'))
    if (matches.length === 0) continue // merged into the agent-config chunk (expected case)
    for (const [, v] of matches) {
      if (v.file !== acEntry.file) {
        fail(
          `Manifest: ${v.src} maps to ${v.file}, NOT to the shared agent-config chunk ${acEntry.file}.\n` +
            `  Both target SFCs must be merged into assets/agent-config-*.js by manualChunks.`,
        )
        return 'failed'
      }
    }
  }

  ok(`manifest : ${acKey} → ${acEntry.file} isDynamicEntry=true; in entry.dynamicImports`)
  return 'passed'
}

// ---------------------------------------------------------------------------
// Layers 1+2 — chunk existence + __name markers (always runs)
// ---------------------------------------------------------------------------
function checkChunksAndMarkers(distDir, html) {
  const entryRel = findEntryScript(html)
  if (!entryRel) return { failed: true }
  const entryAbs = resolve(distDir, entryRel)
  let entrySrc
  try {
    entrySrc = readFileSync(entryAbs, 'utf8')
  } catch (e) {
    fail(`Cannot read entry chunk ${entryAbs}: ${e.message}`)
    return { failed: true }
  }

  // Layer 2a: entry chunk must NOT contain component __name markers.
  const leaked = []
  for (const name of TARGET_COMPONENTS) {
    if (entrySrc.includes(`__name:"${name}"`)) leaked.push(name)
  }
  if (leaked.length) {
    fail(
      `Entry chunk (${entryRel}) contains __name markers for: ${leaked.join(', ')}.\n` +
        `  These components must live in the lazy agent-config chunk only. A static import upstream\n` +
        `  of main.ts pulled component code into the initial bundle.`,
    )
    return { failed: true }
  }

  // Layer 1: exactly one agent-config-*.js exists.
  const assetsDir = resolve(distDir, 'assets')
  const lazyChunks = findLazyChunks(assetsDir)
  if (lazyChunks.length === 0) {
    fail(
      `No assets/agent-config-*.js found — manualChunks rule in vite.config.ts is\n` +
        `  supposed to route EnvPresetManager.vue + AgentConfigFields.vue into a shared lazy chunk.\n` +
        `  Either the build failed or the rule was altered/removed.`,
    )
    return { failed: true }
  }
  if (lazyChunks.length > 1) {
    fail(`Multiple assets/agent-config-*.js chunks found: ${lazyChunks.join(', ')}. Expected exactly one.`)
    return { failed: true }
  }

  const lazyName = lazyChunks[0]
  const lazyAbs = resolve(assetsDir, lazyName)
  let lazySrc
  try {
    lazySrc = readFileSync(lazyAbs, 'utf8')
  } catch (e) {
    fail(`Cannot read lazy chunk ${lazyAbs}: ${e.message}`)
    return { failed: true }
  }

  // Layer 2b: lazy chunk must contain both __name markers.
  const missing = []
  for (const name of TARGET_COMPONENTS) {
    if (!lazySrc.includes(`__name:"${name}"`)) missing.push(name)
  }
  if (missing.length) {
    fail(
      `Lazy chunk assets/${lazyName} exists but is missing __name markers for: ${missing.join(', ')}.\n` +
        `  The chunk name is no longer a reliable signal that it contains the target components;\n` +
        `  this guard needs updating, or manualChunks was repurposed.`,
    )
    return { failed: true }
  }

  return { failed: false, entryRel, entryAbs, entrySrc, lazyName, lazyAbs, lazySrc }
}

// ---------------------------------------------------------------------------
// Layer 3 — dynamic-import trigger check (always runs)
// ---------------------------------------------------------------------------
//
// PR-14 converted TabBar.vue's static imports of EnvPresetManager and
// AgentConfigFields (both in the entry chunk) to defineAsyncComponent,
// producing dynamic `import("./agent-config-HASH.js").then(chunk => chunk.X)`
// expressions — one per async component (currently two). The shared
// cross-chunk Vue-runtime re-export facade produces a top-level static
// import{...}from"./agent-config-HASH.js" that carries only helper symbols
// (h/createVNode/defineComponent/etc.), NOT the component default exports,
// and the dynamic-import-then() is what actually resolves the SFC defaults
// at modal-open time. If BOTH defineAsyncComponent calls revert to static
// imports the dynamic import count drops to 0; if ONE reverts the count
// drops to 1. We require at least as many dynamic imports as there are
// target components (2) so both components stay behind an async boundary.
const EXPECTED_ENTRY_DYNAMIC_IMPORTS = TARGET_COMPONENTS.length

function checkDynamicImportTrigger(entryRel, entrySrc, lazyName) {
  const hashMatch = lazyName.match(/^agent-config-(.+)\.js$/)
  if (!hashMatch) {
    fail(`Could not extract hash from lazy chunk filename ${lazyName}.`)
    return 'failed'
  }
  const hash = hashMatch[1]
  // Match both quote styles and tolerate whitespace/parens wrapping.
  // The shape emitted by Rollup for defineAsyncComponent is:
  //   import("./agent-config-<hash>.js").then(<arrow>)
  // We count any import("./agent-config-<hash>.js") that is followed by
  // .then( — this distinguishes the lazy-trigger dynamic imports from any
  // unrelated string literal containing the chunk path (e.g. in __vite__mapDeps
  // asset lists, which are strings, not import() calls).
  const re = new RegExp(
    `import\\(["']\\./agent-config-${escapeRegExp(hash)}\\.js["']\\)\\.then\\(`,
    'g',
  )
  const matches = entrySrc.match(re)
  const count = matches ? matches.length : 0
  if (count < EXPECTED_ENTRY_DYNAMIC_IMPORTS) {
    fail(
      `Entry chunk (${entryRel}) contains only ${count} dynamic import("./agent-config-${hash}.js").then() expression(s) ` +
        `(expected at least ${EXPECTED_ENTRY_DYNAMIC_IMPORTS}, one per async component: ${TARGET_COMPONENTS.join(', ')}).\n` +
        `  At least one defineAsyncComponent wrapper for EnvPresetManager/AgentConfigFields in entry-level code\n` +
        `  (TabBar.vue) has been replaced with a static import. The component default export has moved into the\n` +
        `  static binding list from agent-config; restore defineAsyncComponent(() => import('@/components/<Name>.vue'))\n` +
        `  for all entry-side uses of these components so they resolve through the async boundary at modal-open time.`,
    )
    return 'failed'
  }
  ok(`trigger : entry chunk contains ${count} dynamic import("./agent-config-${hash}.js").then() expression(s) (≥${EXPECTED_ENTRY_DYNAMIC_IMPORTS} expected)`)
  return 'passed'
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ---------------------------------------------------------------------------
// Layer 4 — no eager static dependency on SFC defaults (always runs)
// ---------------------------------------------------------------------------
//
// Walks from each __name:"Component" marker backwards to the setup/defineComponent
// assignment, then forward past the __scopeId wrapping (the _withScopeId call
// that applies data-v-<hash> scoped-CSS attribute), and checks whether the
// resulting wrapped-SFC variable is directly present in the agent-config
// export block.
//
// In the healthy split, the wrapped-SFC var is an internal symbol — it is
// passed to the Module-namespace factory (Object.freeze(Object.defineProperty(
// {__proto__:null,default:wrapped},Symbol.toStringTag,{value:"Module"}))) and
// consumed inside agent-config by the component's own template render calls;
// it does NOT appear as a standalone export. Only the namespace wrapper
// (Uf/Kf/... as $/Z/...) is exported.
//
// Adding a static import of an SFC default from any entry-level code causes
// Rollup to add a direct export of the wrapped-SFC var (because a static
// `import X from '...'` binds X directly to the default export, not to a
// .default property of a namespace interop object). Detecting that extra
// direct export catches the "hybrid" regression where defineAsyncComponent
// remains in TabBar (so Layer 3 still sees 2 dynamic sites) but some other
// entry-level module (App.vue, LayoutSelector.vue, ...) has added a static
// import that pulls the component code into the eager load path.
//
// We look for the __scopeId wrapper by scanning forward from __name for the
// pattern WRAPPED=HELPER(SETUP,[["__scopeId",...]]) — this is the stable shape
// the Vue SFC compiler emits for scoped-style components. If a future Vue
// version changes that shape, this check fails loudly (exit 1) rather than
// silently passing.

function findWrappedSfcVar(lazySrc, name) {
  const marker = `__name:"${name}"`
  const pos = lazySrc.indexOf(marker)
  if (pos < 0) return null
  // Walk backward up to 3000 chars to find the nearest identifier assignment
  // of the form IDENT=FUNC({  which introduces the defineComponent/setup.
  const back = lazySrc.slice(Math.max(0, pos - 3000), pos)
  const assigns = [...back.matchAll(/(?:^|[;,{}\n(])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*\{/g)]
  if (assigns.length === 0) return null
  const setupVar = assigns[assigns.length - 1][1]
  // Walk forward up to 8000 chars to find WRAPPED=HELPER(setupVar,[["__scopeId"
  const fwd = lazySrc.slice(pos, pos + 8000)
  const esc = escapeRegExp(setupVar)
  const wrapRe = new RegExp(`([A-Za-z_$][A-Za-z0-9_$]*)\\s*=\\s*[A-Za-z_$][A-Za-z0-9_$]*\\s*\\(\\s*${esc}\\s*,\\s*\\[\\["__scopeId"`)
  const wm = fwd.match(wrapRe)
  if (!wm) return null
  return { setupVar, wrappedVar: wm[1] }
}

function parseExportBlock(lazySrc) {
  // export{ a as b, c as d, e };
  const m = lazySrc.match(/export\{([^}]*)\}\s*;?\s*$/)
  if (!m) return null
  const internalToExport = new Map() // internal-var -> exported-name
  const exportedToInternal = new Map() // exported-name -> internal-var
  for (const raw of m[1].split(',')) {
    const s = raw.trim()
    if (!s) continue
    const idx = s.indexOf(' as ')
    if (idx >= 0) {
      const orig = s.slice(0, idx)
      const exp = s.slice(idx + 4)
      internalToExport.set(orig, exp)
      exportedToInternal.set(exp, orig)
    } else {
      internalToExport.set(s, s)
      exportedToInternal.set(s, s)
    }
  }
  return { internalToExport, exportedToInternal }
}

function checkNoEagerSfcImport(lazyName, lazySrc) {
  const exports = parseExportBlock(lazySrc)
  if (!exports) {
    fail(`Could not parse terminal export block in assets/${lazyName}.`)
    return 'failed'
  }
  const leaked = []
  for (const name of TARGET_COMPONENTS) {
    const info = findWrappedSfcVar(lazySrc, name)
    if (!info) {
      fail(
        `Could not locate __scopeId-wrapped SFC variable for ${name} in assets/${lazyName}.\n` +
          `  The Vue SFC compiler output shape may have changed; update this guard's Layer 5.`,
      )
      return 'failed'
    }
    const { setupVar, wrappedVar } = info
    // The wrapped var is allowed to be referenced transitively by the
    // Module-namespace wrapper (X=Object.freeze(Object.defineProperty(
    //   {__proto__:null,default:wrappedVar},Symbol.toStringTag,{value:"Module"}))),
    // but it must NOT appear as a standalone export. Detect that by checking
    // whether wrappedVar is a key in internalToExport.
    if (exports.internalToExport.has(wrappedVar)) {
      const directExport = exports.internalToExport.get(wrappedVar)
      leaked.push({ name, wrappedVar, directExport, setupVar })
    }
  }
  if (leaked.length > 0) {
    const lines = leaked.map((l) =>
      `    • ${l.name}: wrapped-SFC var '${l.wrappedVar}' is directly exported as '${l.directExport}'`,
    )
    fail(
      `agent-config chunk assets/${lazyName} directly exports the __scopeId-wrapped SFC defaults for:\n` +
        lines.join('\n') +
        `\n  In the healthy split, only Module-namespace interop objects (Object.freeze({__proto__:null,default:SFC}))\n` +
        `  are exported; a direct export of the wrapped SFC means some entry-level module has added an eager\n` +
        `  static import of that component (e.g. \`import ${leaked[0].name} from '@/components/${leaked[0].name}.vue'\`)\n` +
        `  while leaving TabBar's defineAsyncComponent wrappers in place — Layer 3 (dynamic-import count) stays\n` +
        `  satisfied but the component default is pulled via a static binding and the chunk becomes an eagerly\n` +
        `  fetched dependency of the entry. Route the import through defineAsyncComponent in the entry-level file,\n` +
        `  or hoist the usage out of entry-level code into a dynamic chunk.`,
    )
    return 'failed'
  }
  ok(`nostatic : agent-config does not directly export any target SFC wrapped default (no eager static SFC import)`)
  return 'passed'
}

// ---------------------------------------------------------------------------
// Layer 6 — no static facade edge to agent-config (always runs)
// ---------------------------------------------------------------------------
//
// RS-01 (round-4 perf audit). Layers 3/4 prove the two SFC *defaults* are not
// pulled into the entry via a dynamic-import revert (Layer 3) or a direct
// static SFC import (Layer 4). Neither catches the *shared-runtime facade*
// edge: when Vue runtime + the plugin-vue export-helper + shared app modules
// (e.g. the useLaunchEnvPresets composable) are statically imported by BOTH
// entry-side SFCs AND the agent-config SFCs, and no dedicated vendor chunk
// exists, Rollup co-locates those shared modules INTO agent-config and makes
// the entry statically re-import them with a cross-chunk facade:
//     import{r as C,m as Xt,…51 symbols…}from"./agent-config-<hash>.js"
// That single static edge drags agent-config (and its CSS <link>) onto the
// initial payload even though EnvPresetManager/AgentConfigFields themselves
// are only reached via dynamic import().then(). Layers 3+4 stay green (the two
// dynamic sites are intact and no SFC default is directly exported), so the
// edge sailed past the guard — the RS-01 blind spot.
//
// Detection: a STATIC `…from"./agent-config-<hash>.js"` import/re-export, or a
// bare side-effect `import"./agent-config-<hash>.js"`, in the entry chunk. The
// healthy dynamic trigger is `import("./agent-config-<hash>.js").then(` — a
// *call expression* with NO `from` clause and a `(` immediately after `import`,
// so it is never matched here. Any `from"./agent-config-…"` is necessarily a
// static binding and therefore the RS-01 edge.
//
// NOTE (policy reversal): earlier revisions of this guard deliberately did NOT
// flag this facade, treating it as "present and correct in the healthy split".
// RS-01's fix routes the shared runtime/composable modules into a dedicated
// `vendor` chunk, which removes the agent-config facade entirely (the entry
// then re-imports those symbols from `vendor`, which is legitimately on the
// critical path). So post-RS-01 the healthy split has NO static agent-config
// edge, and this assertion is consistent with it — a static agent-config
// facade now signals a regression, not the expected shape.
function checkNoStaticFacade(entryRel, entrySrc, lazyName) {
  const hashMatch = lazyName.match(/^agent-config-(.+)\.js$/)
  if (!hashMatch) {
    fail(`Could not extract hash from lazy chunk filename ${lazyName} for facade check.`)
    return 'failed'
  }
  const esc = escapeRegExp(hashMatch[1])
  // Static re-export / import facade: `…from"./agent-config-<hash>.js"`.
  // A `from` clause never appears in the dynamic `import("./…").then(` trigger
  // nor in __vite__mapDeps string arrays, so this uniquely identifies a static
  // binding to the chunk.
  const facadeRe = new RegExp(`from\\s*["']\\./agent-config-${esc}\\.js["']`, 'g')
  // Bare side-effect import: `import"./agent-config-<hash>.js"` (no `from`).
  // The immediately-following quote (not `(`) distinguishes it from `import(`.
  const bareRe = new RegExp(`import\\s*["']\\./agent-config-${esc}\\.js["']`, 'g')
  const facadeMatches = entrySrc.match(facadeRe) || []
  const bareMatches = entrySrc.match(bareRe) || []
  const total = facadeMatches.length + bareMatches.length
  if (total > 0) {
    fail(
      `Entry chunk (${entryRel}) contains ${total} STATIC import/re-export facade edge(s) to ` +
        `agent-config (${facadeMatches.length} \`…from"./agent-config-*.js"\`, ${bareMatches.length} bare \`import"./agent-config-*.js"\`).\n` +
        `  This is the RS-01 static-payload edge: the entry statically re-imports shared runtime/helper\n` +
        `  symbols from agent-config via a cross-chunk facade, forcing the chunk (and its CSS <link>) onto the\n` +
        `  initial critical path even though EnvPresetManager/AgentConfigFields are only reached via dynamic\n` +
        `  import().then(). The healthy dynamic trigger — import("./agent-config-*.js").then( — has no \`from\`\n` +
        `  clause and is NOT counted here. Route the shared Vue runtime + plugin-vue export-helper + any\n` +
        `  composable statically shared between the entry and the agent-config SFCs (e.g. useLaunchEnvPresets)\n` +
        `  into a dedicated \`vendor\` chunk via vite manualChunks, so agent-config carries ONLY modal-gated SFC\n` +
        `  code and the entry re-imports those shared symbols from vendor (which is legitimately critical-path).`,
    )
    return 'failed'
  }
  ok(`nofacade : entry chunk has no static import/re-export facade edge to agent-config (RS-01 clean)`)
  return 'passed'
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
function main() {
  const args = parseArgs(process.argv)
  const distDir = args.dist ? resolve(args.dist) : findDist()
  if (!distDir) {
    console.error(`\x1b[31m✗ CHUNK-SPLIT GUARD FAILED:\x1b[0m Could not locate dist/ directory. Run \`pnpm build\` first, or pass --dist <path>.`)
    process.exit(1)
  }

  const indexHtmlPath = resolve(distDir, 'index.html')
  let html
  try {
    html = readFileSync(indexHtmlPath, 'utf8')
  } catch (e) {
    console.error(`\x1b[31m✗ CHUNK-SPLIT GUARD FAILED:\x1b[0m Cannot read ${indexHtmlPath}: ${e.message}`)
    process.exit(1)
  }

  console.log(`\n🔎 agent-config chunk-split guard (dist: ${distDir})\n`)

  const chunks = checkChunksAndMarkers(distDir, html)
  if (chunks.failed) {
    flushAndExit()
  }

  const trigger = checkDynamicImportTrigger(chunks.entryRel, chunks.entrySrc, chunks.lazyName)
  const nostatic = checkNoEagerSfcImport(chunks.lazyName, chunks.lazySrc)
  const nofacade = checkNoStaticFacade(chunks.entryRel, chunks.entrySrc, chunks.lazyName)
  const manifest = checkManifest(distDir, chunks.lazyName)

  if (failures.length) {
    flushAndExit()
  }

  const sizeKb = (readFileSync(chunks.entryAbs).length / 1024).toFixed(1)
  const lazySizeKb = (readFileSync(chunks.lazyAbs).length / 1024).toFixed(1)
  ok(`markers : entry ${chunks.entryRel} (${sizeKb} KB) clean; lazy assets/${chunks.lazyName} (${lazySizeKb} KB) contains both components`)

  const layers = [
    manifest !== 'skipped' ? `manifest: ${manifest}` : null,
    `markers: passed`,
    `trigger: ${trigger}`,
    `nostatic: ${nostatic}`,
    `nofacade: ${nofacade}`,
  ].filter(Boolean).join(', ')
  console.log(`\n\x1b[32m✓ agent-config chunk split intact (${layers}).\x1b[0m\n`)
}

function flushAndExit() {
  console.error(`\n\x1b[31m✗ CHUNK-SPLIT GUARD FAILED (${failures.length} issue(s)):\x1b[0m`)
  for (let i = 0; i < failures.length; i++) {
    console.error(`\n  ${i + 1}. ${failures[i]}`)
  }
  console.error('')
  process.exit(1)
}

main()
