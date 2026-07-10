# Frontend Response-Speed Audit

Date: 2026-07-11
Baseline: `develop @ 9fda1fa` (post UI-sweep SL-01..SL-13 shipped; SL-12 ETag/304
and `ff55553` board-payload-slim already in place).
Auditor: single-agent linear read-only pass; zero `.vue/.ts/.py` source edits in
this task. Findings are file-anchored and intended to be dispatched as parallel
bounded follow-up tasks.

---

## 1. Methodology

### 1.1 Scope

Companion to `2026-07-10-minimalist-ui-second-look.md`. That doc covered UI polish
(weights, radii, focus rings, token wiring, an opportunistic perf win in SL-12);
this doc is the first systematic sweep of the **response-speed** prong called
out in the workspace standing directive ("美化前端ui + 响应速度", minimalist
elegant style + response speed).

The only performance work already shipped is:
- `37efb0f` — SL-12: ETag/304 + `If-None-Match` on `GET /api/tabs/status`
  (terminalStore) and `statusesEqual` shallow-diff guard.
- `ff55553` — board-payload slim (backend strips goal-packet prose /
  evaluation_reports from list endpoints; heavy fields fetched on demand).
- `364a739`/`dfb26f3` — lazy-load the workspace chunk via `defineAsyncComponent`
  with `requestIdleCallback` prefetch (App.vue:138-140, 388-405).
- `3ee1ef3` — TerminalView xterm padding + iframe lifecycle caps
  (`MAX_SINGLE_PANE_CACHED_TERMINALS=4`).
- `af69c96` — prefetchWorkspaceChunk.

This audit re-checks whether those wins are still sound (they are) and looks
for what remains.

### 1.2 Build snapshot

Fresh `pnpm -C frontend build` on `develop@9fda1fa`:

```
dist/index.html                              0.45 kB │ gzip:  0.30 kB
dist/assets/index-BsirKzA_.css              87.51 kB │ gzip: 12.68 kB
dist/assets/AgentWorkspaceView-CinPz6d4.css 95.72 kB │ gzip: 13.26 kB
dist/assets/index-Dp2NUH-4.js              194.52 kB │ gzip: 67.43 kB
dist/assets/AgentWorkspaceView-DZP14TmR.js 207.17 kB │ gzip: 58.04 kB
✓ built in 1.94s
```

No chunk exceeds 250 KB gzipped (the common "consider code-splitting" threshold).
Total gzipped JS for a workspace-page first load is ~125 KB; idle prefetch means
the workspace chunk is typically already cached by the time a user clicks into
workspace mode. Bundle size is **not** the primary bottleneck — wins below are
dominated by network cadence, store-memoization gaps, and render-path O(n²)
hotspots that show up once a workspace accumulates tasks and reports.

Dependency footprint is small by design: runtime deps are only `vue ^3.5`,
`pinia ^2.2`, `marked ^18`, `dompurify ^3.4`. There is **no** vue-router, no
xterm.js / codemirror in the Vue bundle (terminals are iframed to ttyd), no
UI-framework, no icon pack, no date/charting lib. Icons are CSS text glyphs;
dates are hand-formatted. `marked` + `dompurify` correctly land only in the
async workspace chunk (MarkdownContent:13-14, transitively imported by AWV).

`vite.config.ts` has **no `build.rollupOptions.output.manualChunks`** — all
splitting is driven by the single dynamic `import('@/components/AgentWorkspaceView.vue')`.
That is the right shape for an app this size, but a couple of heavy disclosure
panels that live in the shell chunk are easy wins (PR-04, PR-05).

### 1.3 What was inspected

