# Perf-Backlog Reconciliation (read-only)

**Task:** `24beba03-c030-4d29-b746-590a7e4fe2be` — verify the current state of the
"response-speed" perf-branch fleet + audit-doc drift, and produce a
human-facing merge-readiness picture **before** any human merge.

**Scope guarantee:** strictly read-only w.r.t. all app code and every perf/feature
branch. No app code was modified, no perf/feat branch was touched, nothing was
merged, nothing was pushed to `main`. The **only** artifact created is this file
on the new docs-only branch `docs/perf-backlog-recon` (cut from `main`).

**Snapshot references (all captured this session):**

| Ref | SHA | Note |
| --- | --- | --- |
| `main` (local == SSH remote) | `71b7dbc` | live-deployed tip; +7 commits `develop` lacks |
| `develop` (local only) | `ec30c3d` | integration branch; +100 commits vs `main` |
| `merge-base(main, develop)` | `59fa368` | divergence point — promotion is a **merge, not a fast-forward** |
| `perf/rs01-agent-config-static-edge` | `dbeb2af` | on SSH remote; develop-tip + 21 commits |

---

## 0. Two divergences from the task framing (surfaced & verified)

The task named an "outstanding" fleet of `feat/pr08/09/11/12/14` to be merged.
Ground truth differs on two points (both were recorded as Goal-Packet assumptions
and approved by the reviewer):

- **A — the fleet is NOT on the SSH remote.** `git ls-remote --heads
  git@github.com:Pegessi/claude_hub.git` returns 30 heads; the only
  perf-relevant ones are `main` (`71b7dbc`) and
  `perf/rs01-agent-config-static-edge` (`dbeb2af`). `develop` and every
  `feat/pr*` / `perf/pr*` branch are **local-only worktree branches** — never
  pushed. Delivery target for the perf work is therefore **the develop→main
  promotion**, not the individual feat branches.
- **B — the audit doc lives on `develop`, not `main`.**
  `docs/working-logs/2026-07-11-frontend-response-speed-audit.md` exists on
  `develop` (baselined at `develop @ 9fda1fa`) and is absent from `main`.

**Net reframing:** every `feat/pr*` and `perf/pr*` branch the task named is
**already contained in `develop`** (`git branch --contains` / tip-in-history =
True for all). The genuine open merge question is **`develop` → `main`** (plus the
`rs01` stack that sits on top of `develop`). Individually merging the feat
branches would be redundant no-ops.

---

## 1. Merge-readiness matrix

Status legend: **already-landed** = branch tip is in `develop` history •
**outstanding-clean** = not in `develop`, merges with only trivial conflicts •
**outstanding-conflicts** = not in `develop`, real code conflicts •
**superseded** / **stale** as labelled.

For every already-landed branch: `merge-base(branch, develop) = branch tip`
(fully contained) and `merge-base(branch, main) = 59fa368`.

| Branch | Tip SHA | On SSH remote? | Status | Recommended action | Measured perf impact | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| **`develop`** | `ec30c3d` | ❌ no | **outstanding-clean** (vs `main`) | **MERGE → `main` (1st)** | shell JS **67.4→29.7 KB gz** (see §4) | Carries the entire landed PR-01…PR-12 perf set; only conflict vs main is `CHANGELOG.md`. This is THE promotion. |
| `feat/pr08-workspace-status-poll` | `15e0ceb` | ❌ no | already-landed | DROP (delete local branch) | in develop measurement | Tip in develop history; PR-08 poll-guard live in AWV. |
| `feat/pr09-remote-profiles-cache` | `9eb7ec9` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; `fetchRemoteProfiles({force})` cache live. |
| `feat/pr11-async-modal-components` | `b403823` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; async config panels live (see PR-11 caveat §3). |
| `feat/markdown-content-cache-pr12` | `b46f042` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; `htmlCache` live in MarkdownContent. |
| `feat/pr14-tabbar-async-config` | `ec30c3d` | ❌ no | already-landed (== develop tip) | DROP | in develop measurement | Its tip **is** develop's tip; nothing to merge. |
| `perf/pr02-lookup-maps` | `176a4b1` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; `sessionByTaskId`/`reportsByTaskId` maps live. |
| `perf/pr04-asfp-lazy-clean` | `b08d6f0` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; ASFP `defineAsyncComponent` live. |
| `perf/pr05-manual-chunks` | `fe0a6e7` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; `agent-config` manualChunks rule live. |
| `perf/ui-pr10-bound-todo-queued-columns` | `e6f574a` | ❌ no | already-landed | DROP | in develop measurement | Tip in develop; `COLUMN_COLLAPSE_LIMIT` live. |
| **`perf/rs01-agent-config-static-edge`** | `dbeb2af` | ✅ yes | **outstanding-clean** (on top of develop) | **MERGE → `main` (2nd, after develop)** | unmeasured (see §4) | `develop` is a clean ancestor; adds 21 unique commits (RS-01 bundle fix + round-3/4/6 UI polish + audit A-series). Conflicts vs *raw* main (AWV/TabBar) dissolve once develop lands first. |

