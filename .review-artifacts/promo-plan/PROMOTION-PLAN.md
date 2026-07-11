# Unified Promotion Plan (read-only)

**Task:** `586f9cbf-4aa8-488f-afeb-56263e876e22` — with BOTH the perf line and
the design line waiting on human `→ main` promotion, determine (with git
evidence) whether they collide, produce a file/hunk-level collision map, and
fold everything into a **single ordered promotion sequence**.

**Scope guarantee:** strictly read-only w.r.t. all app code and every
perf/design branch. No app code was modified. No merge was performed. No push
to `main`/`develop`/any `perf/*` or `style/ui-*` branch. Conflict analysis used
`git merge-tree --write-tree` (writes objects to the store, touches no ref) plus
`git commit-tree` to build **dangling** commit objects for the two-step
order-sensitivity simulation (no branch, no ref, nothing reachable). The **only**
new ref is `docs/unified-promotion-plan`; the **only** new file is this
`PROMOTION-PLAN.md`, cut from `main`.

---

## 1. Tips (verified this session)

SHA source: `git rev-parse`. SSH presence: `git ls-remote --heads
git@github.com:Pegessi/claude_hub.git` (SSH identity confirmed —
`ssh -T git@github.com` → "Hi Pegessi!").

| Ref | SHA | On SSH remote? | Notes |
| --- | --- | --- | --- |
| `main` | `71b7dbc5660be4178922ee4a4d4b83ddac7aa259` | ✅ yes | Live-deployed tip. +7 commits that `develop` lacks (backend/recovery fixes). |
| `develop` | `ec30c3daf8d14b68d988c681c5123e119fc3ace3` | ❌ **local-only** | Integration branch; carries the entire landed PR-01…PR-12 perf fleet. Never pushed. |
| `perf/rs01-agent-config-static-edge` | `dbeb2aff05a95c4aa14df25d68c8d253f5665bc1` | ✅ yes | develop tip + 21 unique commits (RS-01 bundle fix + round-3/4/6 UI polish + audit A-series). |
| `style/ui-round9-wave2` (design tip) | `2f71d9a153735d7c5114e69b12752abd2a9b29d5` | ✅ yes | develop tip + 5 CSS-only commits. See §2. |
| `docs/perf-backlog-recon` (prior TASK-D artifact) | `487ba6a` | ✅ yes | RECON.md; re-verified below. |

Merge bases:
- `merge-base(develop, main)` = `59fa368` — develop→main is a **merge, not a fast-forward**.
- `merge-base(2f71d9a, develop)` = `ec30c3d` = **develop tip exactly**.
- `merge-base(2f71d9a, main)` = `59fa368`.
- `merge-base(rs01, develop)` = `ec30c3d` = **develop tip exactly**.

---

## 2. Critical structural finding: the design line ⊇ develop

The task frames "two independent lines." Git says otherwise, and it changes the
whole plan:

- `develop..2f71d9a` = **5 commits** (all CSS): `cdb5056` (R8 wave-1) →
  `a3e7fa3` (R8 wave-2) → `5c869c4` (stripe-axis) → `a3b96d3` (R9 wave-1) →
  `2f71d9a` (R9 wave-2).
- `2f71d9a..develop` = **0 commits**.

So **`style/ui-round9-wave2` = `develop` + 5 CSS-only commits.** Promoting the
design tip *already carries the entire develop/perf fleet.* The design line is
not independent of the perf line — it is a superset of `develop`.

Likewise `rs01 = develop + 21 commits`. Both the design line and the rs01 stack
**fork from the same point** (`develop` tip `ec30c3d`). Therefore the genuine
cross-line collision question is **design × rs01**, both rebased conceptually
onto `develop` — not "design × develop" (design contains develop) and not
"design × perf-fleet" (the fleet is inside develop).

---

## 3. File sets

### 3a. Design line (`develop..2f71d9a`, `git diff --numstat`) — CSS only

| File | +/− | Intent (one line) |
| --- | --- | --- |
| `frontend/src/components/AgentWorkspaceView.vue` | 24 / 27 | Round-8/9 minimalist CSS: token-ize colors/spacing, flatten borders. |
| `frontend/src/components/EnvPresetManager.vue` | 24 / 17 | `.btn` + preset-row restyle to standard-motion tokens. |
| `frontend/src/components/LayoutSelector.vue` | 3 / 19 | Strip decorative rules; token-ize. |
| `frontend/src/components/TabBar.vue` | 5 / 17 | `.btn` transition token cleanup (no press state on this line). |
| `frontend/src/components/TerminalGridView.vue` | 1 / 1 | One-line stripe-axis token. **design-only — no rs01 overlap.** |
| `frontend/src/views/LoginView.vue` | 6 / 2 | h1 font-size → rem + weight token + doc-comment rewrite. |

