# Appearance themes — 2026-11-11

## Goal and scope

Replace the binary theme switch with a small, coherent multi-palette system that
works in both application modes and on phones. Worktree: `theme-system`, branch
`codex/theme-system`, baseline `745b6a9`. No backend preference, provider, shared
service, deployment, merge or push changes.

## Design decision card

- **Goal/boundary:** six curated palettes × light/dark, optional system following;
  keep the current Classic palette and existing saved light/dark preference.
- **Nearest implementations:** existing `theme.css` semantic tokens and terminal
  payload, native `<dialog>` in `ServiceRestartDialog.vue`, mobile app menus.
- **Reuse/add/remove:** keep `ColorScheme` and `data-theme` binary; add independent
  `data-palette`, a small typed preference registry, primitive palette overrides
  and one Appearance dialog. Remove binary switch UI and three root-level theme
  writers; `appStore` owns application/reader state and listeners.
- **Validation contract:** real store tests cover migration, normalization,
  persistence failures, system changes, storage events and listener disposal;
  mocked Chromium SPA tests cover ordinary clicks/keyboard, 12 rendered variants,
  reader inheritance, mobile reachability/overflow, and contrast. Run repository
  `all`, `browser-mocks` and `terminal-hmr` gates; mock coverage is not live provider
  or real-device Safari/Android acceptance.
- **Undecided:** none. Custom arbitrary colors, per-workspace themes, font/density
  settings and a command-palette overhaul are deliberately out of scope.

## Research and rationale

- [Radix functional color scales](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale):
  separate background, control state, border, solid accent and readable-text roles.
  Adopt the role separation, not an extra dependency or raw color-class API.
