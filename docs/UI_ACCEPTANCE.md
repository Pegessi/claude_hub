# UI acceptance

Use this with [CONTRIBUTING](../CONTRIBUTING.md). A passing unit suite or a
reviewer's impression does not establish that a user can operate the page.

## What a UI change must demonstrate

- Product controls use English consistently, including accessible labels,
  errors, empty states and status text. User messages, agent output and external
  names retain their original language.
- The intended action works with ordinary clicks or keyboard interaction.
  Do not use forced clicks to bypass an overlapping element.
- Relevant loading, error, retry and stale-response states preserve the
  operation's identity and user data. Closing a dialog must not silently turn
  an uncertain submission into a new request.
- Check the affected desktop, mobile and narrow-pane layouts. Inspect real
  text/control geometry when testing occlusion; overlapping backgrounds alone
  do not prove that text is hidden.
- State what was mocked and what was actually executed. These checks do not
  establish provider, OAuth or Feishu connectivity.

## Mocked UI gate

Install the declared tools and locked dependencies first. Select an already
installed Chromium executable matching the Playwright package in
`backend/uv.lock`:

```bash
CLAUDE_HUB_BROWSER_EXECUTABLE=/absolute/path/to/chrome \
  ./scripts/verify.sh browser-mocks
```

`CLAUDE_HUB_BROWSER_PORT` defaults to 5291. `CLAUDE_HUB_BROWSER_TIMEOUT` defaults
to 120 seconds per scenario. `CLAUDE_HUB_VERIFY_ARTIFACT_ROOT`, if supplied, must
be an existing task-owned directory. Verification never installs a browser.
The CI job has a separate, explicit installation step.

Both browser targets are explicit, outside `verify.sh all`: they need an installed
browser and owned ports. Each has an independent blocking CI job.
Both the format target and the runner registry are driven by discovery, so a
scenario added to `frontend/tests/` cannot silently go unformatted or unrun:
the owner refuses to start when the registry and the directory disagree. The
registry still names each scenario's entry page, which a filename cannot
supply. The type target
checks the complete backend and the typed browser owner/lifecycle helper;
the existing dynamic scenario scripts are not claimed as strictly type-checked.

| Scenario | Acceptance exercised |
| --- | --- |
| Task execution panel | Recording versus managed controls; progress/handoff and retry identity |
| Workspace Task creation | Unknown outcome/replay; dialog closure and credential recovery; workspace changes |
| Native subagents | Full thread identity, parent-turn grouping, lifecycle display and English controls |
| Chat composer in the full Chat page | Send/queue/stop against a live turn; failed send keeps the text and retries under a new identity; mobile reachability |
| Message provenance in the full Chat page | Persisted Web/Feishu/unknown origins after binding changes; desktop/mobile/grid operation and text clearance |
| Bot components | Settings/binding behavior, secret clearing and desktop/mobile layout |
| Queued chat activation | A hidden queued Chat activates before its draft is sent |
| Goal steering | Steering a running Goal and resuming after a question is answered |
| Chat image quality | An original image is sent once, sized from the real preview |

These are real Vue/Pinia DOM checks served by a dedicated Vite process. Page
APIs are mocked, unknown API routes fail, and WebSockets are blocked. This is
not a real Hub backend, production bundle acceptance or live provider test.
Terminal history/remount behavior has a separate blocking check:

```bash
CLAUDE_HUB_BROWSER_EXECUTABLE=/absolute/path/to/chrome \
  ./scripts/verify.sh terminal-hmr
```

This target requires the declared frontend tools/dependencies plus `tmux` and
`ttyd`. It runs the entire `test_terminal_hmr_recovery_e2e.py` module against its
own real backend, private tmux server and dedicated Vite instance. It is not a
provider/OAuth/Bot test. Pytest must exit zero and produce a nonempty JUnit report
with no skipped, failed or errored cases. The target uses a short private TMPDIR
and retains its exact path with the other test evidence.

`backend-tests` records `scope=default` or `scope=focused`, never claims that all
browser/diagnostic suites ran. The default excludes terminal replay, latency
measurement and HMR; HMR is transferred to the target above, not dropped.
`all` prints its unrun browser targets. Existing Goal/sidebar opt-in browser
flags are cleared only for the default scope, not for explicit focused runs.