No `.js`/`.ts`/config/dependency changes on the design line — pure `<style>`
+ template-class edits.

### 3b. Perf/rs01 line (`develop..rs01`, 22 files) — functional + UI

Notable (churn = insertions):
`App.vue` 23 · `AgentConfigFields.vue` 4 · `AgentStatusFloatingPanel.vue` 19 ·
**`AgentWorkspaceView.vue` 4271** · `BaseModal.vue` 211 (new) ·
`EmptyState.vue` 104 (new) · `EnvPresetManager.vue` 5 · `LayoutSelector.vue` 6 ·
`LoadingButton.vue` 2 · `MobileControls.vue` 5 · `NetworkAccessMenu.vue` 2 ·
**`TabBar.vue` 763** · `TerminalView.vue` 354 · `terminalStore.ts` 36 ·
`workspaceStore.ts` 7 · `LoginView.vue` 16 · `vite.config.ts` 26 · plus
`CHANGELOG.md`, `.github/workflows/ci.yml`, `frontend/package.json`,
`frontend/scripts/verify-chunk-split.mjs` 684 (new), `.review-artifacts/VALIDATION.md` 106 (new).

### 3c. develop line vs main (`main..develop`, frontend churn) — context

`App.vue` 225/12 · `AgentWorkspaceView.vue` 1908/490 · `TabBar.vue` 347/185 ·
`EnvPresetManager.vue` 81/55 · `LayoutSelector.vue` 74/25 ·
`workspaceStore.ts` 278/7 · `vite.config.ts` 31/0 · `LoginView.vue` 39/15 ·
`MarkdownContent.vue` 63/26. Only conflict vs main is `CHANGELOG.md` (§5).

### 3d. main's 7 unique commits (`develop..main`) — must be preserved

`git cherry` shows all 7 as `+` (genuinely main-only): hard-context-recovery,
codex/claude env restart-solo, clear-context-reviewer — backend/workspace
fixes. **One touches frontend:** `2d034f6` *"fix: env restart solo mode for
claude; add codex env restart with solo mode"* edits
`AgentWorkspaceView.vue` (+15/−5) and `TabBar.vue` (+12/−2). This is the source
of the *functional* rs01↔main conflict (§4, step 2).

---

## 4. Collision map

Method: `git merge-tree --write-tree <a> <b>` (auto-computes the real
merge-base; CONFLICT lines emitted on stdout after the tree OID; exit 1 ⇒
conflict). Order sensitivity verified by building dangling merge commits with
`git commit-tree` and re-running merge-tree against them (§4b).

### 4a. Pairwise (each line vs the target it lands on)

| Merge | Real base | Result | Conflicting files |
| --- | --- | --- | --- |
| `develop` → `main` | `59fa368` | CONFLICT | `CHANGELOG.md` only |
| `2f71d9a` (design) → raw `main` | `59fa368` | CONFLICT | `CHANGELOG.md` only¹ |
| `rs01` → raw `main` | `59fa368` | CONFLICT | `CHANGELOG.md`, `AgentWorkspaceView.vue`, `TabBar.vue` |
| **`design` × `rs01`** (both fork develop) | `ec30c3d` | CONFLICT | `EnvPresetManager.vue`, `TabBar.vue`, `LoginView.vue` |

¹ Design → raw main conflicts only on CHANGELOG because design *is* develop+5-CSS
and its CSS edits don't touch main's `2d034f6` functional lines; but design is
never promoted directly to raw main in the recommended plan — it lands after
develop (§4b), where it is **clean**.

### 4b. Order-sensitivity (two-step object-space simulation)

Built dangling commit `C1 = merge(main, develop)` (tree `92352d7`), then merged
each line into `C1` (base now shifts to develop tip `ec30c3d`):

| Step (after develop already in main) | Result | Conflicting files |
| --- | --- | --- |
| then `rs01` → main | CONFLICT | `CHANGELOG.md`, `AgentWorkspaceView.vue`, `TabBar.vue` |
| then `design` → main | **CLEAN (exit 0)** | — none — |

Then continuing to the third merge:

| Third step | Result | Conflicting files |
| --- | --- | --- |
| develop+**rs01** landed, then `design` → main | CONFLICT | `EnvPresetManager.vue`, `TabBar.vue`, `LoginView.vue` |
| develop+**design** landed, then `rs01` → main | CONFLICT | `CHANGELOG.md`, `AWV.vue`, `EnvPresetManager.vue`, `TabBar.vue`, `LoginView.vue` |

