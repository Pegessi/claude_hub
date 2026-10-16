# Chat formulas and rich content

## User outcome and scope

The Chat screenshot showed literal TeX between square brackets instead of a
formula. The shared Markdown renderer had no math tokenizer: ordinary Markdown
escaping consumed the backslashes in `\[` and `\(` before rendering.

This branch adds inline `$...$` / `\(...\)` and display `$$...$$` /
`\[...\]` formulas, readable streaming/error fallbacks, code-block copy
controls and keyboard-accessible horizontal scrolling for wide tables/formulas.
Production is not restarted; integration remains a separate authorized action.

## Research and implementation choices

| Area | Existing behavior | Choice |
| --- | --- | --- |
| Mathematics | Marked GFM only, no math tokens | Private Marked instance with math extensions before ordinary escaping; lazy KaTeX renderer/CSS/local fonts |
| Code | Highlighted `<pre><code>`, no block copy control | Language header and exact rendered-code text copy through the existing secure/insecure-context clipboard utility |
| Wide content | Tables already scroll visually, without a focusable region | Focusable labeled table wrappers; display/long inline formulas scroll within their message |
| Render resources | Every Markdown mount loads highlight.js | Request code/math chunks only when parsed tokens need them; preserve block/list caching and renderer version invalidation |
| Diagrams | Mermaid fences remain code | Follow-up candidate: lazy Mermaid with strict configuration, source fallback and keyboard/zoom design; no dependency added in this branch |
| Long histories/tools | Existing 40-turn initial window; expanding history grows DOM | Follow-up candidate: tool-body lazy mounting and measured windowing, retaining copy/selection, focus and approvals |
| Footnotes/citations | Standard GFM links, no footnote extension | Follow-up candidate after defining reference navigation and citation semantics; do not reinterpret arbitrary numeric brackets |

Primary references checked during this work:

