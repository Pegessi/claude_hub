# Chat image recovery under cross-origin isolation

## Evidence and bounded scope

User report: assistant output sometimes displays only a broken-image icon and
its alt text. Read-only snapshots of four recent streams were inspected:
`visual output测试`, `1166进度图-deepseek2`, `coding for visual`, and `file service`.
These are samples, not an audit of every Chat or proof that every image failure
has this cause. No conversation, file asset or shared service was mutated.

The screenshot matches `visual output测试`, tab
`e5780b1b-7040-45b1-985b-2a76251c3ea1`, turn
`5452d55a-74ac-44de-8930-591b8c6e5791`. Events 85–86 record a successful
`DeployFile` of `/tmp/arena_progress_comparison.png`; the assistant's final
Markdown embeds its returned `lf0-maso.bytedance.net` PNG URL. The sampled
stream page had `has_more=false`.

The CDN returned HTTP 200, `image/png`, 774059 bytes and
`Access-Control-Allow-Origin: *`, but no cross-origin CORP permission. Hub sends
`Cross-Origin-Embedder-Policy: require-corp`. Chromium rejected the default
no-cors image with
`ERR_BLOCKED_BY_RESPONSE.NotSameOriginAfterDefaultedToSameOriginByCoep`.
The same image requested anonymously decoded at **2880 × 2000**. The repair's
real `MarkdownContent` also decoded that original URL; desktop/390px/280px
screenshots show the image instead of broken alt text.

Live-CDN reproduction required this host's Chromium local-network-access grant
and ignoring its certificate errors. Those are test-context allowances, not
changes to Hub policy. The committed regression needs neither allowance and
never contacts the CDN.

## Small change in existing owners

- `MarkdownBlockCache`'s existing image preparation pass marks external HTTP(S)
  images after sanitization, including unscoped content. Lazy loading lets the
  component restore remembered recovery state before loading replacement nodes.
- `MarkdownContent` keeps default no-cors behavior for CORP-compatible hosts.
  On an external error it tries anonymous CORS once. Failure then uses the
  existing unavailable/Retry UI. It does not add a proxy, weaken COEP, change
  credentials to `use-credentials`, or alter signed queries/fragments.
- Per-component URL memory restores the anonymous mode across streamed updates.
  A WeakSet records which actual nodes were changed automatically, so another
  node with the same URL cannot lose its author's explicit anonymous policy.
- Explicit Retry restarts the authored mode; a default external image can make
  at most two attempts (no-cors then anonymous) per click. This also allows a
  temporarily unavailable CORP-only host to recover. An explicitly anonymous
  image retries only anonymously. Local image nonce behavior is unchanged.
- Tab/document identity is part of the rendered block key, preventing identical
  HTML from retaining a previous scope's placeholder and event closure.

No external-image lightbox feature is added. Existing local/provider image
preview behavior is preserved. Responsive raw HTML whose `src` is same-origin
or data but whose selected `srcset` candidate is external remains an existing
coverage gap; this change does not claim to resolve every raw-image form.

## Validation and independent review

The regression extends the existing streaming harness and image browser suite.
It uses a real ephemeral second HTTP origin: Playwright `route.fulfill` can
bypass the very response policy under test. A plain negative-control image must
fail before the repaired component is tested. Assertions check decoded width
and actual `Sec-Fetch-Mode`, not just HTTP status or attributes.

Cases cover tab/managed/unscoped Markdown, lists/raw HTML, successful CORS-only
and CORP-only images, bounded failure during five updates, exact signed-URL
Retry, transient CORP recovery, identical-source scope changes, authored
anonymous mode after same-URL automatic fallback, data and same-origin images.

Independent review found three actionable defects in intermediate candidates:
CORP-only transient failures could become stuck in CORS; same-source scope
changes could retain old placeholders; URL-only provenance could erase a
different node's explicit anonymous mode. All three received focused regression
cases and repairs. Final review and verification results are recorded in the
Task/commit handoff; evidence is retained in this worktree's `.artifacts/`.

Initial full verification exposed an unrelated environment issue:
`FORCE_COLOR=3` puts ANSI escapes before the `ERROR:` prefix expected by
`test_ci_health`. The retry unsets that variable without changing tests.
The default backend scope then passed **6629 tests, 10 skipped**; the outer
500-second tool limit interrupted the subsequent frontend test run, not the
backend result. The frontend suite was rerun separately: **1499 passed**.

Final independent review found no remaining blockers and checked unchanged
product/test blob identities. Its separate live HTTP probes also confirmed
that authored `anonymous` sends no Cookie and `use-credentials` retains only
that author's intended credential behavior.

Additional final-candidate checks passed: document invariants, Black/isort,
frontend lint/types/build, and the focused cross-origin browser regression.
Backend types passed in the full run. The isolated terminal-HMR gate passed
**13 tests** after supplying the already-installed
`COREPACK_HOME=/home/tiger/.cache/node/corepack`; the first attempt's private
HOME could not find pnpm and network downloads are correctly disabled by the
verification runner. No tools were downloaded and no runtime isolation was
removed to make it pass.

Evidence references (relative to the feature worktree unless absolute):

- `verify-all-final.log` under `.artifacts/`: backend default-scope completion;
  `/tmp/claude-hub-verify.z7duhz19`: retained backend reports/provenance.
- `.artifacts/frontend-tests-final.log`: final 1499-test frontend run.
- `.artifacts/image-policy-final/`: final real second-origin regression report.
- `.artifacts/live-cdn/`: live original image report and 1280/390/280px screenshots.
- `.artifacts/claude-hub-verify.a6flaueq/`: final 13-test terminal-HMR gate.
- `.artifacts/claude-hub-browser-mocks.al9ghehz/`: initial complete browser gate
  (exit 0).
- `.artifacts/claude-hub-browser-mocks.uzndtfs4/`: final complete browser gate
  (exit 0, all registered scenarios executed, owned Vite port closed).

The task-owned review server on port 5389 was stopped and its port closure
verified. Runtime-producing gates removed their private runtimes and retained
their evidence; no shared process was stopped.

Delivery is a reviewed feature branch only. Main, deployed assets and the shared
Hub service remain untouched; shipping/restarting requires separate approval.
