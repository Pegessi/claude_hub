# Agent harness improvement program

## Objective and baseline

Improve accepted, recoverable outcomes through the existing Claude Hub task,
session, review, and feedback systems. Deliver independent feature branches
for user review; do not merge or deploy them from this program.

- Baseline: `683e1f5d5cc299044aab1c6e196b5dd7c16aba80`; fetched `origin/main`
  and verified equal to clean local `main` before creating worktrees.
- Design input: the user-provided coding-agent engineering document,
  revision 8, read on 2026-10-05. Its five responsibilities are intent,
  context, execution, verification, and governance. These classify existing
  responsibilities; they do not introduce five parallel state stores.
- All task worktrees are immediate children of `~/claude_hub_worktree`.
  Existing live services, default tmux state, credentials, and protected local
  files are outside implementation scope.

## Work graph and ownership

| Work item | Branch | Owned surfaces | Acceptance |
| --- | --- | --- | --- |
| Task dependencies and recovery snapshots | `codex/agent-os-tasks` | Task schema, task mutations/dispatch, CLI, persistence, Task Graph guide | Invalid edges rejected without side effects; blocked work cannot start; cold recovery and bounded derived snapshots preserve authoritative state |
| Context and delegation | `codex/agent-os-context` | Prompt builder, root instruction pair, workflow guide, isolated prompt measurement | Simple/complex/recovery instructions agree; delegation has explicit ownership and evidence; root instructions remain equal and navigable |
| Verification and feedback | `codex/agent-os-verification` | Reviewer placement, feedback evidence/digests, feedback schema | Review the worker's actual checkout; reject unsupported lesson provenance; preserve bounded failure evidence through digest and prompt |
| Directory hygiene | To be assigned | Bounded repository/worktree inspection and safe cleanup workflow | Preserve dirty/untracked/ignored/occupied/unknown state; never scan the home directory or infer safety from age |
| Coordination and integration | `codex/agent-os-coordination` | This delivery record and integration evidence | Review fixed SHAs, combine branches in an isolated checkout, run relevant regression checks, record limitations and merge order |

The first three writers start independently from the same baseline. Shared
schema edits are restricted to separate Task and Feedback classes. The
context writer is the only writer of `_prompts.py`. Changelog insertion
conflicts will be reconciled in integration, without combining feature
histories or moving `main`.

Implementation follows audit and agreed boundaries. Review follows committed
implementation with a reviewer who did not author that branch. Findings go
back to the owner; the coordinator validates fixes and cross-branch behavior.

## Verification and handoff protocol

- Bind each verdict to base and head SHAs, execution checkout, command,
  result, and any residual risk.
- Use the existing Python environment with an explicit checkout `PYTHONPATH`;
  confirm imports resolve to the candidate. Runtime tests use isolated Hub
  homes, unique tmux sockets and owned ephemeral ports.
- Exercise real API/CLI boundaries and persistence/dispatch failure paths,
  in addition to helper tests. Run related regression checks and formatting /
  type checks appropriate to changed files.
- Claims of completion and iteration counts are not proof of correctness.
  Keep deterministic checks, independent review, and live deployment
  acceptance distinct.
- No online review comments, automatic merge, service restart, or unrelated
  skill/configuration changes are part of this delivery.

## Directory audit

The initial bounded audit enumerated registered Git worktrees and inspected
only those exact paths. Process cwd and tmux pane cwd were checked separately.
Several historical test processes still refer to old worktree paths; such
paths must be preserved and are not evidence of disposable directories.
One confirmed disposable checkout, `codex-sandbox-loopback`, was removed
with `git worktree remove` after fresh checks passed. Its branch
`investigate/codex-sandbox-loopback` at
`71236cd0cd2d94315b655c7811a5af81a7f206db` was retained and read back.

An old worktree is removable only after fresh checks establish merged HEAD,
clean tracked and untracked state, no ignored local content, and no process
or session occupancy. Unknown checks mean retain.

## Results

Baseline verification used the main checkout's Python 3.11 environment with
`PYTHONPATH` pointing at the isolated coordination checkout. Task Graph,
orchestrator-contract and feedback tests passed (66 tests); the persisted
Task Graph API contract passed separately (1 test). An initial run hit a tmux
lookup/cleanup error; the recorded successful reruns used explicit
`PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` and owned test runtimes.

Baseline prompt measurement (same interpreter, isolated runtime): autonomous
complex assignment 9,162 characters / approximately 2,117 tokens; simple
assignment 8,955 / approximately 2,069; review with ten verbose reports 38,907 /
approximately 9,926. Measurements describe context size, not task quality or
end-to-end performance.

Implementation, independent review, validation evidence, and final branch SHAs
will be recorded here after the corresponding checks complete.