| Area | Files | Verdict |
| --- | --- | --- |
| Build / chunk layout | `vite.config.ts`, dist output, all `import(` / `defineAsyncComponent` callsites | Findings PR-04, PR-05, PR-11 |
| Shell chrome + routes | `App.vue` (1007 lines), `main.ts` (17 lines) | Sound (single async split for AWV + idle prefetch) |
| Tab strip | `TabBar.vue` (2593 lines) | Imports ASFP + EPM + ACF statically → PR-04/PR-05 |
| Workspace board | `AgentWorkspaceView.vue` (10949 lines) | Findings PR-01, PR-02, PR-06, PR-07, PR-08, PR-10, PR-11, PR-12 |
| Floating status panel | `AgentStatusFloatingPanel.vue` (1012 lines) | Finding PR-04 |
| Network menu | `NetworkAccessMenu.vue` (655 lines) | Pulled into shell chunk; left as intentional (see §3) |
| Env preset / agent config | `EnvPresetManager.vue` (615 lines), `AgentConfigFields.vue` (336 lines) | Double-bundled finding PR-05 |
| Markdown renderer | `MarkdownContent.vue` (240 lines) | Finding PR-12 |
| Stores | `workspaceStore.ts` (771 lines), `terminalStore.ts` (482 lines), `appStore.ts`, `authStore.ts` | Findings PR-01, PR-02, PR-03, PR-09; terminalStore already sound post SL-12 |
| Terminal views | `TerminalView.vue`, `TerminalGridView.vue`, `MobileControls.vue` | Inspected; not regressed (MAX_SINGLE_PANE_CACHED_TERMINALS=4, rAF resize coalescing, SAB keystroke fast path all intact) |
| v-for keys | all `.vue` files | All lists have `:key`; no index-key misuse on dynamic rows (only trivial static-range loops, benign) |

### 1.4 Verification commands run

```
pnpm -C <worktree>/frontend build                    # chunk sizes
grep -n 'import(' src/**/*.vue src/**/*.ts           # dynamic-import inventory
grep -rn 'setInterval\|setTimeout' src/stores src/components
grep -rn 'If-None-Match\|ETag' src/stores
grep -rn 'v-for' src/components/*.vue | wc -l        # list inventory
wc -l src/components/*.vue src/stores/*.ts
```

All line numbers below reference `develop@9fda1fa` (the branch point for
follow-up tasks); drift will be small since each PR is a 5–60 line edit.

---

## 2. Findings

File-disjoint: PR-01/PR-03 touch workspaceStore + backend board endpoint
(coordinate if dispatching same sprint), PR-04/PR-05 both touch TabBar imports
(serialise or combine), PR-02/PR-06/PR-07/PR-08/PR-12 all touch AWV or its
imports but in different code regions so they can still run in parallel with
normal conflict resolution — callers noted.

| ID | Title | File:line | Impact | Effort | Disjoint? |
| --- | --- | --- | --- | --- | --- |
| PR-01 | Board ships full report bodies every 2.5s | `workspaceStore.ts:96,246`; backend board endpoint; `types/index.ts:559-564` | **high** | M | backend + store |
| PR-02 | Non-memoized O(N·R) per-card scans on every poll | `workspaceStore.ts:148-155`; `AWV:4085-4090,697,702,4152,4206,4283,4291,4297` | **high** | S | AWV + store |
| PR-03 | `fetchFeedbackLessons` chained to every board poll, no ETag | `workspaceStore.ts:240,247,321-329,341,358,381` | **med** | S | store-only |
| PR-04 | ASFP (1012 lines) statically imported into shell chunk | `TabBar.vue:641` (shell chunk); refs `App.vue:121-125` | **med** | S | TabBar-only |
| PR-05 | EnvPresetManager + AgentConfigFields double-bundled | `TabBar.vue:650-651`, `AWV:3092-3093` | **med** | S | TabBar + AWV imports |
| PR-06 | `latestResidentReport` sorts on every compute | `AWV:6008-6018` | **med** | S | AWV-only |
| PR-07 | `feedbackLessonMatchSummary` O(T·L·tokenize) per poll | `AWV:3446-3491,3539-3544` | **low** | S | AWV-only |
| PR-08 | Overlapping agent-status polls in workspace mode | `AWV:6122-6123`; `terminalStore.ts:261-278`; `ASFP:427` | **low** | S | AWV-only (guard) |
| PR-09 | `fetchRemoteProfiles` refetched from 8 sites, no in-flight dedup | `AWV:4900-4919,5140,5156,5486,5498,5505,6070,6091,6120` | **low** | S | AWV-only |
| PR-10 | Kanban todo/queued columns unbounded (only done collapses) | `AWV:3288-3306,455,478-488,496` | **low** | M | AWV-only |
| PR-11 | Heavy modal subtrees inline in 10 949-line AWV | `AWV:3091-3097` (static imports); 13+ modal v-if blocks in template | **low** | M | AWV — extract components |
| PR-12 | MarkdownContent re-parses + re-sanitizes per report per prop change | `MarkdownContent.vue:13-14,32-44`; AWV render sites 804/855/1134/1168/1285/1321/1393/1594 | **low** | S | MarkdownContent-only |

