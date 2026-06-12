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
git pull --rebase origin main
```

### 2. Create an isolated worktree + feature branch

```bash
cd <your-main-worktree>
git worktree add ../claude_hub-<slug> -b <type>/<short-description> main
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

Run the checks relevant to the files you touched:

**Backend (any Python change):**
```bash
cd backend
uv run black --check .
uv run isort --check .
uv run mypy .
uv run pytest -xvs --ignore=tests/test_terminal_replay.py
```

If you changed terminal rendering / ttyd / tmux glue:
```bash
uv run pytest tests/test_terminal_replay.py -v
```

**Frontend (any Vue/TS/CSS change):**
```bash
cd frontend
pnpm run lint:check
pnpm run build
pnpm run test:unit
```

**Docs-only / .github changes:** at minimum, run the docs integrity check:
```bash
diff -q AGENTS.md CLAUDE.md
```

### 6. Update `CHANGELOG.md`

For any non-trivial change that ships user-facing or dev-facing behavior, add
an entry to `CHANGELOG.md` at the top of the **Unreleased** section (or under
today's date if there is no Unreleased block) using:

```
### <type>: <one-line description>
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`.

### 7. Open a PR, review, merge to `main`, push

Wait for CI to pass and for a reviewer to approve. After squash/merge,
remember to clean up the worktree (see [Cleanup](#cleanup) below).

## Cleanup

Once your branch is merged:

```bash
cd <your-main-worktree>
git worktree remove ../claude_hub-<slug>
git branch -d <type>/<short-description>
git pull --rebase origin main
```

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
