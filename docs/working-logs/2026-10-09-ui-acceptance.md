# Stage 2: behavioral and UI acceptance

## Scope and delivery

Candidate: `feat/quality-stage2`, based on stage 1
`630a3a322d1b52976fc5fb8a7d7d71540def07e4`, in
`~/claude_hub_worktree/quality-stage2-acceptance`.
Development does not authorize main integration, push, deployment, a production
restart or a real Provider/Bot/OAuth test. Stage 3 remains a separate candidate.

The current contract is [UI acceptance](../UI_ACCEPTANCE.md). This record explains
the implementation and observed results; it is not another set of runtime rules.

## Changes

- Reuse the five existing real Vue/Pinia browser scenarios through one explicit
  `verify.sh browser-mocks` target and an independent CI job. Dependencies and
  Chromium installation remain explicit. `all` does not start a browser.
- Share browser/context/driver ownership and failure reporting. The owner uses
  private runtime paths, a restricted environment, its own Vite process and
  nonce readiness check, a refusal endpoint instead of a real backend, finite
  waits and per-runner identity checks. Deep artifact paths do not become long
  Chromium socket paths. Only a complete pass removes the owned short temp root.
- Move the complete terminal HMR module to its own explicit target and blocking
  CI job with installed dependencies, an explicit browser and a nonempty,
  zero-skip/failure/error JUnit requirement. Default backend scope is recorded
  as `default`, not `full`; focused backend invocations remain supported.
- Retire the unused single-Bot browser-config fixture, whose APIs and UI were
  replaced by the pool. Keep the manual latency benchmark explicitly manual.
- Replace obsolete single-Bot provenance mocks with exact current pool routes
  and state. Unknown API traffic fails instead of silently returning empty data.
- Exercise same-page Task credential recovery after close/reopen, existing
  replay and workspace-change rules, native child/parent lifecycle separation,
  full thread identities, source history and Bot component behavior.
- Make native subagent product labels consistently English, without changing
  lifecycle computation, thread grouping or user/agent content.
- Fix two actual Chat layout defects exposed by full-page acceptance: the pane
  name covered the Feishu control; on mobile a long first message also reached
  beneath the floating controls. Preserve the name, move it left of the button,
  and add mobile timeline top clearance. Keep ordinary clicks and glyph-line
  geometry assertions; do not force clicks or just compare backgrounds.
- Include browser scripts in formatting checks and the typed owner/helper in
  full backend typing. Existing dynamic scenario bodies are not represented as
  strictly type-checked. Add regression checks for short-temp routing and the
  executable entrypoint contract.

## Observed failures and corrections

The initial owner integration exposed four unmigrated `asyncio.run(main())`
entrypoints; the run failed before producing lifecycle evidence. Corrected
entrypoints are now exercised with `runpy` and a non-executing owner spy.

The next run failed Chromium startup with SIGTRAP for all five scenarios. With
the same environment and executable, changing only TMPDIR to a short private
path allowed real launch/context/close. The owner now assigns short numeric
subdirectories. This establishes the path dependence; the specific Chromium
internal failure mechanism was not separately traced.

The first working browser run passed four scenarios but failed the full Chat
provenance click: the pane name intercepted pointer events. After separating
those controls, all five passed. A subsequent long-message mobile check then
proved actual text overlap, rather than only background overlap: the first text
line occupied y=79..93 while the Feishu control occupied y=59..95. The mobile
clearance change removed those intersections. Earlier failures remain in the
local evidence directory and are not counted as passes.

The first aggregate run completed 2,712 backend tests but could not start HMR:
its default Playwright fixture looked for a browser in the private XDG cache.
HMR was therefore transferred to the explicit target above rather than silently
excluded. Its first explicit run had 12 passing cases and valid JUnit, but editing
the executing shell script caused an outer EOF/exit 2. It was not accepted. A
frozen-entrypoint rerun completed all 12 cases with JUnit and outer exit 0.

