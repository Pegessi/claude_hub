# Agent Instructions

Read `CLAUDE.md` before editing this repository. It is the canonical project
conventions document.

## Mandatory Branch Workflow

Do not develop directly on `main`.

For any code, UI, backend, test, or documentation change:
1. Start from a clean, up-to-date `main`.
2. Create a task branch, for example `git checkout -b feat/short-name` or
   `git checkout -b fix/short-name`.
3. Make and validate changes on that branch. Use an isolated worktree when the
   active main checkout is serving the app or the task may conflict with other
   local work.
4. Commit with a conventional commit message.
5. Merge back to `main` only after validation and review/approval.
6. Push `main` only after the merge is complete and `main` is synced locally.

If a user asks to "merge and push", that means finish the branch-to-main flow;
it is not permission to skip the feature/fix branch.

## Protected Local State

Do not delete, reset, or overwrite untracked or unrelated files. Treat local
noise such as `.cursor/` and `tmp_remote_media/` as protected unless the user
explicitly asks to modify it.