### PR-01 — Board ships full report bodies every 2.5s (high / M)

**Files:** `backend/claude_hub/api/workspaces.py` (board endpoint),
`frontend/src/stores/workspaceStore.ts:96,233-247` (board fetch),
`frontend/src/types/index.ts:559-564` (`WorkspaceBoard.reports: AgentReport[]`),
`frontend/src/components/AgentWorkspaceView.vue` uses of `workspaceStore.reports`.

**Symptom:** The board endpoint returns a `WorkspaceBoard` whose `reports` field
is `AgentReport[]` — every report, for every task, full body (21 non-id fields
per report including `message`, `message_en`, `message_zh` markdown strings,
`changed_files[]`, `acceptance_check[]`, `profile_results[]`, `evaluation_report`,
`risks`, `validation`). The board poll fires every 2500 ms (`AWV:6123`). The
only Kanban-card reader of `board.reports` is `latestReportByTaskId`
(`workspaceStore.ts:140-146`) — a map of task_id → the **latest** report.
Older reports are consumed only in the selected-task detail panel, which
*separately* calls `fetchTaskReports(taskId)` (`workspaceStore.ts:261-289`)
to get the same reports again over HTTP. Result: every report body ships
twice (once on the 2.5s board poll, once in the on-demand detail fetch), and
every polling cycle transfers KB–tens-of-KB of markdown strings that the Kanban
view never renders.

**Proposed fix:**
1. Add a backend field-projection (or a new `reports_lite: Array<{id, task_id, session_id, state, created_at}>` / `latest_reports_by_task: Record<task_id, AgentReportHeader>`);
2. Frontend: stop reading report bodies off `board.reports`; populate `latestReportByTaskId` from the lite header map and keep `fetchTaskReports` for the detail panel;
3. Bump `boardETags` content hash accordingly.

Follows the precedent set by `ff55553` board-payload-slim (which already strips
goal-packet prose + evaluation reports). Risk: low if the shape change is
additive (lite field + deprecate full `reports`), medium if done in place.
Serialization note: shares workspaceStore + backend with PR-03; can ship in the
same backend diff or as a sequential pair.

**Impact:** high. This is the single biggest wire-cost win because the 2.5s
board poll is the hottest request in the app; older-report markdown is the
largest variable-size segment of the payload on workspaces with many completed
tasks.

### PR-02 — Non-memoized O(N·R) per-card scans on every poll (high / S)

**Files:** `frontend/src/stores/workspaceStore.ts:148-155`,
`frontend/src/components/AgentWorkspaceView.vue` template usages at lines
`697, 702, 4152, 4206, 4283, 4291, 4297` (per task card), plus
`latestReviewReportForTask` at `4085-4090` which calls `.reportsForTask(task).filter(...)`.

**Symptom:** The store already memoizes `latestReportByTaskId` as a computed
Record (lines 140-146) but two analogous accessors are plain methods that do
linear scans **per call**:
- `sessionForTask(task)` (`:148-151`) does `sessions.value.find(...)`.
- `reportsForTask(task)` (`:153-155`) does `reports.value.filter(...)`.

