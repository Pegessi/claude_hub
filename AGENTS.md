# Agent Instructions

Read `CLAUDE.md` before editing this repository. It is the canonical project
conventions document.

## Mandatory Branch Workflow

Do not develop directly on `main`.

For any code, UI, backend, test, or documentation change:
1. Start from a clean, up-to-date `main`.
2. Create an isolated worktree with a task branch, for example
   `git worktree add ../claude_hub-<slug> -b fix/short-name main`.
3. Make and validate changes inside that worktree. Do not edit the shared main
   checkout or reuse another task's worktree for new development.
4. For frontend changes, run a dedicated dev/review server from that worktree on
   its own port. After the user confirms the change or the validation window is
   over, stop that debug service before merging or leaving the task.
5. Commit with a conventional commit message.
6. Merge back to `main` only after validation and review/approval.
7. Push `main` only after the merge is complete and `main` is synced locally.

If a user asks to "merge and push", that means finish the branch-to-main flow;
it is not permission to skip the feature/fix branch.

## Protected Local State

Do not delete, reset, or overwrite untracked or unrelated files. Treat local
noise such as `.cursor/` and `tmp_remote_media/` as protected unless the user
explicitly asks to modify it.
