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

For test and CI changes, follow the [CI reliability contract](docs/CI_RELIABILITY.md):
choose the smallest meaningful check, keep IDs/output bounded, inspect cost and
failure evidence, and report the exact verified scope. Collection automatically
rejects oversized pytest IDs; slow-phase timings are diagnostic, not a time gate.

Use the shared local/CI entry point from the feature worktree:

```bash
./scripts/verify.sh all
```

Individual targets are available for iteration (`./scripts/verify.sh --help`).
For a single backend regression, keep the same isolation and evidence handling:

```bash
./scripts/verify.sh backend-tests -- tests/test_task_attachment_transaction.py -q
```

Focused runs record their selected arguments and do not count as a default-suite pass.
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

Standard local `verify.sh` targets warn and continue when uv, Python, Node or
pnpm versions differ from these pins; their results are useful feedback but are
not equivalent to verification on the declared toolchain. CI remains strict.
To require exact versions locally, set `CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1`.
The explicit `browser-mocks` target retains exact Python, Node and Playwright
requirements for its browser acceptance evidence. Missing tools, missing
installed dependencies and failing checks still fail in every mode.

Do not change the lockfiles or use a different interpreter merely to make a
check pass. The verification script does not install dependencies, download a
browser, or restart a service. Backend tests get private HOME/XDG/Hub paths.
Never point the default check at a running developer or production backend.

#### Runtime cleanup and retained evidence

