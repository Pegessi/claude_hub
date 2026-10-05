# Read-only worktree inventory

## System overview

The execution and governance layers need a repeatable inventory before cleanup.
This implements the bounded worktree inspection requested from the
[Agent OS source document, revision 8](https://bytedance.larkoffice.com/docx/GuXNdeQvEoyHRBx1aNUcpVMJngb).
It is a standalone Python 3.10+ standard-library script, with no new registry,
service, background task, or deletion mode.

```sh
python3 scripts/worktree_inventory.py --repo /Users/bytedance/claude_hub
python3 scripts/worktree_inventory.py --repo /Users/bytedance/claude_hub --json
```

The repository is mandatory; the tool does not search for it. Only paths returned
by `git worktree list --porcelain -z` are considered. The CLI fixes the eligible
root to `~/claude_hub_worktree`, with immediate children only. The primary
checkout, out-of-root paths, symlinks, locked, prunable, bare and unavailable paths
are retained without invoking worktree-local Git inspection. No recursive home
scan, directory relocation, Git prune/remove or branch deletion is implemented.

## Module design

- Capture the local integration ref (`main` by default; `--base` can select another
  local ref) without fetching or altering any branch.
- Sample visible process cwd with `lsof`, wide process argv with `ps`, and pane
  cwd from the default tmux server. Argv remains in memory; only matched PID and
  source pairs are returned. Raw subprocess stderr is never emitted. Unavailable,
  timed-out, incomplete or unparseable probes make eligibility unknown.
- Inspect eligible registered paths with optional Git locks and fsmonitor disabled.
  Porcelain status includes tracked changes, untracked directories and ignored
  content. Any of these retains the worktree, including otherwise disposable build
  caches. Git index mtime preservation has a regression test.
- Compare the recorded HEAD to the current checkout HEAD to detect drift during
  the scan, then use `merge-base --is-ancestor` against the pinned local base SHA.
- Produce `retain` or `candidate_for_manual_review`. A candidate requires clean
  status, merged HEAD, no observed occupancy, and no unknown probe. Classification
  never authorizes or performs removal.
- Each subprocess has a default 3-second timeout (maximum 30), within a shared
  60-second subprocess budget (maximum 600). Exhausted budget yields unknown for
  remaining checks. Over-8-MiB captured output is discarded and marked unknown.
  JSON and compact output contain timestamps, reasons, unknowns and timeout limits.

## Key issues and limits

- A process snapshot can become stale immediately. `ps`/`lsof` visibility depends
  on OS permissions, and only the default tmux server is queried. Other tmux
  sessions may still be detected by their visible shell cwd. Relative argv paths,
  externally held files and future process starts are not a complete ownership
  oracle. Recheck manually before any separate cleanup action.
- A path substring in argv conservatively retains a directory, including possible
  prefix false positives. Reports do not contain full argv, credentials, stderr,
  file contents, or individual dirty-file paths.
- Git/local path reads are scoped to the explicit repository and registered
  canonical children. Time budgets bound subprocesses; they are not a hard
  real-time deadline for OS path metadata calls.
- An initial live run surfaced lsof's mandatory `fcwd` descriptor even with
  `-Fpn`. The parser now explicitly accepts it, and the regression fixture uses
  this observed format. Other unexpected fields remain unknown.

## Validation and local observation

```sh
/Users/bytedance/claude_hub/backend/.venv/bin/python -m unittest discover \
  -s scripts/tests -p test_worktree_inventory.py -v
```

The tests create synthetic temporary Git repositories unrelated to Claude Hub's
registered worktrees. They cover merged clean candidates, primary and outside-root
protection, dirty/untracked/ignored contents, unmerged commits, symlink/locked/
prunable/missing paths, three occupancy sources, unknown/error/timeout handling,
literal data parsing, head drift, output privacy and non-refreshing Git index reads.
The 15-test suite passed in 26.730 seconds. After adding the CLI smoke case and
rejecting partial Git results with stderr, all six affected probe/error tests
passed in 2.436 seconds (`python scripts/tests/test_worktree_inventory.py
ProbeTests InventoryTests.test_failed_and_timed_out_git_checks_never_become_candidates -v`).
Black/isort passed for both new Python files, and mypy passed for the script.

A read-only snapshot at **2026-10-05 02:43:37 Asia/Shanghai**, against local main
`683e1f5d5cc299044aab1c6e196b5dd7c16aba80`, returned **44 registered worktrees, all
retained, zero unknown checks**. Reasons overlap: 42 had ignored content, 33 were
unmerged, 7 had observed occupants, 6 had untracked content, 3 had tracked changes,
and 2 were outside the canonical root (including the primary checkout).
This is a timestamped observation, not a persistent cleanup decision. No checkout,
branch, session, live service, or registration was removed or changed by the tool.