**PR-06 known-finding verdict (task asked to confirm/refute): CONFIRMED
superseded → no-op.** See §2 PR-06.

---

## 2. Audit-doc drift (`2026-07-11-frontend-response-speed-audit.md`)

The doc is baselined at `develop @ 9fda1fa`; develop-tip is now `ec30c3d` **after
the entire PR-01…PR-12 set landed**, so nearly every cited `file:line` has
drifted. Each row verified against develop-tip source this session.

| PR | Audit cited | Develop-tip reality | Landed? | Drift |
| --- | --- | --- | --- | --- |
| PR-01 | "backend board endpoint"; `workspaceStore.ts:96,246`; `types/index.ts:559-564`; proposes `reports_lite` | Board projection is `_board_report_projection` in **`services/workspace_manager/_tmux_queries.py:884-905`** + `schemas.py:977 resident_report`; store `residentReport` at **L125/902** | ✅ **landed** (via `resident_report` projection scalar, not the proposed `reports_lite`) | **DRIFT**: wrong backend file; store lines 96/246→125/902 |
| PR-02 | `workspaceStore.ts:148-155`; AWV template lines | `sessionByTaskId` L183, `reportsByTaskId` L204, `sessionForTask` L229, `reportsForTask` L234 (exact proposed maps, with O(1) comments) | ✅ **landed** (as proposed) | **DRIFT**: 148-155 → 180-247 |
| PR-03 | `workspaceStore.ts:240,247,321-381`; proposes ETag + dedup | `lessonFetches` dedup map **present** (L100/450/487/494); `lessonsETag` **ABSENT** | ⚠️ **PARTIAL** — dedup shipped, ETag half never shipped | **DRIFT** + genuine outstanding sub-item |
| PR-04 | `TabBar.vue:641` static import | ASFP `defineAsyncComponent` at **TabBar L665-666** | ✅ **landed** | **DRIFT**: line |
| PR-05 | `TabBar.vue:650-651`, `AWV:3092-3093`; proposes shared async or manualChunks | `manualChunks` `'agent-config'` rule in **`vite.config.ts:74-86`** + modulePreload filter (L69) + async in both callsites | ✅ **landed** (chose the manualChunks option) | **DRIFT**: line; impl variant |
| **PR-06** | `AWV:6008-6018` — filter+spread+sort on every compute | **L6118**: `const latestResidentReport = computed(() => workspaceStore.residentReport)` — the scan is **gone**; store has **no** `latestReportBySessionId` | ✅ **superseded → NO-OP** | **DRIFT**: 6008-6018→6118; cited code no longer exists |
| PR-07 | `AWV:3446-3491,3539-3544` | `feedbackTokens` L3502, memoized `tokenizedLessons` L3531-3538, `matchingFeedbackLessonsFor` L3564 | ✅ **landed** (pre-tokenize memo) | **DRIFT**: lines |
| PR-08 | `AWV:6122-6123`; ASFP:427 | PR-08 guard comment at **AWV:6256**; `startAgentStatusPolling()` removed from AWV mount | ✅ **landed** | **DRIFT**: line |
| PR-09 | `AWV:4900-4919,…` local fetch, no dedup | Hoisted to store: `fetchRemoteProfiles({force?})` at **`workspaceStore.ts:406`** | ✅ **landed** (hoisted as proposed) | **DRIFT**: moved AWV→store |
| PR-10 | `AWV:3288-3306` `DONE_TASK_COLLAPSE_LIMIT` | Generalized to **`COLUMN_COLLAPSE_LIMIT`** L3335 + `isCollapsibleColumn`/`columnCollapsedCount` L3355 | ✅ **landed** (generalized to all collapsible columns — beyond proposal) | **DRIFT**: name + line |
| PR-11 | `AWV:3091-3097` static; "13+ modal v-if" to extract | Only `EnvPresetManager`+`AgentConfigFields` converted to async (L3122-3125) — overlaps PR-05/14 | ⚠️ **PARTIAL** — shared config panels async; the broad file-browser/markdown-preview/lessons/resident modal extraction **NOT done** | **DRIFT** + genuine outstanding (low priority) |
| PR-12 | `MarkdownContent.vue:13-14,32-44` | `htmlCache` Map at **L47-48** (`cached !== undefined` short-circuit) | ✅ **landed** | **DRIFT**: line |

**Drift summary:** **all 12** PR-NN refs have drifted line numbers/locations
(expected — the doc predates the landings). **10 landed fully**, **2 partial**
(PR-03 ETag half; PR-11 heavy-modal extraction). This is a textbook case of the
`project_perf_audit_doc_stale_verify_first` lesson: the doc's `file:line` anchors
are stale and must not be trusted for dispatch without re-verification — which is
now moot since the work landed.

---

## 3. Genuinely outstanding perf work (not yet in develop)

Only two items remain open; both are *increments on top of the landed set*, not
whole PRs:

1. **PR-03 ETag half** — `lessonsETag` / `If-None-Match` on
   `GET /api/workspaces/:id/lessons` was never added (dedup landed without it).
   ~20 lines mirroring SL-12; med-value.
