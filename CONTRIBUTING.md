# Contributing to Claude Hub

Thanks for contributing to Claude Hub! Please read this document before
opening any pull request — the development workflow here is intentionally
strict to keep `main` always shippable and reviewable.

## ⚠️  Rule #1 — Never develop directly on `main`

Every feature, bug fix, UI change, test, documentation update, and managed
workspace task **must** use an isolated worktree on a feature branch. No
exceptions. See [Mandatory Workflow](#mandatory-workflow) below.

## Mandatory Workflow

Follow this flow for every change — even small doc updates and one-line fixes.

### 1. Sync `main`

```bash
cd <your-main-worktree>
git fetch origin
git pull --ff-only origin main
```

### 2. Create an isolated worktree + feature branch

```bash
cd <your-main-worktree>
mkdir -p ~/claude_hub_worktree
git worktree add ~/claude_hub_worktree/<slug> -b <type>/<short-description> origin/main
```

Branch naming convention: use conventional-commit types as the prefix.

| Prefix         | When to use |
| -------------- | ----------- |
| `feat/`        | New functionality, new endpoints, new UI |
| `fix/`         | Bug fixes, regression repairs |
| `docs/`        | Documentation-only changes (README, ARCHITECTURE, working logs) |
| `style/`       | Pure formatting / whitespace / CSS tweaks (no behavior change) |
| `refactor/`    | Restructuring code without changing behavior |
| `test/`        | Adding or fixing tests only |
| `chore/`       | Build scripts, CI config, dependency bumps, repo hygiene |
| `ci/`          | CI/CD workflow changes |

### 3. Work only inside the task worktree

Never edit files in the `main` worktree directly.

If the change touches the frontend, run a dedicated dev server from that
worktree on its own port and stop the server before merging.

### 4. Commit with conventional commits

Use the same `type:` prefix in every commit message:

```
feat: add workspace batch task creation
fix: prevent stale reviewer verdict from being misrouted
docs: update ARCHITECTURE module reference table
chore: expand .gitignore for ad-hoc GPU probe artifacts
ci: add AGENTS.md <> CLAUDE.md sync check
```

### 5. Run validation

Use the shared local/CI entry point from the feature worktree:

```bash
./scripts/verify.sh all
```

Individual targets are available for iteration (`./scripts/verify.sh --help`).
For a single backend regression, keep the same isolation and evidence handling:

```bash
./scripts/verify.sh backend-tests -- tests/test_task_attachment_transaction.py -q
```

Focused runs record their selected arguments and do not count as a full pass.
The full backend type target checks both product code and tests. Formatting,
types, backend behavior, frontend lint, frontend behavior, and frontend build
report independently; passing one is not evidence that another passed.

Tool versions are declared in `.python-test-version`, `.node-test-version`, `.uv-version`,
and `frontend/package.json` (`packageManager`). The Python/Node pins are verification-only;
they are not auto-discovered runtime-version files. Install those tools explicitly
using the environment's approved download/cache locations, then install locked
dependencies from the feature checkout:

```bash
(cd backend && uv sync --frozen --extra dev --python "$(cat ../.python-test-version)")
(cd frontend && pnpm install --frozen-lockfile)
```

Do not change the lockfiles or use a different interpreter merely to make a
check pass. The verification script does not install dependencies, download a
browser, or restart a service. Backend tests get private HOME/XDG/Hub paths and
retain their evidence directory. Tests may start their own helper processes;
inspect failed or interrupted runs before removing their runtime directories.
Never point the default check at a running developer or production backend.

UI checks must state whether they inspected real rendering, mocked APIs, or live
external integration. A screenshot is evidence of the inspected state, not proof
of every visual or interaction requirement. Requirements determine the expected
behavior; existing implementation strings must not be the only test oracle.

For docs-only changes, run `./scripts/verify.sh docs` and verify that the changed
instructions match the current commands, paths, and module responsibilities.

### 6. Update `CHANGELOG.md`

For any non-trivial change that ships user-facing or dev-facing behavior, add
an entry to `CHANGELOG.md` at the top of the **Unreleased** section (or under
today's date if there is no Unreleased block) using:

```
### <type>: <one-line description>
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`.

### 7. Open a PR, review, merge to `main`, push

Wait for the required checks and independent review before merging. Record the
exact candidate SHA, check results and unverified criteria. Developing a branch
does not authorize merge, push, deployment, or production restart; follow the
user's delivery boundary. A staged program may deliver branches for later merge.

## Cleanup

A merged branch is not evidence that its worktree is disposable. First inspect
Git status and all untracked/needed ignored files, and verify that no process,
tmux session, dev server or browser test still uses it. Unknown ownership means
keep it. Preserve needed evidence and stop only task-owned resources.

Only after those checks, remove the exact disposable checkout with
`git worktree remove ~/claude_hub_worktree/<slug>`. Do not use recursive deletion
or remove a shared/persistent agent session as incidental cleanup.

## AGENTS.md and CLAUDE.md

These two files **must remain byte-identical**. The rule is enforced in CI —
the `repo-docs` job runs `diff AGENTS.md CLAUDE.md` and fails on any mismatch.
If you edit one, always copy the exact same content into the other in the same
commit.

## What belongs where

| Kind of change                     | Where to document it |
| ---------------------------------- | -------------------- |
| Deep architecture / data flow      | `ARCHITECTURE.md`    |
| Incidents, bug history, pitfall   | `WORKLOG.md`         |
| Design notes for new subsystems    | `docs/working-logs/YYYY-MM-DD-topic.md` |
| Development workflow / rules       | `CONTRIBUTING.md` (this file) + `CLAUDE.md` / `AGENTS.md` |
| Merge-level change history         | `CHANGELOG.md`       |
| Security reporting policy          | `SECURITY.md`        |

## Questions?

If any part of this workflow is unclear, check `CLAUDE.md` (the agent entry
guide) or look at recently-merged PRs for a pattern to follow.