Both are invoked from template expressions inside task-card v-for loops, which
means every render (which happens after every non-304 board poll) runs N·S and
N·R scans for N cards. `latestReviewReportForTask` compounds this by calling
`reportsForTask(task).filter(isReviewReport)` — two passes per call. With 100
queued tasks × 200 reports × 5 callsites this is the most visible
reactive-render hotspot.

**Proposed fix:** Mirror `latestReportByTaskId` with:
```
const sessionByTaskId = computed<Record<string, ManagedSession>>(() => { ... })
const reportsByTaskId  = computed<Record<string, AgentReport[]>>(() => { ... })
```
built in a single O(S+R) pass after each board replacement, and switch the
template getters / `latestReviewReportForTask` to look up from the maps.
For the "latest review report" per task, either add a dedicated map or derive
it in the same pass.

**Impact:** high. Straightforward O(N·(R+S)) → O(N+R+S) with a ~20 line store
change and a few template one-line swaps; risk is very low (pure refactor with
identical output).

### PR-03 — `fetchFeedbackLessons` chained to every board poll, no ETag (med / S)

**Files:** `frontend/src/stores/workspaceStore.ts:240, 247, 321-329, 341, 358, 381`.

**Symptom:** After every successful board fetch (304 or 200 except the 304
early-return path), the store `await fetchFeedbackLessons(workspaceId)` fires
an additional `GET /api/workspaces/:id/lessons?limit=50`. That request has
**no** `If-None-Match` / ETag handling, no in-flight promise coalescing, and
lessons change on human timescales (feedback ingest) not poll-cycle timescales.
Combined with PR-01 this doubles poll HTTP volume in workspace mode.

**Proposed fix:**
1. Send `If-None-Match` against a per-workspace `lessonsETag` and handle
   304 (mirror board ETAGS / terminalStore `lastAgentStatusETag`).
2. Add in-flight dedup (a `lessonFetches` promise-coalescing map like
   `boardFetches/taskDetailFetches` at lines 75, 83, 89).
3. Optionally lower cadence to only refetch on (a) mount, (b) after a
   report POST that reported a lesson match, or (c) every 30–60s instead of
   every 2.5s.

This is the same pattern as SL-12 (terminalStore ETag); ~20 lines of TS.

**Serialization:** shares workspaceStore with PR-01/PR-02; combine in one
store-cleanup commit or run sequentially.

### PR-04 — ASFP (1012 lines) statically imported into shell chunk (med / S)

**Files:** `frontend/src/components/TabBar.vue:182-188,641` (two
`<AgentStatusFloatingPanel>` instances mounted in tab strip),
`frontend/src/App.vue:121-125` (TabBar is statically imported into shell).

**Symptom:** `AgentStatusFloatingPanel.vue` is 1012 lines of agent list,
panels, per-row actions, refresh, and observer setup, imported statically by
TabBar, which is imported statically by App.vue. It therefore lands in the
`index-*.js` initial chunk (67 KB gz) even though:
- it renders inside two `<details>`-like collapsed chips (`.status-trigger`)
  and is invisible until a user clicks a chip;
- it has its own `panel-mode-switch`, per-row actions, observer, and
  per-agent-row logic.

For a user who only ever uses terminal mode (the default on boot), this is
dead JS in the initial bundle.

**Proposed fix:** `defineAsyncComponent(() => import('@/components/AgentStatusFloatingPanel.vue'))`
in TabBar.vue and add a small `Suspense`-less placeholder (the existing chip
still reads static counts from terminalStore, so the panel content can hydrate
on first open without layout shift). This moves ~10–15 KB of component code to
an on-demand chunk (loaded on first chip open, cheap).

