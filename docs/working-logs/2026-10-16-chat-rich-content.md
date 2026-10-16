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
the frontend suite has 852 passing cases, including 53 Markdown cases.

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