- [Marked extension pipeline](https://marked.js.org/using_pro): tokenize new math
  syntax before Markdown can consume escapes, with a dedicated parser instance.
- [KaTeX delimiters](https://katex.org/docs/autorender): four conventional delimiter
  pairs; dollar matching is deliberately conservative for ordinary prices.
- [KaTeX options](https://katex.org/docs/options) and
  [security](https://katex.org/docs/security): `trust: false`, bounded expansion
  and size, fresh macro scope, escaped source fallback and final DOMPurify.
- [MathJax dynamic typesetting](https://docs.mathjax.org/en/latest/web/typeset.html)
  was considered; this renderer uses synchronous string output inside the
  existing sanitized block cache and does not mutate the live DOM afterward.
- [Mermaid usage and security levels](https://mermaid.js.org/config/usage.html)
  informs the deferred diagram work: restrict untrusted diagram HTML/clicks,
  preserve source, and avoid adding the full renderer to plain-message startup.

KaTeX 0.19.0 is locked. Formula output includes MathML and visual HTML. Unsupported
commands remain source; this is mathematical TeX support, not a full LaTeX document
engine. No bare-bracket heuristic is used. Formula parsing does not affect code
fences/spans; escaped dollars and ordinary `$5 and $10` remain literal. Source
remains authoritative for full-message copy/edit/fork, and macro definitions do
not persist between formulas/messages.

## Validation and evidence

The existing full-SPA image scenario is extended with a streamed partial formula
that completes, all four delimiters, a matrix, table math, source fallback,
sanitization, exact code copy, lazy resource requests and keyboard scrolling at
390px/280px. It uses the normal lifecycle owner and mocked/refused APIs, never
the production Hub or a provider. Clipboard `writeText` is mocked; the click,
text extraction and feedback are real Vue DOM behavior.

Unit probes exercise delimiter preservation, nested Markdown, cache invalidation,
currency/code literals, partial/malformed input, macro isolation and bounded
recursive expansion. Node uses a DOMPurify stub; the browser exercises real
sanitization and MathML. Unit tests alone are not sanitization evidence.

Task evidence is retained under
`/home/tiger/claude_hub_workspace/chat-rich-content/`.
Validation uses Node 24.21.0, pnpm 9.15.9, uv 0.12.15, Python 3.11.16 and
Playwright 1.58.0 with the installed Chromium. A task-local wrapper selects
the declared pnpm version without changing the machine's global toolchain.

The standard `verify.sh all` run passed docs, checkout safety, backend types,
backend tests (3,534 passed / 7 skipped), and frontend lint/types/tests/build.
Its formatter target detected the browser scenario's Python formatting; that
file was formatted using `backend/pyproject.toml`, and `backend-format` passed
on rerun. Latest frontend targets are rerun after the final source changes;
the original frontend suite had 852 passing cases, including 53 Markdown cases.

Independent review reproduced and closed currency/code pairing, table alignment
and image-alt regressions. Both explicit Markdown images and ordinary local
links promoted to image previews retain literal mathematical alternative text.
Prices next to TeX-command or negative-number formulas do not consume the later
opening dollar. Existing code spans, shell variables, GFM alignment and source
copy remain intact.

The 10-scenario mocked-browser gate passes, including real DOMPurify/MathML,
partial-to-completed streaming, local font loads, lazy renderers, exact code
copy, and 390px/280px keyboard-scroll containment. A final screenshot pass uses
a taller viewport solely to capture the full message without parent-scroll
clipping; the layout/scroll assertions still run at 844px height. Clipboard
permissions and real provider connectivity are not claimed. Owned browser,
context, driver and Vite cleanup are checked by the normal owner; the port
must refuse connections after cleanup.

The build keeps the math renderer separate: approximately 258 kB JS (78 kB
gzip) and 30 kB CSS (8 kB gzip), plus locally served fonts requested as needed.
Plain messages do not request these assets or the code highlighter. These are
bundle observations, not a claim of measured user-device speed improvement.

Terminal HMR/replay and live-provider tests were not run for this Markdown-only
change. The working tree's commit and retained evidence are the handoff point;
no production service restart or main-branch merge is included.

## Independent cross-validation follow-up

Two fresh read-only reviewers checked grammar/caching/performance and nested
Markdown/security boundaries separately. They reproduced dollar pairing that
misread shell substitutions, quoted variables and prices preceding negative
formulas, plus formula parsing inside raw HTML code/keyboard text. Conservative
dollar boundaries and Marked's raw-block state now preserve those literals;
numeric derivatives, norms and quoted TeX text remain valid formulas.
Bare `$$` shell PID syntax can still overlap display-math delimiters in prose;
shell examples in code spans/fences remain literal, and unsupported formulas
fall back to readable source without changing the stored message.

Malformed repeated delimiters also exposed synchronous quadratic work: custom
suffix searches and Marked's return-value-collecting `walkTokens` both copied or
rescanned increasingly long input. Math now bounds incomplete backslash scans
at the next unescaped opener and extends the native inline text stopping rules
without a suffix-wide pre-scan. A private effects-only token visitor retains
table/list/image traversal while avoiding accumulation of unused return values.
The private parser leaves shared Marked rules unchanged.

Independent offline controls compare full rendered output with native Marked,
not just elapsed time. Previously, malformed 32 KiB backslash input took about
1–2.7 seconds and 64 KiB escaped-closer input about 11.8 seconds. The repaired
32/64 KiB cases take tens to low hundreds of milliseconds on this host, close
to native parsing. Repeated dollar text is also covered to prevent the same
problem at inline boundaries. These are adversarial fixture measurements, not
general browser/device performance claims.

The browser scenario adds both 64 KiB backslash and 48 KiB dollar fixtures, a
broad five-second freeze guard, literal raw HTML code/keyboard assertions, and
hostile link/image/TeX inputs through actual DOMPurify and post-processing. The
offline reviewers stub DOMPurify; only browser checks provide real DOM evidence.
Cross-validation probes and before/after source hashes are retained under
`chat-rich-content/cross-validation/`; the final candidate report supersedes
the original `delivery.json` for this follow-up.

Both independent reviewers passed the final math/cache source hashes. All ten
mocked-API browser scenarios passed, including the real sanitizer, code copy,
stream completion and 390px/280px keyboard scrolling. Adversarial browser render
times were approximately 136 ms for the 64 KiB backslash fixture and 19 ms for
the 48 KiB dollar fixture. Raw inline HTML retains native Marked punctuation
escaping; the guard prevents formula interpretation rather than changing that
existing escape behavior. The tested hostile final DOM contained no selected
executable link/image/event-handler nodes. Owned browser resources closed, Vite
exited normally and port 5307 refused connections after cleanup; broader
process-tree exit was not independently verified by the lifecycle owner.

The final strict `./scripts/verify.sh all` rerun exited 0: all standard targets
passed, with 3,534 backend tests passed / 7 skipped and 857 frontend tests passed
(58 Markdown cases). Docs, browser-scenario formatting and the staged-index
safety check are checked again after the final evidence notes. The cross-review
fix is committed separately from the original feature; integration and a
production restart remain outside this handoff.

## Authorized main integration

After cross-validation, the user authorized integration. Fetched `origin/main`
and clean local main both remained `2d14506f2e004fc5f382c8dde0361c23acadc594`.
The dedicated checkout is `~/claude_hub_worktree/chat-rich-content-main-integration`,
branch `codex/chat-rich-content-main-integration`; the same Task remains under
initiator control with its configured review policy retained.

Fixed merge `735c001d2376f64ee7d4bfbc09ae1c665609f109` has that main commit
as first parent and reviewed feature `1b4bc751a947aac7f45d7cc3f02c7a117144b33f`
as second parent. Its tree `73cef1424a4f345f8224afa065f4a4bcf715bc74` is identical
to the validated feature tree. Git's native reconstruction has no conflicts or
manual decision paths; `verify.sh merge-decisions` passes with decision count 0,
so no decision record is needed. This mechanical result is distinct from
independent review of the resulting call paths and evidence.

The existing strict all-target and ten-scenario browser results apply unchanged
to that exact merge tree; no application, test or dependency files change during
integration. This documentation-only follow-up records the immutable merge and
is checked with the docs and staged-index gates. Independent final integration
review includes both the fixed merge and this follow-up. Before publication,
re-fetch and require clean main to fast-forward from the reviewed first parent;
never overwrite new remote work or force-push.

Integration evidence is retained under
`/home/tiger/claude_hub_workspace/chat-rich-content/integration/`. Git publication
does not restart or deploy the shared service; production acceptance remains
separate from the mocked browser and offline review evidence above.

## Concurrent local-main preservation

The pre-publication guard found local main advanced concurrently to
`15b4834f612d67f3c326287fb1fbc6b128a2f8ee`, with eight commits absent from
`origin/main`. Their direct-tab cleanup working log explicitly excludes remote
push. The guard stopped before updating either local or remote refs.

Local merge `c5d848a9eae1ae30df1cedebbfc008cdc0e572ec` preserves that local
main as first parent and the reviewed rich-content candidate `4daae296` as
second parent. Only the changelog conflicts; all release entries survive.
The accompanying local-merge decision record binds this immutable merge.
Application paths merge natively, including managed Chat's host/image changes
and the rich-content parser. Fresh full and browser gates and independent review
must cover this combined tree; previous exact-feature-tree evidence alone does
not validate it.

Publication has two intentional Git tips. Local main may fast-forward to the
combined reviewed candidate, retaining all local-only work. Remote main may
fast-forward only to `4daae29647815baf00d3e03e158fd7e3866397a5`, which publishes
the authorized rich-content feature without publishing the other eight commits.
Require the expected current tips and clean local state before each update;
never force-push or reset unrelated work.

PR 15's thirteen completed remote checks passed; backend pytest remained running.
The original main's run `38786712942` also had its backend job canceled after
35 minutes, with no available log. This is historical context, not proof of the
current cause or a passing remote result. Required local strict all checks and
independent review are the publication evidence; remote backend CI remains an
explicit unverified item. No branch protection/ruleset requires a remote status.