Risk: low — the panel is always a child of a collapsed toggle; the only
subtlety is the existing `store.startAgentStatusPolling()` call in
ASFP:427 (polling needs to start even if the panel hasn't been imported yet).
Move that `startAgentStatusPolling` to TabBar onMounted (where it already
effectively fires) so the import stays truly lazy.

### PR-05 — EnvPresetManager + AgentConfigFields double-bundled (med / S)

**Files:** `frontend/src/components/TabBar.vue:650-651`,
`frontend/src/components/AgentWorkspaceView.vue:3092-3093`.

**Symptom:** `EnvPresetManager.vue` (615 lines) and `AgentConfigFields.vue`
(336 lines) are statically imported by **both** TabBar (shell chunk) and AWV
(workspace chunk). Because they are static imports from two different entry
chunks, Rollup cannot deduplicate them into a shared chunk without a
`manualChunks` hint, and they ship in *both* bundles. They render only behind
`v-if="showSwitchEnvManager"` / modal gates.

**Proposed fix:**
1. Easiest: create a shared `async-components.ts` (or define them inline) as
   `defineAsyncComponent` in BOTH TabBar and AWV, so Rollup emits the module
   once as a shared lazy chunk. Or
2. Add a tiny `manualChunks` rule in `vite.config.ts` for these two files
   (and `MarkdownContent` if it ever leaks into the shell).

Removes ~950 lines of JS from the initial shell chunk (and a duplicate copy
from the workspace chunk).

**Serialization:** touches the same TabBar imports as PR-04; combine into a
single "lazy-load shell panels" commit or run sequentially.

### PR-06 — `latestResidentReport` sorts all resident reports on every compute (med / S)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:6008-6018`.

**Symptom:**
```ts
const latestResidentReport = computed(() => {
  const sessionId = activeWorkspace.value?.resident_agent_session_id
  if (!sessionId) return null
  const residentReports = workspaceStore.reports.filter(
    report => report.session_id === sessionId
  )
  if (residentReports.length === 0) return null
  return [...residentReports].sort(
    (a, b) => (parseTimestampMs(b.created_at) ?? 0) - (parseTimestampMs(a.created_at) ?? 0)
  )[0]
})
```
Runs on every `workspaceStore.reports` reference change (every non-304 board
poll). Allocates a filtered array, spreads + sorts it, just to take `[0]`. On
a workspace with R=1000 reports this is an O(R) filter + O(K log K) sort where
K is the resident-session report count.

**Proposed fix:** fold the lookup into `workspaceStore.ts` alongside
`latestReportByTaskId` — add `latestReportBySessionId` (same single-pass shape
keyed on session_id). That makes this a Record lookup in AWV with zero
per-poll allocation. Even a local single-pass "track max as we scan" rewrite
would be a clear win without touching the store.

### PR-07 — `feedbackLessonMatchSummary` runs O(T·L·tokenize) per poll (low / S)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:3446-3491`
(`feedbackTokens`, `matchingFeedbackLessons`), `2475` (render site),
`3539-3544` (computed).

**Symptom:** The summary strip at line 2475 renders something like
"4 tasks match feedback lessons", computed as:
```ts
const matchedTaskCount = tasks.value.filter(task => matchingFeedbackLessons(task).length > 0).length
```
`matchingFeedbackLessons` builds a token Set from the task title + prompt and
a token Set from each lesson (via `feedbackTokens`, a regex + CJK n-gram
tokenizer) and intersects. That is tokenize-per-task-per-lesson on every poll
where either tasks or lessons change (which is every non-304 board poll,
since both come from the board response). On a workspace with T=100 tasks and
L=50 lessons this is 5000 tokenize passes per 2.5s — not catastrophic but it
is work done to render a one-line label that could be cached.

**Proposed fix:** pre-tokenize lessons once when `feedbackLessons` changes
(memoize an array of `{lesson, tokens}`), then iterate tasks × pre-tokenized
lessons. Even better: cap the check to open/selected/working+queued columns,
or memoize per task id so re-renders don't re-tokenize unchanged tasks.

### PR-08 — Overlapping agent-status polls in workspace mode (low / S)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:6122-6123`,
`frontend/src/stores/terminalStore.ts:261-278`,
`frontend/src/components/AgentStatusFloatingPanel.vue:427` (implicit poll
owner in terminal mode).

**Symptom:** The terminalStore `statusPollTimer` is ref-counted
(`statusPollConsumers`). In terminal mode, ASFP mounts and calls
`startAgentStatusPolling()` (consumers=1, poll runs). In workspace mode, AWV
mounts and calls it again (consumers=2, still one 5s interval). The
workspace board poll runs every 2500 ms and returns `board.sessions[]` that
already contains `runtime_status`, `status`, `queued_count`, `target` per
session. Two consumers in the frontend use the two data sources:
- Board cards in AWV read `board.sessions[*]`;
- ASFP chips read `agentStatuses` from terminalStore.

This is a correctness problem waiting to happen (state can desync if one poll
succeeds and the other fails) as well as a redundant HTTP request every 5s.

**Proposed fix:** narrow scope first — when AWV is mounted and the board
poll is running, either:
1. Don't bump `statusPollConsumers` from AWV:6122 (ASFP already starts
   polling when it mounts, and the board poll is already the richer source
   for workspace header cards), or