**Reading:** order matters. Once develop lands, design-vs-main is *clean* — the
CSS commits never fight main's `2d034f6`. The rs01-vs-main functional conflict
(AWV+TabBar, from `2d034f6`) is unavoidable and independent of design. The
design×rs01 CSS conflict (EnvPresetManager/TabBar/LoginView) is also unavoidable
and appears in whichever of the two lands *second*. Landing **rs01 before
design** keeps the two conflict classes separated (step 2 = functional only,
step 3 = CSS only). Landing **design before rs01** collapses both classes into
the final rs01 merge, where `TabBar.vue` then carries *both* the functional
(`2d034f6`) and the CSS (press-state) conflicts interleaved in one file — strictly
harder to hand-resolve.

### 4c. Concrete conflict hunks

**Class A — functional (rs01 × main `2d034f6`), appears when rs01 lands on main:**

- `AgentWorkspaceView.vue` (+15/−5 on main side): env-restart-solo-mode logic vs
  rs01's 4271-line UI refactor of the same file. Resolve by **keeping main's
  functional env-restart edit** and re-applying it inside rs01's restructured
  markup/handlers.
- `TabBar.vue` (+12/−2 on main side): same `2d034f6` env-restart control vs
  rs01's 763-line TabBar rework. Same resolution: preserve main's behavior atop
  rs01 structure.

**Class B — CSS token divergence (design × rs01), appears when the second of the
two lands:**

