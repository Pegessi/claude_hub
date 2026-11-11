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
| Appearance themes | Desktop and mobile Terminal/Workspace menus (including 760/761/768px boundary widths) open the same Appearance dialog; eight themes in light/dark have matching real previews, visible material/display-heading differences, readable core text/code/on-accent/message roles and a mode-independent selection; retired palettes recover safely and returning to Classic leaves no game treatment; System follows OS changes, choices survive reload and synchronize to another open page/standalone reader; native radio keyboard interaction, focus containment/return, Done/Esc/backdrop dismissal, and narrow/short-screen overflow and 44px primary controls are checked with mocked APIs |
| Theme interaction states | Actual Goal/approval and Workspace/TaskCreate primary controls retain readable paired ink/fill during native mouse-down as well as rest/hover; probes release outside the CTA without sending. Filled selected navigation and its nested labels/actions remain readable in their own interaction states. Ordinary two-Chat pane clicks move a distinct, contrast-safe active border across all 16 theme variants without shifting either pane's content geometry. |
| Theme keyboard and inline editing | Native Tab reaches real controls, not artificially focusable tab containers; a changed, contrast-safe focus cue remains visible at its four side midpoints. Real inline rename must focus automatically before test repair, retain readable text/boundary, preserve raw name case and cancel with Escape without a rename PATCH or page error. Sampled focus crops complement these computed-style checks; this is not exhaustive pixel or assistive-technology certification. |
| Game-theme identity | Inspect real full-Chat desktop and phone views in dark/light, plus color/grayscale before-after evidence: silhouette, display hierarchy and chrome material must distinguish the game themes beyond hue. Keep body/code readable, picker previews isolated, and the five non-game appearances unchanged within the documented source-bound comparison scope. |
| Task execution panel | Recording versus managed controls; progress/handoff and retry identity |
| Workspace Task creation | Unknown outcome/replay; dialog closure and credential recovery; workspace changes; image paste, decoded preview, attachment-only payload and exact replay; managed Agent/Reviewer and Task Terminal/Chat selection, including MASO Chat and remote restrictions |
| Workspace Task detail | At 390/720/1280px, open a task, expand/collapse Actions where applicable, perform Start through the real control and observe working status; verify overlays close without intercepting subsequent input |
| Native subagents | Summary-first questions/replies; no execution-detail request before expansion; detail-read retry, fixed-cursor pagination and cache across Chat switches; warm-cache edit reloads renumbered history and removes superseded turns; full thread identity, parent-turn grouping, lifecycle display and English controls |
| Sub-agent Agents dock | At 1280px the pane-chrome pill opens the anchored popover (Active first, collapsible Earlier, archive restore, one-click jump) and the workspace-attributed child stays out of the manual tab strip; at 390px the same pill sits on the session-name pill's row with no composer bar, and reveals a bottom sheet closed by Done, Esc or a backdrop tap |
| Native sub-agent dock window | With no Hub children, the pane-chrome pill lists the native child from the parent turn; its row carries `data-kind="native"`, the provider label, the child's report and a `View` action. `View` closes the popover and opens the Chat body's window, which replays the same rows as the inline region (delegated prompt, child report, dispatch result) behind `aria-modal`, a Tab trap and a body scroll lock; Done, Esc (including after focus left the window, and with a top-layer lightbox still owning the key) and a backdrop tap dismiss it and return focus to the row that opened it while that reveal is still open, else to the pill. `Show in transcript` expands the folded turn, opens the child's inline row and scrolls to it. A reloaded shallow history lists no child until its turn's execution detail is opened, then the same window opens for that historical child. At 390px the same pill opens the sheet, a row opens the window as a bottom sheet, and nothing overflows horizontally. |
| Chat composer in the full Chat page | History stays readable during delayed/unavailable capabilities while controls stay disabled; 30-minute native capability reuse, fresh MASO readiness and explicit refresh; MASO Chat-only selection, unavailable-service warning/create refusal, retry and preset recovery, existing-Chat service-loss explanation and model/effort provider keys; condensation retains visible output; send/queue/stop against a live turn; failed send keeps the text and retries with the same request identity; mobile reachability; managed-session identity distinct from provider identity, managed mailbox send/cancel and durable busy-turn input; compact reasoning states, plaintext card continuity, parent/child interruption isolation, reduced-motion and unobscured mobile thinking, no CSS animations in hidden child threads; lazy main/child tool details, bounded large-group summaries, native keyboard folds, complete keyboard-scrollable payloads and streamed status updates |
| Managed Chat question answer | A rendered blocking card shows the question as its title with each option's provider tradeoff; number keys 1–9 answer it while it is the open decision and a digit typed into the composer stays text; a rendered blocking card uses normal delivery with exact turn/request identity; a lost HTTP response leaves the card unresolved, and retry retains the same client turn ID before resolution; the delivered card collapses to its one-line resolved state; a card that arrived before its answer renders at the end of the turn rather than on top of the explanation, folded or expanded, live or finished, is never hidden by the activity fold, and is moved after the parts have been grouped so it stays an approval boundary (two reasoning runs either side of it do not merge, and a live turn's trailing run keeps animating); receipt states (claimed/resolved/stale) and managed mode/Goal/schedule controls behave as specified |
| Message provenance in the full Chat page | Persisted Web/Feishu/unknown origins after binding changes; desktop/mobile/grid operation and text clearance |
| Bot components | Settings/binding behavior, secret clearing and desktop/mobile layout |
| Queued chat activation | A hidden queued Chat activates before sending; editing yields activation and survives bounded Chat eviction |
| Goal steering and native status | Queue details/edit/cancel, original-image preview, empty-composer steering without Stop, objective editing, image-aware creation with the remembered Plan-first choice, a planning turn that parks on needs_input and renders its Approve & start / Revise card from the Goal snapshot (desktop and phone widths), and explicit interrupted-image recovery on an insecure-context API surface; Goal read/start errors and message-inbox HTTP/network failures show recovery guidance, retain drafts/images/last-known receipts, and retry observations without sending messages at desktop/narrow/mobile widths |
| Chat images and rich Markdown | An original image is sent once, sized from the real preview; assistant images open at actual size; streamed formulas render with MathML, code and prices remain literal, code copies exactly, and wide tables/formulas scroll by keyboard on mobile and narrow panes; Markdown/code/data links share an in-page attachment dialog with nested navigation, exact source copy, mobile Back/close and retained Chat drafts/scroll; stale entry links cannot change the selected Chat on preview closure or deactivation, genuine differing-tab Back/Forward still routes, and long-filename 320×390 previews retain readable source and reachable 44px actions (including standalone slots); explicit standalone Markdown opening remains reloadable, scoped and sanitized |
| Standalone Markdown reader | Reader-only typography and 960px width; dark/light themes, 390/320px layouts and large-text reflow; keyboard focus and 44px mobile actions; local table overflow; retained document DOM and no extra reads on source toggles; no full App/terminal/Workspace module load; loading/empty/truncated/error spacing and SVG-anchor rewriting |
| Dot launcher and panel | Aligned mobile toolbar controls at 360/390/768px, hidden mobile status summaries with continued polling and per-tab status transitions, normal Dot Chat/memory clicks, identity editing, task watching and lessons; desktop launcher and status summaries at 769/1280px |
| Needs-you inbox | The top bar's single Needs-you entry counts only sessions waiting on the user, reads as an amber tray glyph with that count, dropping its label at phone width for space (the name stays as `aria-label` and as a hover tip on a narrow window that has a pointer) sitting before the phone's ⋯ app menu (the catch-all keeps the far-right slot) and keeping `Needs you` beside the same glyph at desktop width (unread finished turn plus blocked question) and hides the badge at zero; its groups are titled `Waiting on you` / `Working` / `Recently done` so the surface's own name is not repeated as a group heading; its filter segments keep native Enter activation instead of jumping to the cursor row, Enter on the panel opens the cursor row and marks it viewed, Escape closes from inside the reply field, a reload never re-opens the surface, and at 390px the entry reveals a full-height sheet (Done and Esc close it) rather than a popover |
| Service restart | Default automatic Chat continuation and explicit opt-out; desktop/mobile restart controls; a lost POST response retains request identity; pending continuation remains observable; stream recovery preserves the unsent draft without another send or provider replacement |
| Diff review comments | Numbered gutter and one file header per file on the real diff card; the `+` affordance opens the inline editor (hover/focus on desktop, the row itself on touch), a Shift-click range names `path:lines` low-to-high and editing an existing note rather than adding a second one when it is extended over that note, one tap opens one editor on touch, the note stays pinned under its row, and the per-tab tray jumps back to the line, edits and removes entries at 1440/390px; a saved row whose turn/part keys were renumbered by a history reload is marked "Line no longer in this turn" instead of silently ignoring the jump, and the mark clears once the row resolves again; Send composes one message into the composer queue rather than a new transport |
| Chat work log and activity header | A running turn names the step it is on beside its stopwatch and counts the work done so far, with a failure marked on its own phrase rather than repainting the whole line (`2 edits · 2 commands · 2 reads · 1 failed` in amber); the turn's raw event count rides on that line as `N events so far` so a turn whose detail is not reduced yet still reports its volume, and such a turn reads `Working` rather than `Waiting for the first response`, until the pane backfills the newest turn's detail on activation so its steps appear (one read per turn — a turn that began before this pane was visible, including while the tab was hidden, owes one; a read retired by a growing descriptor is retried up to three times and a gap repair re-reads a range that is already loaded; the fold stays closed unless the reader opens it), and a turn that began in the open pane counts its own content events (a turn that has only started counts none, so it still reads as waiting) so `N events so far` grows even though the backend only describes hydrated history, with the larger of that tally and any descriptor shown; a fold whose detail has not been read inventories itself as `Activity · N events`; a turn that has not reported anything yet shows `Waiting for the first response` with the same stopwatch and one factual sentence taken from the Hub's own tab status — provider running (with its activity when it reports one), idle, offline, or the stream reconnecting — so a silent turn is legible without repeating the composer's model label; a sample older than a minute is discarded rather than repeated as current; the status light is centred on its label, not baselined; a turn blocked on the user's answer or on a sub-agent shows no "Working" word and a stopped pulse, so waiting never reads as activity; a finished turn's header reports `Activity · <steps> · <failed> · <duration>`, falls back to `Activity` plus duration while its detail is unread, and never shows a raw event count; the footer follows every batch appended after mount instead of freezing on its first label; both lines stay inside 390px with no clipped or overflowing text |
| Code display | A fenced code block's header names the language as a reader recognises it (`py` → `Python`, `ts` → `TypeScript`, a language-less fence → `Code`) rather than echoing the info string; a block whose lines overflow scrolls horizontally with a themed right-edge fade that appears only when it really overflows and withdraws at the scroll end, and never adds a vertical scrollbar; an edited-file diff card highlights each row with the language of the file it came from (chip next to the file name when the bundled highlighter has that grammar, nothing for an unknown extension), keeps its +/- tint, sign column and line numbers, tokenises nothing for a diff over 400 rows, repaints when the highlighter chunk lands or the theme flips, and binds only escaped text plus highlight.js spans so tool-argument markup can never execute |