2. Stop ASFP from starting polling when inside workspace mode (have it read
   from board.sessions instead), or
3. Long-term: derive agentStatuses from board.sessions in workspace mode,
   drop the dual poll entirely.

The reference-counting machinery already supports (1) cleanly. (2)/(3) are
slightly larger refactors because ASFP is shared with terminal mode.

### PR-09 — `fetchRemoteProfiles` refetched from 8 sites, no in-flight dedup (low / S)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:4900-4919,
5140, 5156, 5486, 5498, 5505, 6070, 6091, 6120`.

**Symptom:** `fetchRemoteProfiles()` is defined locally in AWV (not in a
store) and does a bare `fetch('/api/remote/profiles')` with no in-flight
promise coalescing, no ETag, and no early-return if already loaded. It is
called: once on mount (6120), once on every modal open that needs profiles
(5 lines in the 5140-5505 range, including env/agent/task modals), and from
two watchers on workspace form fields (6070, 6091). Rapidly opening modals or
editing workspace fields can fire multiple concurrent GETs.

**Proposed fix:**
1. Hoist `remoteProfiles` + `fetchRemoteProfiles` into the app/workspace
   store with the same in-flight promise-coalescing map pattern used by
   `boardFetches` / `taskDetailFetches` (workspaceStore:75, 83, 89).
2. If already loaded, return the cached list; otherwise coallesce onto a
   single in-flight promise.
3. Call once per workspace mount, not per modal open.

### PR-10 — Kanban todo/queued columns unbounded; only done collapses to 10 (low / M)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:3288-3306`
(`DONE_TASK_COLLAPSE_LIMIT=10`, `tasksForColumn` returns a capped slice for
done), `455, 478-488` (done collapse toggle), `496` (`.task-list` container,
no capped height / virtual scroll).

**Symptom:** The done column has a collapse limit (10), but the other four
columns (todo / queued / working / review) have no cap. The comment at
3288-3290 says "with 100+ completed tasks, rendering all done cards on
every poll can cause noticeable jank" — the same applies to todo/queued in
workspaces that queue many tasks (backlog scenarios). Each task card
includes multiple buttons, selects, avatars, dependency selects, and runs
the PR-02 linear scans.

The working/review columns are normally small by construction (agents cap
parallelism), but todo/queued can grow without bound.

**Proposed fix:**
- Short-term (M, low risk): apply the same `DONE_TASK_COLLAPSE_LIMIT` pattern
  to todo/queued with a "show N more" expander. Reuse the existing collapse
  machinery.
- Medium-term (L, higher reward): adopt a lightweight windowing strategy
  for columns that exceed ~50 cards (CSS content-visibility or a simple
  IntersectionObserver cull, no new dependency needed).
- Don't bring in a virtual-scroll library just for this.

### PR-11 — Heavy modal subtrees inline in 10 949-line AWV (low / M)