Runtime-producing targets use one stdlib process owner per invocation. Logs,
provenance, exit codes, emitted JUnit/`.coverage` data and browser reports/screenshots
stay in the printed evidence directory under `CLAUDE_HUB_VERIFY_ARTIFACT_ROOT`
(default: the caller's TMPDIR, then `/tmp`). The default coverage XML remains at
`backend/coverage.xml`. Disposable HOME/XDG/tmp/Hub data uses
a separate short `/tmp/ch-v-*` directory. Once owned processes exit, that runtime
is removed on success, failure and handled interruption. `runtime.json` records
the runtime path, cleanup result and final status; `exit-code` is the final
verification status, while `pytest-exit-code` and `log-exit-code` separate test
failure from a failed log write.

To inspect raw temporary state after a run, opt in **before starting it**:

```bash
CLAUDE_HUB_VERIFY_KEEP_RUNTIME=1 ./scripts/verify.sh backend-tests -- tests/test_example.py -q
```

The default is `0`; only `0` and `1` are accepted. INT/TERM allow bounded
cooperative shutdown before terminating residual owned processes. Pytest targets
receive SIGINT so their finalizers can run; the outer exit remains 130/143 for the
original signal. Linux temporarily makes the fresh owner a child subreaper, so
surviving detached descendants remain identifiable even when they deny `/proc`
inspection. Such live children prevent successful shutdown, including in keep
mode. Other POSIX hosts check the owned group only. The Python 3.11+ owner
observes the leader with `waitid(WNOWAIT)` and reaps it after the last group
operation, preventing PID reuse from retargeting cleanup. Hosts without that API
are rejected. Dead adopted children are reaped while tests run, so nested
process-group shutdown does not wait on their zombies.

Both runtime and evidence directory descriptors are held from creation. Recursive
deletion and final report/status publication use those pinned descriptors.
Replaced paths cannot redirect them into another tree. HMR helper
logs live under the evidence directory's `terminal-hmr/` folder. Unsafe paths or
live users retain runtime with a warning; a changed evidence path makes success
nonzero and leaves reports in the pinned original directory.

This is not a security sandbox. POSIX cannot condition the final empty-root
`rmdir` on an inode: a concurrent replacement by another empty directory can still
remove that empty name, but its tree is never recursively traversed. SIGKILL,
machine loss and unsupported process inspection still limit guarantees. No
historical runs, retained evidence, caller-supplied paths, shared services,
virtual environments or dependency caches are swept.

UI checks must state whether they inspected real rendering, mocked APIs, or live
external integration. A screenshot is evidence of the inspected state, not proof
of every visual or interaction requirement. Requirements determine the expected
behavior; existing implementation strings must not be the only test oracle.
For UI changes, also run the explicit `browser-mocks` and `terminal-hmr` targets in
[UI acceptance](docs/UI_ACCEPTANCE.md). They require an existing browser and owned
runtimes; neither is included in `all` or proves live external integration.

#### Environment provenance and commit safety

Backend checks first probe the Python selected by the same `uv run` command used
for the checks. The probe runs with `-I`, private runtime directories and a neutral
cwd. It verifies the local editable project and the pytest/Black/isort/mypy module
and distribution locations. Tools run through `python -I -m`, avoiding copied
console-script shebangs. Version drift follows the policy above; it never relaxes
source-location checks. This is selected-module provenance, not a dependency-lock
or file-integrity audit. Recreate each worktree's environment with the locked setup
above; do not copy a virtual environment. Verification refuses a backend `.env`
rather than reading, moving or deleting it.

Stage the intended files explicitly, then run:

```bash
./scripts/verify.sh check-staged
```

This reads the index's blobs, not the working tree. Git environment variables that
redirect the repository, index or object database must be unset explicitly; the
check refuses them instead of inspecting a different snapshot. It rejects private
root/backend `.env` files and the machine-local `frontend/pnpm-workspace.yaml`,
and checks the production host argument of a tracked
`start.sh`, when present. The repository default remains
`"${CLAUDE_HUB_HOST:-0.0.0.0}"`; setting `CLAUDE_HUB_HOST=::` in the deployment
environment is permitted. Intentional changes to that startup contract need a
corresponding reviewed check change. This is not a general shell interpreter or
a requirement that the script always exist.

`./scripts/verify.sh checkout-safety` checks the fixed commit resolved from HEAD;
it runs in `all` and a separate CI job. A committed-tree pass does not validate
unstaged or staged edits. No hook is installed, Git configuration is unchanged,
and arbitrary `git commit`/`git commit -a` invocations are not automatically
intercepted. Do not treat this content check as secret scanning or cleanup
permission for unrelated files.

#### Reviewing a fixed merge

The merge-decision check compares Git's native reconstruction with one immutable
merge tree and includes paths with native conflicts. It can identify manual edits
outside conflict paths; it cannot detect semantic errors in an automatic merge
or prove that a documented transplant preserved every behavior.

```bash
# MERGE_SHA must be a full immutable commit ID. Choose a new RECORD_PATH.
backend/.venv/bin/python -I scripts/check_merge_decisions.py inspect \
  --merge "$MERGE_SHA" > "$RECORD_PATH"
./scripts/verify.sh merge-decisions --merge "$MERGE_SHA" --record "$RECORD_PATH"
```

Inspect generates object identities. Fill only `reason`, `disposition` and
`destinations` for each path. Dispositions are `resolve`, `retain-parent`,
`delete` and `transplant`; transplant destinations must exist in that merge tree.
An independent reviewer still checks the relevant parent changes and resulting
call paths. The filled record can live beside its working log, as in
`docs/working-logs/2026-10-12-stage3-merge-decisions.json`. Commit it afterwards:
it binds the merge SHA and cannot be embedded in the commit it identifies.

Without decision paths, `check` needs no record. Missing/stale coverage exits 1;
unsupported history/output or Git failure exits 2. Only two-parent merges with one
merge base are supported. A shallow clone is usable only when every shallow
boundary is a known ancestor of that base; otherwise obtain sufficient history or
report the check as unrun. Git must support the required `merge-tree` options.
Records bind its exact version; regenerate and review the template after an
upgrade. The tool uses an isolated temporary object database, not the source
index, worktree, refs, hooks or merge-driver configuration.

This target is explicit, not part of `all` or an automatic CI merge scan. A caller
must select the merge and supply its history. Later commits require their own
review; a passing old record does not validate a later repair. The example record
intentionally retains a known hover-style omission in its historical merge,
illustrating why coverage of written decisions is not semantic approval.

For docs-only changes, run `./scripts/verify.sh docs` and verify that the changed
instructions match the current commands, paths, and module responsibilities.

### 6. Update `CHANGELOG.md`

For any non-trivial change that ships user-facing or dev-facing behavior, add
an entry to `CHANGELOG.md` at the top of the **Released** section using:

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
