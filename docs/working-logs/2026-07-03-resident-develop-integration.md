# Resident → develop → Main: Integration Workflow for Resident-Created Work

Date: 2026-07-03
Scope: process / branching — no product-code changes in this commit.
Task: workspace `d9426eae-...` "Resident develop integration worktree bootstrap".

## Why this exists

The resident agent (cb-agent-*, cb-reviewer-*, cb-codex-*) creates feature branches
and working logs as part of autonomous and reviewed task execution. Up to this
point every such branch was cut directly off `main` and proposed for direct
merge into `main` after review. That works, but it means any half-baked or
intermediate resident output sits one click away from `main`, and there is no
obvious place to batch up several resident contributions before they are
manually curated into a single main-merge.

A dedicated integration branch named **`develop`** (worktree `../claude_hub-develop`)
gives resident-created work a stable landing zone that is *not* `main`, while
preserving the existing rule that `main` requires explicit human approval,
CI-green validation, and a deliberate merge.

This log records the rules of the road. It is intentionally a process doc; the
workspace-manager / resident-prompt changes that teach the resident to
automatically target `develop` are a separate code task.

## Branch topology

```
main  ───────────●─────●──────────────  (protected; human-gated)
                  \     \
                   \     `- feat/resident-X  (resident-created, short-lived)
                    \
                     `- develop ─●─●─●─●─   (integration; resident PRs land here)
                                    \
                                     `- feat/resident-Y  (next resident task)
```

- `main` remains the single source of truth for deployed/running state.
- `develop` is a long-lived local branch that *always* starts from (and is
  periodically rebased/merged up to) `main`. It is where resident-created
  feature branches are merged first.
- Each resident task still creates its own isolated `feat/<slug>` worktree and
  branch off the **current tip of `develop`** (not `main`), does its work
  there, is reviewed there, and is proposed for merge into `develop`.
- A human batches up validated `develop` content into `main` on their own
  schedule, following the existing "validation + review + explicit approval"
  rule from CLAUDE.md.

## Rules

### 1. Where resident branches are cut

- Resident feature branches are cut from **`develop`**, not `main`, whenever
  `develop` exists.
- If `develop` does not exist (fresh clone, new machine), resident falls back
  to `main` and may create `develop` from `main` on first integration (this
  is exactly what this bootstrap task does).
- The feature branch naming convention (`feat/<slug>` / `fix/<slug>`) and
  worktree layout (`../claude_hub-<slug>`) do not change — only the base
  commit changes.

### 2. Where resident work lands first (integration target)

- Resident PRs / reviewed-and-passed task output is merged into **`develop`**
  first, not directly into `main`.
- `develop` acts as a rolling integration buffer: several resident features
  can stack on it before a human curates a main-merge.
- If a resident change turns out to be bad in integration, the human can
  drop, amend, or rework it on `develop` before it touches `main`.

### 3. Main is still protected

Nothing in this document changes the `main` protection rules from CLAUDE.md.
Concretely:

- `main` still requires CI-green (black / isort / mypy / pytest / frontend
  eslint + vue-tsc + build).
- `main` still requires an independent reviewer pass for non-trivial changes.
- `main` still requires **explicit human approval** to merge. `ready_for_review`
  and `review_passed` are not merge approval.
- `main` must never be edited directly; all changes come through a reviewed
  feature branch (cut from `develop`) merged via a merge commit.
- Conventional commits are still required.
- CHANGELOG.md must still be updated for meaningful shipped changes.

### 4. Forbidden git operations (data-loss safety)

On every branch — but especially on `main` and `develop` because they are
shared/long-lived — the following are forbidden unless a human explicitly asks
in a task and the request is unambiguous:

- `git reset --hard` on a shared branch (`main`, `develop`, any pushed branch).
- `git push --force` / `git push --force-with-lease` to `main` or `develop`.
- `git branch -D` on a branch that has not been merged.
- `git worktree remove` on a worktree that has uncommitted changes you did
  not just author in the current task.
- `git clean -fdx` or any bulk-delete of untracked files — untracked files
  are protected per CLAUDE.md.
- Rebasing a branch that has already been pushed to a shared remote.
- Deleting `.git/` contents, stashing without a named message, or any other
  operation that makes work unreachable without a reflog walk.

When in doubt, use `--dry-run` first, or ask.

### 5. develop lifetime / hygiene

- `develop` is long-lived; it is not deleted after each main-merge.
- After `develop` has been merged into `main`, `develop` should be fast-forwarded
  (or merged forward) to the new `main` tip before the next round of resident
  work branches off of it. Old feature branches that have landed in `develop`
  (and then in `main`) can be deleted along with their worktrees.
- `develop` may diverge from `main` only by the set of resident-created
  changes that have not yet been curated into `main`. It should not carry
  experimental state for long periods without intent.

### 6. AGENTS.md / CLAUDE.md parity

This commit does **not** edit `AGENTS.md` or `CLAUDE.md`. When the code-side
changes (resident prompt teaching it to cut from `develop`) land, the
"Mandatory Workflow" section of both files must be updated in the same commit
to point residents at `develop` as their base. Per the rule in those files,
`AGENTS.md` and `CLAUDE.md` must remain identical — never edit one without
the other.

## What was done in this bootstrap task

- Confirmed main is clean at `59fa368`, up-to-date with `origin/main`.
- Confirmed no `develop` branch (local or remote) and no `../claude_hub-develop`
  path existed before this task.
- Created `develop` branch from current main and worktree at
  `../claude_hub-develop`.
- Added this working log on `develop` (commits are on `develop`, not on
  `main`).
- Did not push anywhere, did not merge to main, did not touch product code,
  did not touch protected untracked files.