**Files:** `frontend/src/components/AgentWorkspaceView.vue:3091-3097`
(import block — note every helper component is a static import); modal
v-if blocks sprinkled through the template (workspace create/edit, add-task,
edit-task, lessons manager, agent manager, switch-env, file browser,
markdown preview, image lightbox, resident agent config, feedback-lesson
editor).

**Symptom:** AWV is 10 949 lines. Most of that length is modal/panel
subtrees behind `v-if` toggles that are not mounted on first render. They
are, however, statically imported child components and inline template, so:
- the AgentWorkspaceView JS chunk (207 KB raw / 58 KB gz) must be parsed/eval'd
  before first paint to workspace mode even though modals aren't open;
- Vue has to compile and reconcile all those VNode branches every render
  (the `v-if=false` branches return `null` but the patch tree still walks
  them);
- any extracted child component that pulls in additional code (file browser,
  markdown preview with artifact rendering) inflates the workspace chunk
  even if the user never opens it.

Unlike PR-04/PR-05 (shell chunk), this is the async workspace chunk so it
doesn't affect terminal-mode boot — but it does affect first workspace-view
TTI and code-size growth.

**Proposed fix:** extract each major modal as its own `.vue` file and
`defineAsyncComponent` it (or just `() => import(...)` the modal components
from AWV). Start with the heaviest modals that pull in unique code
(file-browser modal, markdown/artifact preview, lessons manager, resident
config). Each extraction is independent and parallelizable.

Risk: medium — extracting v-if blocks requires hoisting the v-model /
event-callback props cleanly; pay attention to Pinia store access (stores are
singletons so no prop-drilling is needed, which makes extraction easy).

### PR-12 — MarkdownContent re-parses + re-sanitizes per report per prop change (low / S)

**Files:** `frontend/src/components/MarkdownContent.vue:13-14,32-44`
(`safeHtml` computed runs `marked.parse` + `DOMPurify.sanitize` every time
`props.text` changes identity); AWV render sites at lines 804, 855, 1134,
1168, 1285, 1321, 1393, 1594 (eight `<MarkdownContent>` instances per
selected-task detail panel).

**Symptom:** when a task with 50 reports is opened, the detail panel mounts
~8 MarkdownContent instances per visible section (latest-run message, review
reason, risks, validation, per-report message/message_en/message_zh), each
running marked + DOMPurify on mount. Computed caching inside each instance
does prevent re-parse when unrelated state changes, but board polls that
replace the `reports[]` array (PR-01) will invalidate every report's `text`
prop reference even if the string is unchanged, causing a re-parse.