These are real Vue/Pinia DOM checks served by a dedicated Vite process. Page
APIs are mocked, unknown API routes fail, and WebSockets are blocked. This is
not a real Hub backend, production bundle acceptance or live provider test.
Terminal history/remount behavior has a separate blocking check:

```bash
CLAUDE_HUB_BROWSER_EXECUTABLE=/absolute/path/to/chrome \
  ./scripts/verify.sh terminal-hmr
```

This target also checks palette-only and light/dark changes against a real ttyd
terminal: theme messages update xterm while the iframe/document/buffer and output
remain intact, without reconnecting.

This target requires the declared frontend tools/dependencies plus `tmux` and
`ttyd`. It runs the entire `test_terminal_hmr_recovery_e2e.py` module against its
own real backend, private tmux server and dedicated Vite instance. It is not a
provider/OAuth/Bot test. Pytest must exit zero and produce a nonempty JUnit report
with no skipped, failed or errored cases. The target uses a short private TMPDIR
and records its exact path with the other test evidence. The default removes
that runtime after owned processes exit; set `CLAUDE_HUB_VERIFY_KEEP_RUNTIME=1`
before the run when raw temporary state is needed. Backend/Vite helper logs are
kept under `terminal-hmr/<case>/` in the evidence directory, with a unique log per
launch so restarts cannot overwrite prior diagnostics.

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
the repository's `.env` or inherit provider credentials. Through `verify.sh`,
HOME/XDG/browser temporary data uses the short private `/tmp/ch-v-*/browser`
runtime, separate from evidence. The outer owner removes it after processes exit
on success, failure and handled interruption, unless explicit retention or a
safety check requires keeping it. `summary.json` reports `managed_by_verify` for
this temporary data; the parent `runtime.json` and `exit-code` hold the final
cleanup/verification outcome. Direct standalone use of `verify_browser_mocks.py`
keeps its previous `/tmp/ch-ui-*` behavior: only fully passing runs remove that
short temporary root.

Retain `summary.json`, per-scenario `report.json` and `lifecycle.json`, logs and
screenshots. Success requires matching runner identity, a zero process exit,
a passing business report, complete context/browser/driver closure and an
owned Vite port that no longer accepts connections. Missing evidence is not a
pass. CI uploads the available evidence even on failure.

SIGINT/SIGTERM request bounded cleanup; close failures, timeouts and forced
termination cannot pass. The shell uses `exec` so INT/TERM reach the outer
stdlib owner. It gives the browser owner up to 60 seconds for its own cleanup
and summary, then terminates residual owned processes with bounded waits.
Hard kills or machine loss can still prevent final reporting. CI allows 90 seconds
before hard kill so the 60-second cooperative phase has residual-cleanup margin.
Linux adopts surviving detached descendants and checks visible runtime references;
other POSIX hosts cannot detect escaped sessions. `process_tree_exit: not_verified` deliberately
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
behavioral consequence, or to raise a count. Ten real browsers already cost
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