- [Linear UI redesign](https://linear.app/blog/how-we-redesigned-the-linear-ui):
  low-chroma structural planes and a small primitive vocabulary make themes
  cohesive without a new per-component palette. Here the presets are curated
  rather than exposing an unvalidated arbitrary-color generator.
- [GitHub appearance settings](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-personal-account-settings/managing-your-theme-settings):
  separate mode/system-following from selected colors.
- [Catppuccin style guide](https://github.com/catppuccin/catppuccin/blob/main/docs/style-guide.md):
  use related but distinguishable chrome, canvas and raised-surface planes.

Classic retains carbon/blue dark and neutral/clay light. Slate is cool blue;
Sand is warm paper/terracotta; Forest is green/teal; Dusk is dimmed violet;
Contrast emphasizes text and borders. Each has both modes. Status and code token
roles remain mode-specific, not recolored to the accent hue.

## Implementation

`main.ts` initializes the existing app store before selecting the dynamic root.
The store resolves an atomic `{mode, palette}` browser preference, listens to the
OS only for System mode, updates same-origin open pages from storage events, and
writes resolved root attributes and the mobile browser theme-color. Invalid
records recover to Classic; legacy preferences are read but never eagerly
rewritten. Failed storage writes keep the in-memory choice and show a limitation.

`palettes.css` adds 13 primitive colors per variant and maps those into the
existing `--ch-color-*`/terminal contract. The picker previews use the identical
CSS scopes; they are not approximate duplicated swatches. Semantic status and
syntax colors continue to come from the light/dark base. Terminal views observe
palette as well as resolved mode, repainting without reconnecting or replacing
the terminal.

Desktop opens a native dialog from the top-bar Appearance control. Terminal and
Workspace mobile menus open the same dialog. At <=640px it becomes a bottom
sheet with two-column previews, safe-area padding, independently scrollable
content, and 44px mode/Done controls. Native radio groups provide keyboard
selection; native modal focus containment, Escape, Done and backdrop dismissal
retain the saved choice and restore focus to the visible opener.

## Validation evidence

- Focused real-store tests: 6/6; existing frontend unit suite: 1,453/1,453 before
  adding those cases. Frontend types, lint and production build passed. The build
  retains the existing large-chunk warning (no new runtime dependency).
- `browser_appearance.py`: passed on owned Vite `:5318` with Python 3.11.16,
  Playwright 1.58.0 and Chromium 1208. All 12 variants, native keyboard/dismissal,
  system/explicit mode, reload, blocked persistence, legacy recovery and cross-tab
  reader updates passed; no unknown API calls or page errors. One document read
  and retained article DOM across palette changes. Phone sizes: 390×844,
  320×568 and 568×320. Evidence: `tasks/theme-browser/run-05/`.
- Negative control: injecting foreground equal to canvas was rejected at 1:1;
  this confirms the contrast gate measures rendered colors rather than merely
  finding expected source strings (`tasks/theme-browser/negative-01/`).
- Parent independently inspected Sand/light, Dusk/dark and portrait/landscape
  phone captures, and recomputed new core text contrast on canvas/chrome/surface/
  raised planes: minimum 4.85:1; white on new accent fills: minimum 5.97:1.
- `terminal-hmr`: 12/12 passed, with real private backend/ttyd/tmux and owned Vite;
  runtime removed. Evidence: `tasks/theme-evidence/claude-hub-verify.0guwzuzq/`.
  Initial attempt hit a Corepack cache miss under private HOME; rerun used the
  already installed pinned pnpm 9.15.9 directly via a task-local PATH shim.
- Whole-repository `all` and `browser-mocks` run separately; final status and
  independent fixed-SHA review are recorded in Task
  `6e8a377f-403d-4738-9108-f24a6ec258f9`. The first `all` attempt reached backend
  suite completion but was terminated at the 500-second foreground deadline
  before reporting, so it is **not** counted as passing. The owned runtime was
  removed; the retry runs with a lifetime longer than the suite. The first
  browser owner refused occupied `:5317`; that unrelated listener was left
  untouched and the retry uses checked-free `:5357`.

## Independent review and closure

The first fixed-SHA review (`e3f1d8e`) found two substantive design issues and one
mobile reachability gap; passing the first browser matrix did not catch them.

1. Filled approval hover consumers used the pale accent intended for text/rings.
   Both Chat approval Submit and Goal plan Approve now use the existing dedicated
   `--ch-color-accent-fill-hover` role. A rendered follow-up also exposed Goal's
   primary selector losing to its generic button selector; the primary rule now
   wins in normal/hover states. Tests distinguish the primary from secondary
   buttons and measure actual foreground/background, not only a token table.
   The full-SPA Goal/approval scenario passed 48 rest/hover samples (minimum
   hovered contrast 4.615:1) in `tasks/theme-browser/review-hover-02/`. Restoring
   the old hover CSS in the browser makes both controls' negative checks fail
   at 3.43:1. The shared helper is included in format and type verification.
2. Removed `viewport-fit=cover`: this feature must not opt the entire app and
   standalone readers into edge-to-edge iPhone layout without an all-edge policy.
   Existing safe-area-aware sheet spacing remains, within the browser's safe
   viewport. Physical notch/Safari acceptance is still not claimed.
3. Aligned the Workspace mobile header with the shell's 768px breakpoint, closing
   the 761–768px band where neither Appearance entry was reachable. The browser
   gate now checks 760, 761 and 768px, not just a conventional phone width.

Added a real terminal palette regression to the existing HMR suite. It changes
Classic → Forest dark → Sand dark → Sand light through the real store and checks
received `terminal-theme` payloads and live xterm options. The iframe, document,
xterm instance and buffer remain identical; no iframe load occurs and seeded
terminal content survives. The full expanded HMR target passed **13/13**, evidence
`tasks/theme-evidence/claude-hub-verify.9hzrqr6h/`.

The complete standard gate on the initial candidate passed **6,556 backend tests
(9 explicit skips)** and **1,459 frontend tests**, plus types, format, lint and
build; all **24 mocked browser scenarios** also passed. Final post-review gates
and the delta review are retained under `tasks/theme-evidence/` and attached to
the same Task, rather than counting the earlier candidate as final evidence.

No real provider, OAuth/Bot, physical mobile device or Safari acceptance is
claimed. The appearance matrix covers token/reader rendering with mocked data;
real rendered Goal/approval controls and the private ttyd terminal are checked
separately. This is not an audit of every existing application's text contrast.
No task server is retained for delivery.


## Visual-feedback revision: studio and game-inspired themes

The user accepted Classic and Contrast, rejected the four tinted alternatives,
then explicitly requested integrating the three neutral studies into this branch
and adding Zenless Zone Zero, Red Dead Redemption and GTA V inspired designs.
The final set for this revision is Classic, Contrast, Mono, Studio, Carbon,
Zenless, Frontier and Los Santos. The existing preference/store/terminal lifecycle
is retained; old unshipped palette IDs fall back to Classic while preserving mode.

**Change boundary:** `palettes.css` owns paired colors; `theme-treatments.css` owns
scoped radii/materials/display headings and small chrome motifs. The normal text
and code fonts stay shared. The eight-card picker uses four desktop columns and
two phone columns with its existing scroll/focus handling. Classic and Contrast
receive no new treatments. Bright fills use the existing `on-accent` token across
all actual accent consumers; user bubbles get a separate `message-user-text`
role. Danger/success foregrounds are intentionally not mechanically changed.

**References:** neutral studies follow the Geist/Linear/Raycast sources above.
Game references are the official [Zenless Zone Zero](https://zenless.hoyoverse.com/),
[Red Dead Redemption 2](https://www.rockstargames.com/reddeadredemption2) and
[GTA V](https://www.rockstargames.com/gta-v) sites. Hex values and CSS motifs are
our own interpretation, not claimed to be official game design tokens. No logos,
artwork, game fonts, audio, animations or full-canvas textures are imported.

**Acceptance:** all 16 variants must preserve core text/code and on-fill contrast,
show their material/font/edge differences, return to an unchanged Classic, keep
retired-ID mode preferences, work at narrow/short mobile and 768px boundaries,
and repaint an existing terminal without reconnecting. Actual rendered primary
controls are checked at rest/hover, rather than accepting CSS token values alone.
Final source SHA, full gates and independent review are recorded in the same Task
`6e8a377f-403d-4738-9108-f24a6ec258f9` and revision evidence. The user explicitly
requested a feature-branch push and retained `theme-preview-ipv6` on port 5358;
this does not authorize a merge to main or a shared backend restart.


Revision evidence before fixed-SHA review:

- Full frontend unit suite: **1,460 passed**; types, lint and bundle passed.
- Appearance scenario: all **16 variants**, both Classic switch-back snapshots,
  retired-ID recovery, keyboard/storage/reader and mobile-boundary contracts pass.
  Parent spotted a preview-only inheritance leak: Contrast's optional message
  primitives inherited the selected root's values. Defaults now resolve locally
  in each scope via `:where(:root, .theme-preview)`. The strengthened gate checks
  **32 foreign-root tiles**, including their painted bubble and paired ink/fill,
  rather than only the selected tile's canvas (`eight-palettes-fidelity`).
- Actual Goal/approval primary controls: **64 rest/hover samples**, minimum hovered
  contrast **4.615:1**; full game Chat/Goal desktop and phone captures inspected
  (`tasks/theme-browser/eight-chat-01/`). No unknown API calls or page errors.
- Full real terminal HMR gate: **13/13 passed**, including Mono dark → Zenless dark
  → Zenless light on the same iframe/document/xterm/buffer, preserving output.
  Evidence: `tasks/theme-game-evidence/claude-hub-verify.4kzjsj9b/`.
- Final complete checks run only after the candidate is frozen. They run serially
  (`all`, then `browser-mocks`) because the existing browser owner reserves a
  fixed refusal port. Do not edit `verify.sh` or source while these checks run.
  Independent review and final source/evidence identities stay in the same Task.


The revision review found two interaction regressions before the requested push.
Generic `.primary-button:active` rules in the TaskCreate dialog and shared
Workspace cards retained dark on-accent ink while replacing a bright fill with a
neutral dark pressed surface. Those rules now keep the accent fill/ink pair at
rest, hover and press. The new material override also removed the active-pane
border/halo; it was deleted so the original semantic selected-pane indicator
survives every theme, rather than trading keyboard ownership clarity for styling.

Real pointer-down coverage now samples the Goal/approval buttons in all three
states, plus the enabled Workspace header and valid TaskCreate submit controls;
release moves outside the action so the probe cannot submit. The Workspace
harness loads the actual production CSS and proves bright fills/dark ink before
sampling, rather than validating its old fallback colors. A real two-Chat split
view checks 32 ordinary pane handoffs across all 16 variants and contrast of the
selected border. Browser-local negative controls and exact final results are
retained in the revision evidence/Task.

The source treatment selectors were also flattened for the development preview;
the existing Vite production build already flattened them. Base per-tile font,
radius and shadow resets were verified, so no redundant reset was introduced.
The pre-fix aggregate check was intentionally cancelled after review; its retained
runtime identified a child that was confirmed exited. Only final frozen-source
checks count toward push acceptance. No shared service or user preview was stopped.


Focused closure evidence: `workspace-press-01` passed 64 real rest/pressed samples
across Add Task and Create Task, while `goal-approval-press-01` passed 96 real
rest/hover/pressed samples across Goal approve and approval Submit. Minimum
pressed contrast is 4.615:1. `split-handoff-01` passed 32 ordinary two-Chat pane
handoffs, minimum selected-border contrast 3.763:1. Parent reconciled the reports
and lifecycle closure; the Workspace/Goal scenarios completed in about 30/36s.
Browser-local negatives restore the reviewed defects and fail at 1.9719:1 on
Mono's pressed Create Task or equal active/inactive gray pane borders. Source
was not edited for negatives. All are in `tasks/theme-browser/`; the user preview
on 5358 remains running by explicit request, while test Vite5318 is stopped.

The frozen `9d056b3` passed strict `all` (6556 backend tests, 9 skips; 1460
frontend tests) and 13 terminal-HMR cases. Its browser owner passed 18 scenarios,
then stopped Appearance at the normal 120s deadline, leaving five scenarios
unrun. Earlier focused Appearance passes had allowed 180s and actually exceeded
138s, so they did not establish the CI-sized budget. That failed gate remains
in `tasks/theme-game-evidence/final-9d056b3/` and is not relabeled as a full pass.

The test now returns input to the main page after opening its separate reader,
using `bring_to_front()` while keeping the reader open and every assertion.
Small monotonic phase records make the scenario's cost inspectable. Under a
real 120s outer deadline, `foreground-budget-01` completes in 16.014s (16.291s
outer wall), retaining all 16 variants, 32 foreign-root preview tile checks,
reader DOM/one-read continuity and all phone/Workspace widths. Both visibility
observations say `visible` in headless Chromium; the evidence supports restoring
the interaction target, not a claim that `document.hidden` caused the delay.
No product code, coverage, runner/CI limits or animation behavior was changed.

On `cc74766`, strict `all` passed again and the browser owner passed 23/24,
including Appearance. The unchanged image-quality scenario instead timed out
waiting for the authored HTML external-link popup; an unchanged focused run
reproduced it. Two task-local diagnostic runs preserved the native click and
opened the link successfully, recording a shared native window and the retained
iframe's header-height/scroll transition. They did not prove the original gate
fixed or capture a failing event target; no sandbox/security change was justified.

The test now explicitly scrolls the external link into view and asserts viewport
visibility after source/preview restoration, before the original single native
click and popup expectation. It adds no sleep, retry or timeout relaxation.
`popup-readiness-01` runs the original uninstrumented scenario under 120s and
passes in 42.447s, including one external navigation, noopener, retained frame
DOM/scroll, sanitization and mobile checks, with complete cleanup. This establishes
the visible input target, not statistically proven elimination of every Chromium
hit-testing fluctuation. Final frozen gates run browser-first, then `all` and
terminal-HMR serially, to fail fast at the affected boundary.

## User-requested Gemini art-direction revision

The user rejected the game collection's visual strength, specifically Zenless as
only green frames, and requested MASO/Gemini review and optimization. Passing
functional checks was not treated as visual acceptance. Gemini's independent
critique found that the three grayscale silhouettes were nearly identical.
The initial implementation attempt encountered GoogleGenAI HTTP 429; its failed
owned Chat was archived without changing provider or pretending code was done.
After the user explicitly requested continuation, the same
`gemini-4-argon_reasoning_high` model completed a three-file product revision.

The revision reuses the existing palette registry, semantic color pairs and
`data-palette`-scoped CSS. It adds no preference mirror, runtime style engine,
component fork, dependency, artwork or remote font. Zenless now uses solid volt
capsules, asymmetric corners, oblique headings, halftone chrome and hard offset
shadows. Frontier uses square bookplate frames, serif/small-caps chapter rules,
ledger chrome and leather/vellum planes. Los Santos uses sharp HUD selections,
leading bars, condensed display fallbacks, Pacific-navy/concrete surfaces and
segmented emerald/cyan/amber accents. The picker uses the same grammar; body and
code are not skewed or replaced with decorative artwork.

Evidence is in `tasks/theme-gemini-style/candidate-02/`: actual desktop and phone
Chat in dark/light, Appearance/Workspace entry points, and color/grayscale
before-after sheets. The parent inspected representative rendered states rather
than accepting CSS declarations as proof of style. Only game palette sections,
game-scoped treatments and three descriptions changed in the product handoff.

An immutable `faf10bd` baseline was captured separately before comparison:
10 direct non-game states and 30 game-to-non-game return states. The first
collector attempt caught intermediate transition values; its sole recapture
awaited actual finite transitions without changing values or tolerances. The
accepted reference includes 152 root tokens per page, 23 real main-view and 11
reader components, their styles/pseudo-elements/dimensions, and retained reader
DOM with one read. Parent verified collector/fixture hashes, complete payload
equality and current stylesheet source hashes: the candidate's five non-game
appearances match the reference exactly within that documented desktop scope.
The temporary baseline checkout/server was removed; evidence remains retained.

Gemini's existing focused Appearance, Goal/approval, Workspace creation and
split-pane scenarios have passing reports and complete browser lifecycle
closure. Parent reconciled the numeric scope: the primary-control minimum across
all 16 variants remains 4.615:1; 6.731:1 is the game-only minimum, not the all-theme
minimum. New filled selected-navigation consumers and all three live terminal
palettes are receiving targeted coverage before the next frozen final gates.
No new branch push or final acceptance is inferred from this focused handoff.

The targeted extension subsequently passed: 120 real selected-navigation,
nested action and checked-mode samples across six game variants (text threshold
4.5, icon threshold 3). Parent independently recomputed all samples, including
102 nested paint composites, from raw captured colors: minimum 4.871706:1. The
suspected active-tab icon hover collision was not a product defect: the chip
fill is a 7%-alpha overlay, not an opaque surface, and actual compositing retains
contrast. The Los Santos sidebar gradient is bounded by its opaque computed
stops rather than incorrectly sampling the ancestor behind it.

The non-color identity negative keeps the actual Zenless palette, every root
token and the Goal DOM, but neutralizes only geometry/type/shadow. The same cue
assertion rejects capsule navigation, oblique hierarchy, asymmetric cards and
hard offsets; restoring the styles restores the checks. All six booted 390px
phone captures retain 44px primary controls and zero horizontal overflow. The
existing live-terminal case now exercises eight updates through Mono, all three
games in both modes and Mono restoration, preserving iframe/document/terminal/
buffer identity, zero frame loads, marker text and the full xterm theme.

Focused results and retained diagnostics are in `tasks/theme-gemini-style/qa/`.
The Goal/Chat scenario completes in 63.4s under the unchanged 120s budget;
Appearance, Inbox and Workspace creation also pass. The focused live-terminal
case passes with its isolated runtime removed. Parent's pre-commit type check
caught one fixed-length tuple inference in the test helper; an explicit variadic
tuple annotation fixes typing without changing runtime behavior or assertions.

Use `tasks/theme-gemini-style/parent-review/phone-dark-light-{color,grayscale}.png`
for phone comparison: these are derived from the verified QA screenshots and
the grayscale export checks equal R/G/B for every pixel. The original Gemini
phone composite has a mislabeled color duplicate in its grayscale section and
is not used as grayscale proof; the original desktop grayscale sheet is valid.
These image derivatives do not alter the tested UI. Final acceptance remains
bound to the next frozen SHA, independent review and full serial gates, with
results retained in the same Task/artifact tree rather than inferred here.

### Focus-layout finding after the frozen gates

Frozen `6e4d382` passes strict `browser-mocks` (24 scenarios), `all` (6,556
backend tests, 9 skips, 1,460 frontend tests) and `terminal-hmr` (13 cases), with
all five runtime owners stopped and disposable runtimes removed. This is not
acceptance of that candidate: a subsequent bounded real two-Chat App probe found
a focus-layout invariant missing from those checks.

At 1440×900, native pane-name 0→1→0 clicks retain both pane/Chat DOM nodes and
tab assignments. All outer panes stay 594×793. Classic and Frontier controls
retain inner rectangles and emit no observed resize entries. Zenless activation
changes content 590×789→590×787 and moves it down 2px; Los Santos changes
592×791→589×789 and moves it right 3px/down 2px. Both directions reproduce,
with six ResizeObserver entries per handoff across the two panes, their content
wrappers and Chat roots. Parent independently recomputed all raw deltas and
entry counts. Evidence and the bounded replay are in
`tasks/theme-gemini-style/qa/focus-geometry/`.

The cause is active-only border-width changes in the game treatments. The real
terminal observes its container and schedules iframe resize, but this probe
renders mocked Chat panes, not ttyd: terminal row/column changes, reconnect or
data loss were not measured and are not claimed. The existing live-terminal
harness mounts TerminalView directly, bypassing TerminalPane's styled frame,
so its passing result does not cover this invariant. The follow-up must keep
focus geometry constant while preserving the active appearance and selection
contrast, extend the existing all-palette handoffs, and re-freeze/review/validate
before delivery; passing the original gates alone is insufficient.

The same retained Gemini made the minimal correction: move Zenless's top/right/
bottom/left widths `4/2/2/2` and Los Santos's `3/1/1/4` into the base pane rule,
removing the active-only width overrides. Active border colors and shadows stay
unchanged. The existing handoff helper now compares both actual wrapper and
Chat-root rectangles before/after each native focus click, with exact equality,
no tolerance and the original 32 selection/contrast checks retained.

Focused Inbox passes in 26.208s. Browser-only negatives restore each game's old
width rules and the actual shared helper rejects `Focus changed inner pane
geometry`; removing the overrides restores all 32 passing handoffs. Fifteen
active computed properties match in both games' dark-mode comparisons; this
is not pixel-byte identity. The immutable non-game collector re-runs in 31.645s
and again yields 10 direct/30 return states with zero differences; parent checks
complete payload equality and current stylesheet hashes independently.
`qa/focus-geometry/fixed-summary.json` preserves these results and the cleanup.

A post-run mypy check catches the browser API's `Any` return in the new geometry
helper. Assigning it to an explicitly typed local before returning fixes the
annotation boundary without ignores or casts. This occurs after the focused
browser runs, whose original tested hash is retained; final full gates must use
the newly frozen helper. Standard backend types and format pass on that final
source. No new scenario, runner, dependency or product state machine is added.

### Native keyboard and inline rename follow-up

`3f6800d` also passes the full serial gates, but independent review requests
keyboard-focus and active-tab editor corrections. Real probes in
`qa/keyboard-rename/` capture 62 native keyboard samples and six actual
inline rename flows. Scope is narrowed from the static review: `.tab` is a plain
div with no tabindex, not a native Tab stop. It must not be made artificially
focusable for a test. Add-tab and the booted Zenless phone menu trigger acquire
real focus shadows; those are not observed shadow-suppression failures.

Active mode/layout controls have no measured focused-versus-rest paint change
in the six game variants; parent also verifies identical Zenless navigation
focus/rest PNGs. This describes the current candidate, not proof every layout
case was newly introduced by the redesign. Some Workspace primary variants
also lose focus paint. A separate failure affects actual tab/sidebar menu and
close/main buttons: a ring can exist but blend into an accent-filled surface.
Zenless/dark examples measure 1:1. The correction must address actual adjacent
paint, not merely add another same-color ring or replace working controls.

Actual active-tab editor text/outline contrast is 1.121/1.000 in Zenless dark,
1.637/1.138 in Los Santos dark, 1.985/1.000 in Frontier light and 2.527/1.000 in
Los Santos light. The raw stored name is preserved despite CSS uppercase in
Los Santos; probes use `text_content`, not rendered `innerText`, for identity.
Every flow types a temporary name then cancels with Escape, with zero PATCHes.

This real interaction also exposes an existing `TabBar.vue` error present in
`faf10bd`: a template ref inside `v-for` is treated as a scalar input, so the
rename timer throws `focus is not a function`. The bounded correction models
the collected ref array and focuses/selects its only editor; `editingTabId`
already guarantees a single editor. Scheduling, markup and rename APIs remain
unchanged. Frontend types and lint pass. Future committed coverage must require
production autofocus before any test action that could focus the input, and
must not suppress page errors. The baseline probe honestly retains six such
errors and a failing collection status rather than calling that run clean.

The same Gemini adds game-scoped focus outlines, contrast-paired tab descendant
rings, Zenless sidebar rings and active rename ink/caret/boundary. Rest decoration
and the five non-game styles remain scoped out of the change. Working add-tab
and phone trigger rules are left intact. The shared native-Tab oracle accepts a
changed, contrast-safe real outline, crisp shadow or border cue; it excludes
unchanged shadow components and verifies four visible side midpoints, rather
than requiring one exact token. An algebra negative rejects an unchanged inset
plus an invisible new outer ring, while accepting a genuinely contrasting cue.

Final focused results in `qa/keyboard-rename/fixed/HANDOFF.md`: Goal 73.544s,
Workspace 33.912s, Inbox 25.212s, real-oracle negatives 7.135s and immutable
comparison 31.622s, under unchanged 120s per-command budgets. Parent verifies
all six product/test hashes and independently recomputes 62 native focus cases
(minimum qualifying contrast 4.793:1 in Chat/phone, 5.079:1 in Workspace), six
automatically focused/cancelled renames (text/boundary minimum 6.731:1, zero
PATCHes), 32 stable pane handoffs and 10 direct/30 return complete-payload
non-game equalities. Existing pointer/selected-chrome samples remain intact.
All final page-error and unknown-request arrays are empty; the Workspace
fixture's six intentionally injected resource-console errors remain unfiltered.

Four browser-only faults are rejected by the actual oracles: missing navigation
cue, same-color tab ring, old rename text ink and old rename boundary ink. Each
restored positive passes. Sampled focus crops show readable menu/close glyphs
and phone rings without visible clipping; style/sidepoint checks are not an
exhaustive pixel or assistive-technology audit. Standard final backend types
and formatting pass, and the temporary Vite/browser resources are closed.
A discarded negative probe initially failed to create its output directory;
its log is retained and the corrected probe passes without a tracked source
change. Independent re-review must precede the next full frozen gate.

## Illustrated skins and orange/black revision (2026-11-12)

The preceding iteration was delivered at `a36148c` after independent approval
and its own full serial gates. It is now a baseline, not evidence that this new
iteration is accepted. The user explicitly rejects green as Zenless's dominant
color and allows richer wallpapers and iconography for all three game themes.
Orange/black is a design preference, not a claim that the game has one official
palette. Semantic success/ANSI green retains its operational meaning.

The same initiator-controlled Task is reopened at execution epoch 4. Product
work again uses the requested MASO `gemini-4-argon_reasoning_high`, in the same
`codex/theme-system` worktree; no main merge or shared-service operation is
included. Research and baseline/QA planning are read-only. The parent retains
integration, independent review, verification, delivery and cleanup ownership.

### Design boundary and reuse

The existing palette registry, `appStore` appearance lifecycle, game treatments,
shell consumers and picker remain the single theme path. Original local SVG
scenes and a small theme-aware chrome-icon component extend that path; there is
no second preference store, wallpaper service or persistence format. Neutral
fallbacks must preserve the five non-game styles and layout. Themes decorate
real controls, never provider identity, fake telemetry or fictional features.

The intended compositions are distinct: orange/obsidian analog street graphics
for Zenless, engraved western wilderness and parchment/leather for Frontier,
and a palm/city/coast scene with utility-map details for Los Santos. Artwork
must survive in actual desktop and phone views, not merely appear as an unused
CSS URL. Reading/control surfaces separate art from ink; body/code typography,
keyboard focus, native inline rename and stable pane geometry are retained.

Official reference entry pages were checked at HoYoverse's Zenless site and
Rockstar's Red Dead Redemption 2 and GTA V sites. These are visual references,
not a licensed asset inventory. New assets are authored for this repository;
no blanket copyright-clearance or unmeasured APCA/AAA claim is made.

### Verification agreement and baseline

The immutable non-game `baseline-02` reference and collector are retained.
Parent recomputation confirms all six prior source-identity hashes against
`a36148c` Git blobs. Existing full Chat/phone screenshots are before references,
not goldens for the new art direction. Evidence and the current execution
record live in ignored `tasks/theme-rich-skins/`.

The bounded acceptance contract requires broad orange color-family checks,
local/decoded artwork, visible artwork-only ON/OFF pixel contribution in an
approved exposed region, distinct rendered icon silhouettes in real controls,
and negative controls for hidden/missing art, stale/common icons and missing
readability backplates. Flat-color/gradient contrast arithmetic alone cannot
certify arbitrary SVG or pseudo-element overlays. Existing native interaction,
persistence, System, cross-tab, reader and terminal checks remain required, as
do ten direct and thirty return non-game equalities. Deadlines are not relaxed.

At this log update the product implementation and rendering are in progress;
new focused acceptance, independent review and frozen-SHA full gates are still
pending. The pre-existing IPv6 preview on 5358 is deliberately retained.