- `EnvPresetManager.vue` — `.btn` transition. design=`transition:
  background-color var(--ch-motion-standard);` vs rs01=`transition:
  background-color var(--ch-motion-fast);`. **Resolution hint:** pick one motion
  token (rs01's `--ch-motion-fast` is the newer perf-line intent); 1-line pick.
- `TabBar.vue` — `.btn` transition + press state. design=`transition:
  background-color var(--ch-motion-standard), opacity var(--ch-motion-standard);`
  vs rs01 adds `transform 80ms ease` to the transition **and** a new
  `.btn:active:not(:disabled) { transform: translateY(1px); }` press affordance.
  **Resolution hint:** take rs01's superset (keeps the press state) and, if the
  design line's standard-motion opacity is preferred, merge the two transition
  lists — union, not either/or.
- `LoginView.vue` — 2 hunks: (a) the CSS doc-comment block (design describes
  rem-based sizes; rs01 describes the `--ch-font-*` px scale) — **prose only,
  keep rs01's description of the px-token reality**; (b) `h1` font-size:
  design=`font-size: 2rem; font-weight: var(--ch-weight-semibold);` vs
  rs01=`font-size: var(--ch-font-2xl);`. **Resolution hint:** pick the token form
  (`--ch-font-2xl`) to match rs01's px-scale, decide whether to keep the explicit
  weight; 2-line pick.

All Class-B conflicts are tiny single-property / token-swap divergences,
hand-resolvable in minutes. `TerminalGridView.vue` and `LayoutSelector.vue` are
**not** in any conflict set (LayoutSelector auto-merges; TerminalGridView is
design-only).

---

## 5. develop → main conflict detail (`CHANGELOG.md`)

`git merge-tree --write-tree main develop` → single CONFLICT in `CHANGELOG.md`
(append-both: main added 7-commit entries, develop added the perf-fleet entries
under overlapping headings). Trivial: keep both sections. `AGENTS.md`,
`CLAUDE.md`, all backend files, `AgentWorkspaceView.vue`, `TabBar.vue`
auto-merge at this step. (Re-confirms prior `docs/perf-backlog-recon` RECON.md.)

---

## 6. Single ordered promotion sequence (human-executed, over SSH)

Push URL is SSH (`git@github.com:Pegessi/claude_hub.git`); origin's embedded
HTTPS PAT is expired — all pushes must go over SSH. `develop` is local-only, so
step 1 promotes local `develop`.

> **Chosen order: develop → rs01 → design.** Rationale in §4b: this isolates the
> single hard *functional* merge (rs01 × main's `2d034f6`) into step 2 with only
> AWV+TabBar, and leaves step 3 (design) as pure trivial CSS-token picks. The
> alternative (develop → design → rs01) forces `TabBar.vue` to carry both the
> functional and the CSS conflict in one file at the final step — avoid it.

**Step 0 — sync & branch.** `git fetch` (HTTPS ok for fetch). Confirm tips match
§1. Create a throwaway integration branch off `main` if you don't want to resolve
on `main` directly.

**Step 1 — `develop` → `main`.**
- Expected conflict: **`CHANGELOG.md` only** (append-both; keep both sections).
- Preserve main's 7 unique commits — they survive the merge automatically
  (they're on the `main` side); `2d034f6`'s AWV/TabBar edits auto-merge here.
- Build + smoke-test, commit, `git push git@github.com:… main`.
- Verify `git ls-remote` main == local.

**Step 2 — `perf/rs01-agent-config-static-edge` → `main`** (after step 1).
- Expected conflict (verified against develop-landed main): **`CHANGELOG.md`,
  `AgentWorkspaceView.vue`, `TabBar.vue`** — the *functional* Class-A conflicts
  (rs01's UI churn × main's `2d034f6` env-restart-solo fix). Resolve by
  preserving main's env-restart behavior atop rs01's restructured code (§4c).
- This is the one non-trivial merge. Build, run the rs01 chunk-split verifier
  (`frontend/scripts/verify-chunk-split.mjs`) + `.review-artifacts/VALIDATION.md`
  checks, commit, push over SSH, verify ls-remote.

**Step 3 — `style/ui-round9-wave2` (design) → `main`** (after steps 1-2).
- Expected conflict: **`EnvPresetManager.vue`, `TabBar.vue`, `LoginView.vue`** —
  the *CSS-token* Class-B conflicts only (§4c). Each is a 1–2 line token pick;
  no logic touched.
- Note: design would be *clean* against develop-only main, but rs01 (step 2)
  introduced the CSS-token deltas it collides with — so these appear here by
  construction. Resolve per §4c hints, build (visual check on the round-9
  minimalist styling), commit, push over SSH, verify ls-remote.

**After all three:** delete the now-redundant local branches whose tips are
contained in the promoted history (the `feat/pr*` / `perf/pr*` fleet — all inside
`develop`; see prior RECON.md §5 drop list). `develop` may be re-based onto the
new `main` or retired.

### Order summary table

| # | Merge | Expected conflicts | Class | Difficulty |
| --- | --- | --- | --- | --- |
| 1 | develop → main | `CHANGELOG.md` | trivial append | minutes |
| 2 | rs01 → main | `CHANGELOG.md`, `AWV.vue`, `TabBar.vue` | functional (`2d034f6`) | the one careful merge |
| 3 | design → main | `EnvPresetManager.vue`, `TabBar.vue`, `LoginView.vue` | CSS token pick | minutes |

---

## 7. Order-sensitivity verdict

**Yes, order changes the work.** The conflict *set* is not symmetric across
orderings:

- develop **must** go first — it dissolves rs01's and design's spurious conflicts
  against raw main's `59fa368` base and establishes develop tip as the shared
  base for steps 2-3.
- With develop landed, **rs01-then-design** cleanly separates the functional
  merge (step 2: AWV+TabBar) from the CSS merge (step 3: EnvPresetManager /
  TabBar / LoginView), so no single file carries two conflict classes at once.
- **design-then-rs01** is worse: the final rs01 merge then conflicts in 5 files
  including `TabBar.vue` carrying *both* the `2d034f6` functional hunk and the
  press-state CSS hunk simultaneously.

Recommended: **develop → rs01 → design.**

---

## 8. Compliance statement

- **No code modified.** No file outside this `PROMOTION-PLAN.md` was written. No
  app code, no `develop`/`main`/`perf/*`/`style/ui-*` branch was edited, merged,
  or pushed.
- Conflict analysis used only read-only object-space operations
  (`git merge-tree --write-tree`, `git commit-tree` producing **dangling**
  commits with no ref) — nothing reachable, nothing pushed.
- Any perf figure referenced (shell chunk 67.4→29.7 KB gz) is carried over from
  the prior `docs/perf-backlog-recon` RECON.md **measured** build; rs01's
  isolated build delta is **unmeasured** (not built this task) — not guessed.
- Sole new ref: `docs/unified-promotion-plan` (cut from `main` `71b7dbc`). Sole
  new file: this document. Pushed docs-branch SHA recorded below after push
  (ls-remote-verified).

**Pushed docs-branch SHA:** `910fdeb65591ace803ddba3415c0c16806d1c1bf` on
`docs/unified-promotion-plan` (cut from `main` `71b7dbc`). Verified: SSH push
rc 0, `ssh -T git@github.com` → "Hi Pegessi!", `git ls-remote --heads
git@github.com:Pegessi/claude_hub.git docs/unified-promotion-plan` == local
HEAD. (This SHA line was added by amending the same docs commit; the final
pushed SHA is recorded in the ready_for_review report.)

---

*Generated read-only. No app code, no perf/design branch, and no merge was
performed. Sole artifact: this file on `docs/unified-promotion-plan` (cut from
`main` @ `71b7dbc`).*
