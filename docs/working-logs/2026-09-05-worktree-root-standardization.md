# Worktree Root Standardization

## System overview

Claude Hub has one primary checkout at `~/claude_hub`. All linked task,
review, test, and documentation worktrees now use the canonical sibling root
`~/claude_hub_worktree/`, with one immediate child directory per task slug.

## Module design

- New worktrees are created with `git worktree add` directly under the
  canonical root.
- Existing registered worktrees are relocated with `git worktree move`, which
  updates Git's worktree metadata together with the filesystem path.
- Cleanup requires three independent checks: the branch is already contained
  by `main`, the worktree has no tracked or untracked changes, and no process
  has its command or current directory under that worktree.
- Dirty, unmerged, or process-owned worktrees are preserved. Active services
  and tmux sessions must be stopped by an explicitly authorized controller
  before their worktrees can be moved or removed.

## Migration snapshot

On 2026-09-05, ten clean, merged, inactive worktrees and one stale registered
entry were removed. Thirteen dirty or unmerged inactive worktrees were moved
under the canonical root. The merged `feat/model-switcher-and-cursor-fix`
test stack on ports 5175/8175 and its isolated runtime were explicitly stopped
and removed.

Six process-owned worktrees were deliberately left at their existing paths:

- `~/claude_hub-agent-tree`
- `~/claude_hub-chat-history-cache`
- `~/claude_hub-paseo-v2`
- `~/claude_hub-structured-chat-fixes`
- `~/claude_hub-structured-ui`
- `~/Projects/codex_workspace/claude_hub-terminal-hmr-recovery`

They require separate service/session shutdown authorization before migration.

## Key issues and pitfalls

- A clean, merged branch is not sufficient deletion evidence when tmux,
  browser, or server processes still use the worktree as their current
  directory.
- Plain `mv` leaves Git's linked-worktree metadata stale; use
  `git worktree move`.
- Untracked runtime or review artifacts are protected state unless their
  deletion is explicitly authorized.
- The primary checkout remains at `~/claude_hub`; it must never be moved into
  the linked-worktree root.