A subsequent default run hit the existing seven-terminal recovery timing bound
(10.83s versus 10s). The stand-ins resolved system Python 3.12.3 rather than the
declared pytest Python 3.11.16; they now exec that exact interpreter, with a
separate argv/interpreter regression. Pinning alone did not remove timing
variance. The test now uses the existing explicit E2E timeout scale for its two
budgets, while retaining base/default 10s and 12s, all identity/content/storage
assertions and cleanup. Budgets must be finite and positive. Raw times and
whether the original conditions were met are printed separately. Final default
validation used **scale=2**; this is not a claim of stable unscaled performance.

## Validation

Evidence root: `/tmp/claude-hub-quality-stage2.nTgRhA`.

- `full-all-scale2.log` / `.exit`: **aggregate exit 0**, with
  `CLAUDE_HUB_E2E_TIMEOUT_SCALE=2`. Default backend: **2,724 passed / 7 skipped**;
  frontend: **700 passed**. Docs, format, complete backend typing (**234 files**),
  frontend lint/types/build each passed. The existing large-bundle advisory remains.
  Six skips are explicit manual Goal/sidebar browser checks; one is the clean-
  checkout Git provenance check, recorded separately at handoff.
- The same full run logged raw recovery **9.43s cold / 9.52s overall**, within
  the original bounds on that run. Prior scale=1 failures/retries remain evidence
  that the original 10s condition is not consistently established on this host.
- `scaled-focused.log`: **61 passed**, including recovery, browser lifecycle,
  executable entrypoints, JUnit gating and document contracts. These overlap the
  default suite and are not added to its unique count.
- `hmr-explicit-final.log` / `.exit`: **12 passed**, JUnit exit 0 and outer exit 0
  in 211.66s, with the module's private backend/Vite/tmux setup. Evidence is in
  `claude-hub-verify.8Te0d0`; no fixed passed-count shortcut is used by the gate.
- `claude-hub-browser-mocks.LAI4mC/run`: **all five real-browser scenarios passed**,
  including desktop, 3-column and 390px provenance layouts. Mobile long-message
  text/control intersections were empty. Every context/browser/driver close
  completed, Vite exited normally and its port no longer accepted connections;
  the run removed its own short temp root.
- `term-probe-84d4fa2941`: a dedicated temporary runner acquired a real browser
  context, atomically reported readiness and remained alive. The controller
  verified its parent PID, then sent TERM only to its own coordinator Popen.
  Coordinator/runner returned 143/INTERRUPTED, with CancelledError, matching
  identities and three completed close stages. Vite exited normally, the port
  did not accept connections, no forced kill was needed and failure temp data
  was retained. This one run exited 0.214 seconds after TERM; that is not a
  universal time guarantee. The temporary runner was archived and removed only
  after validation. No probe mode was added to the normal helper or CLI.
- `final-process-audit.json`: no candidate browser/Node/Python/ttyd/Uvicorn
  processes, or processes with cwd inside the final backend/HMR test roots,
  remained at that audit point. This remains a scoped audit, not a full tree proof.

Independent read-only review covered browser ownership and CI integration,
subagent English/lifecycle semantics, and the Chat layout fix. Those reviews
were static; the runs above were performed by the workspace owner.

## Limits

The five UI scenarios mock application APIs and block WebSockets. The separate
HMR suite uses a real isolated backend and terminal runtime. Neither establishes
a provider, OAuth or Feishu round trip or production-bundle acceptance. The new
GitHub jobs have not yet run on GitHub. The final aggregate result is explicitly
conditioned on E2E scale=2; the original 10s performance goal is not claimed as
stable. Stage-one totals are not relabeled as stage-two results. Resource reports retain `process_tree_exit: not_verified`:
closed APIs, owned process exits and the scoped cwd audit do not prove that all
possible detached Chromium descendants have disappeared.