**Proposed fix:**
1. Stabilize report references across board polls when the report body is
   unchanged (this is a natural side effect of PR-01 — if reports are not in
   the board payload at all, only `fetchTaskReports` populates them and
   there's no per-poll invalidation).
2. Add a small LRU / Map cache keyed on the input string inside
   MarkdownContent so repeated reports with identical text (common in
   progress updates like "Working…") share a sanitized HTML string.
3. Passing `v-memo="[report.id]"` on the MarkdownContent instances in the
   report list avoids re-renders when other reports change.

Risk: low — MarkdownContent is 240 lines and has only one consumer shape.

---

## 3. Explicitly out of scope / intentional

These items looked at during the audit were rejected as findings:

- **"Bundle size is too big"** — total gzipped JS is ~125 KB for the
  workspace view and ~67 KB for terminal shell; well under any reasonable
  threshold. No third-party bloat (no icons, no router, no xterm in the Vue
  bundle).
- **"All polling intervals are too fast"** — 2500 ms board poll and 5000 ms
  terminal-status poll are reference-counted, ETag/304-gated (terminalStore
  fully, board partially), and have an `isTextEntryFocused` guard for mobile
  keystroke lag. They are appropriate for a live terminal/agent dashboard.
- **"Deep ttyd / xterm.js / WebSocket throughput work"** — terminals live in
  iframed ttyd; the parent Vue app only exchanges postMessage resize/focus
  events and uses a SharedArrayBuffer fast path for synthetic mobile
  keystrokes. Touching this is high risk and the iframe boundary already
  isolates terminal throughput from Vue reactivity. Explicitly flagged
  high-risk in the task prompt.
- **"v-for without :key"** — grepped all lists; every dynamic list has
  `:key`. Only trivial static-range loops (layout grid dots, 3 skeleton
  cards, static GP sections) use index keys and those are bounded to <10.
- **"Add a virtual-list library"** — the only list that can plausibly grow
  to hundreds of cards is the Kanban todo/queued column (PR-10); capping
  with an expander or CSS `content-visibility` is enough without a new dep.
- **"Skeleton / loading state for task detail"** — the board skeleton
  (AWV:417-446) covers first load; the detail panel renders against the
  board's task stub and fills in as `taskDetail` / `taskReports` resolve,
  which is the correct optimistic pattern. No additional skeleton needed.
- **"Debounce every input"** — form inputs are v-model-local with
  explicit Save/Enter submission (workspace rename, task edit, tab rename);
  only discrete `@change` events dispatch store actions. There is no
  search-as-you-type pattern to debounce.
- **"NetworkAccessMenu (655 lines) in shell chunk"** — inspected; it is
  mounted in the `<details>` disclosure in the App shell header
  (App.vue:70) **outside** of any `v-if` (its container is always in-DOM,
  only the panel is hidden via the native `<details>` toggle). Because of
  how App.vue wires it (rendered into the top bar by default), async-loading
  it would require restructuring the disclosure; leaving as-is. Its cost is
  also mostly CSS and a handful of form elements. Can revisit if PR-04/05
  move the needle enough that the remaining shell cost needs more work.
- **"iOS Safari keyboard / mobile viewport perf"** — covered by the existing
  `on-ios-safari…` lesson and the prior mobile-audit track; out of scope for
  this desktop-focused response-speed sweep.
- **"Re-documenting already-shipped wins"** (SL-12 ETag, board-payload-slim,
  AWV lazy chunk, terminal iframe cap, SAB keystroke fast path, rAF resize)
  — intentionally not listed again; see §1.1 for prior commits.

---

## 4. Dispatch notes

All 12 findings file to a small set of owning components/stores:

| File(s) | PRs |
| --- | --- |
| `backend/claude_hub/api/workspaces.py` | PR-01 |
| `frontend/src/stores/workspaceStore.ts` | PR-01, PR-02, PR-03, PR-09 (if hoisted) |
| `frontend/src/stores/terminalStore.ts` | none (already sound post SL-12; PR-08 touch is trivial) |
| `frontend/src/components/TabBar.vue` | PR-04, PR-05 |
| `frontend/src/components/AgentWorkspaceView.vue` | PR-02, PR-06, PR-07, PR-08, PR-09, PR-10, PR-11 |
| `frontend/src/components/MarkdownContent.vue` | PR-12 |
| `vite.config.ts` | PR-05 (optional `manualChunks` alternative) |

Recommended dispatch order:
1. **Batch A — store cleanup, can ship in one PR or three parallel PRs:**
   PR-01 (board field projection, needs backend change), PR-02 (memoize
   maps), PR-03 (lessons ETag). Highest leverage; independent of UI changes.
2. **Batch B — shell lazy-loading:** PR-04 + PR-05 (both touch TabBar
   imports; combine into one PR to avoid churn).
3. **Batch C — AWV render hotspots, parallelizable:** PR-06, PR-07, PR-09,
   PR-12 (each touches disjoint lines/functions; can run in parallel with
   normal conflict resolution since none move code).
4. **Batch D — deferred / larger:** PR-08 (decide on dedup direction first),
   PR-10 (column collapse extension), PR-11 (modal extractions — can be
   split one-modal-per-PR for incremental wins).

Every PR is scoped to CSS/TS/Vue edits that match the established style
(Composition API, `storeToRefs`, `var(--ch-*)` tokens) and can be validated
with the existing `pnpm lint && pnpm build` path plus a port-dedicated dev
smoke.