Real-process deadlines can use `CLAUDE_HUB_E2E_TIMEOUT_SCALE` explicitly. The
verification environment records its input and the recovery test prints the
effective scale, raw durations and original-bound results. Default recovery
budgets remain 10s/12s. A scaled functional pass is not proof of an unscaled
performance goal; keep that condition visible in the handoff.

Other deliberate boundaries:
- Terminal replay is a blocking CI job with a `terminal-replay` verify
  target, after its width and shell assumptions were fixed.
- `test_terminal_input_latency_perf.py` is a documented, on-demand measurement,
  not a portable timing gate. Its module documents the manual command; do not
  start its terminal load against a shared service.
- Goal and sidebar review scripts remain opt-in checks, not part of these gates.
- The obsolete single-Bot `browser_feishu_bot_config.py` was retired. Current
  pool checks use the Bot-components and full-page provenance scenarios; this
  does not claim every global settings interaction is covered.

## Evidence and cleanup

For `browser-mocks`, the owner starts only its own Vite and scenario processes. It uses private
HOME/XDG paths, an environment allowlist, a nonce readiness endpoint and a
bound refusal endpoint for otherwise-unmocked API traffic. It does not load
the repository's `.env` or inherit provider credentials. Browser temporary
paths use a short, private `/tmp/ch-ui-*` root; deep artifact paths can prevent
Chromium startup. Only a fully passing run removes that root. Failures and
interruptions retain it and record its exact path.

Retain `summary.json`, per-scenario `report.json` and `lifecycle.json`, logs and
screenshots. Success requires matching runner identity, a zero process exit,
a passing business report, complete context/browser/driver closure and an
owned Vite port that no longer accepts connections. Missing evidence is not a
pass. CI uploads the available evidence even on failure.

SIGINT/SIGTERM request bounded cleanup; close failures, timeouts and forced
termination cannot pass. The shell uses `exec` so the CI timeout reaches the
Python owner directly. An additional interruption or hard kill may prevent the
final report from being written. `process_tree_exit: not_verified` deliberately
limits the claim: completed API closes and owned process exits do not prove
that every possible Chromium descendant has disappeared. Do not remove retained
failure directories or signal processes without checking their ownership.

The scenarios cover named requirements, not every control or layout in the
application. Extend the relevant scenario for a changed contract; keep a real
behavioral assertion even when adding a cheap source-wiring test.

## Adding a gate for new work

Decide this when the change lands, not afterwards. A gate written later is
written against the code as it ended up, so it tends to restate the
implementation instead of the requirement.

Add or extend a browser scenario when the change alters what a user can
observe or do: a control's enablement or wording, what survives a reload or
a failed request, what a state transition leaves on screen, or a layout rule
that has broken before. Prefer extending the scenario that already owns that
surface; a new scenario is for a surface none of them covers.

Do not add one for logic a unit test can pin down, for styling with no
behavioral consequence, or to raise a count. Nine real browsers already cost
several minutes.

Write the assertion against the requirement, in the user's terms. "Stop
releases the composer and drains the queue" stays true across a refactor;
"flushDraftQueue is called once" does not, and a gate that breaks on every
refactor gets weakened until it stops meaning anything. When a product change
makes an assertion fail, first ask whether the requirement changed. If it
did, restate it; if it did not, the gate just caught something.

Then make it real:

- Build on `run_browser_main` from the start. A scenario that drives its own
  browser produces no lifecycle evidence, and the owner refuses it. Three
  scenarios were written that way and sat unexecuted until converted.
- Mock the exact endpoints the page calls, with the backend's real response
  shape. Unknown routes fail by design; that failure is usually a fixture
  that has drifted from a contract, which is worth knowing.
- Run it before trusting it, then put the defect back and confirm it fails.
  A gate that has never failed has not been shown to detect anything. Every
  scenario here has had an assertion that was wrong until it was run.

Registration is automatic: discovery drives both the formatter and the runner
registry, and the owner refuses to start when the registry and the directory
disagree. Supply the entry page in `RUNNERS`, and add a row to the table above
saying what the scenario accepts.