2. **PR-11 heavy-modal extraction** — only the two shared agent-config panels
   were made async (that overlaps PR-05/14). The audit's larger proposal to
   `defineAsyncComponent` the file-browser / markdown-preview / lessons /
   resident-config modals out of the 10 949-line AWV is **not done**. Low
   priority (async workspace chunk, doesn't affect terminal-mode boot).

Everything else the audit proposed is live in `develop`.

---

## 4. Measured impact

**Cheaply measurable — develop-tip production build** (`pnpm build`, develop
worktree, this session) vs the audit's recorded `develop @ 9fda1fa` baseline:

| Asset | Audit baseline (`9fda1fa`) gz | Develop-tip (`ec30c3d`) gz | Δ |
| --- | --- | --- | --- |
| `index-*.js` (initial shell) | 67.43 KB | **29.68 KB** | **−37.75 KB gz** |
| `AgentWorkspaceView-*.js` (async chunk) | 58.04 KB | 58.74 KB | +0.70 KB (noise) |
| new on-demand `agent-config-*.js` | — | 34.90 KB | deferred off shell |
| new on-demand `AgentStatusFloatingPanel-*.js` | — | 2.89 KB | deferred off shell |
| new on-demand `AgentAvatar-*.js` | — | 2.21 KB | deferred off shell |

The initial terminal-mode shell chunk dropped **~37.7 KB gz (≈56%)** by deferring
the agent-config panels, ASFP, and AgentAvatar into on-demand chunks — the
cumulative effect of the landed PR-04/05/11/14 shell work. The async workspace
chunk is unchanged (expected: it was already lazy).

**Not cheaply measurable — per-branch tip deltas & rs01 build.** Each already-landed
feat branch's isolated contribution cannot be re-measured without checking out and
building each in turn (no node_modules; per-branch build cost is not "cheap" per
the task's own guard). rs01's build delta on top of develop is **unmeasured** — I
did not build it. Not guessing numbers.

---

## 5. Recommended merge order & drop list

**Merge order (human-executed, human-approved):**

1. **`develop` → `main`** — the high-value promotion. Brings the full landed
   PR-01…PR-12 perf set (and the 100-commit develop delta) to the deployed
   `main`. Conflict surface (read-only `git merge-tree --write-tree main develop`):
   **only `CHANGELOG.md`** (append-both; `AGENTS.md` auto-merges). Note `main`'s
   7 unique commits (hard-context-recovery, codex/claude restart-solo,
   clear-context-reviewer — backend/workspace fixes) are genuinely main-only
   (`git cherry` all `+`) and must be preserved through the merge; two of them
   (`2d034f6`) touch AWV.vue + TabBar.vue, so resolve with care.
2. **`perf/rs01-agent-config-static-edge` → `main`** — *after* step 1. `develop`
   is a clean ancestor of rs01, so once develop lands, rs01's remaining delta is
   just its 21 unique commits (RS-01 bundle fix + round-3/4/6 UI polish + audit
   A-series). Against *raw* main, `merge-tree` shows conflicts in `CHANGELOG.md`
   + `AgentWorkspaceView.vue` + `TabBar.vue` — but those largely dissolve once
   develop is already in main. Re-test conflicts after step 1.

**DROP (no merge — already contained in `develop`; delete stale local branches
once `develop` lands):**

- `feat/pr08-workspace-status-poll` (`15e0ceb`)
- `feat/pr09-remote-profiles-cache` (`9eb7ec9`)
- `feat/pr11-async-modal-components` (`b403823`)
- `feat/markdown-content-cache-pr12` (`b46f042`)
- `feat/pr14-tabbar-async-config` (`ec30c3d`, == develop tip)
- `perf/pr02-lookup-maps` (`176a4b1`)
- `perf/pr04-asfp-lazy-clean` (`b08d6f0`)
- `perf/pr05-manual-chunks` (`fe0a6e7`)
- `perf/ui-pr10-bound-todo-queued-columns` (`e6f574a`)

Merging any of these individually is a redundant no-op — their tips are already
in `develop`'s history.

**No superseded-and-should-drop *outstanding* branch** exists other than the
PR-06 *finding* (which is code-superseded by PR-01, already reflected in develop —
nothing to drop at the branch level).

---

## 6. Follow-ups for the human (optional, out of this read-only task's scope)

- After `develop` → `main`, the audit doc's `file:line` anchors will drift
  further; consider marking PR-01…PR-12 "landed" in the doc or archiving it.
- The two genuine remainders (§3) — PR-03 lessons ETag, PR-11 heavy-modal
  extraction — can be dispatched as small follow-ups if the response-speed prong
  is pursued further.
- If a precise rs01-vs-develop bundle delta is wanted, build rs01-tip in a
  throwaway worktree (not done here — labelled unmeasured, not guessed).

---

*Generated read-only. No app code, no perf/feat branch, and no merge was performed.
Sole artifact: this file on `docs/perf-backlog-recon` (cut from `main` @ `71b7dbc`).*
