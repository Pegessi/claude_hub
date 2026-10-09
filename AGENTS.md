# Claude Hub - Agent Entry Guide

> `AGENTS.md` and `CLAUDE.md` must remain byte-identical in the same commit.
> This is the always-read map. Load details only for the current task.

Claude Hub is a persistent terminal and agent workspace service: Vue 3/TypeScript/
Vite/Pinia frontend, Python 3.11+/FastAPI backend, ttyd/tmux terminals; use pnpm and uv.

## Mandatory workflow

**Never develop directly on `main`. Every code, test, UI, documentation, review,
experiment and managed workspace task uses an isolated feature worktree.**

1. Start from clean `main`: fetch/sync first; preserve unrelated local state.
2. Every linked worktree must be an immediate child of `~/claude_hub_worktree/`.
   The only exception is the primary checkout at `~/claude_hub`.
   Create the root if needed, then:
   `git worktree add ~/claude_hub_worktree/<slug> -b codex/<feature> main`.
3. Work only in that checkout. If you accidentally edit main, stop and preserve
   your changes before transferring them to an isolated worktree.
4. Frontend changes require a dedicated worktree dev/review server on its own
   port. Stop task-owned servers before merging or leaving the task.
5. Run validation appropriate to touched files; independently review the candidate
   and fix actionable findings. Commit with conventional `feat:`, `fix:`, `docs:`,
   `style:`, `refactor:`, `test:`, `chore:` or `ci:` messages.
6. Update `CHANGELOG.md` for meaningful changes and add a focused working log for
   significant work. Deliver branch/SHA, checks/results and unverified risks.
7. Merge into `main` only after validation and review/approval, then push `main`.
   A request to merge/push requires this branch-to-main flow. A request for
   independent branches stops at branch delivery until integration is authorized.

Worktrees beside the primary checkout, under `/tmp`, or in another project are
forbidden. Before relocating/removing a worktree, inspect Git status and verify
no process, tmux session, dev server or browser test uses it; preserve dirty,
untracked and needed ignored state. Use `git worktree move`, never plain `mv`;
use `git worktree remove` only for a proven disposable checkout.

## Hard boundaries

- Do not stop/restart the primary Hub on 5173/8173. Feature backends must not write
  `~/.claude_hub/workspaces` or use the default tmux server. Use isolated runtime
  homes and `tmux -L`; set overrides before importing backend modules.
- Do not delete, reset or overwrite unrelated/untracked files. `.cursor/`,
  `tasks/`, `tmp_remote_media/`, captured `*.log`, `abl_*.json`, `nccl_*`,
  `pure_pytorch_*`, `run_*.sh`, `summarize_mem*.py` and `sweep_*.sh` are protected
  unless explicitly in scope. Age or a merged branch is not cleanup authorization.
- No recursive scans of `/`, `/Users`, all home, or all volumes. Use bounded
  `git ls-files`, `rg --files` and targeted reads. Stop on macOS privacy denial.
- CodeGraph is explicit opt-in only for the current request; do not load, inspect,
  initialize or sync it implicitly.
- Keep one writer per owned scope. Research/review are read-only; parallel writers
  need independent files and isolated worktrees/resources. The main service,
  shared databases, ports, tmux and external resources are not isolated by Git.

## Five-layer system map

| Layer | Responsibility and source of truth | Read first |
| --- | --- | --- |
| Intent | User outcome, Task, Goal Packet, dependencies and acceptance | [Task Graph](docs/TASK_GRAPH.md) |
| Context | Bounded derived snapshots, prompts and relevant lessons; current records/Git win | [Architecture](ARCHITECTURE.md#five-layer-responsibility-map) |
| Execution | Assigned session, owned worktree/resources, dispatch and evidence handoff | [Agent workflow](docs/AGENT_WORKFLOW.md) |
| Verification | Reproducible checks, independent reviewer/evaluator, human acceptance | [Review profiles](docs/working-logs/2026-05-26-review-profiles-v1.md) |
| Governance | Runtime/permission boundaries, ownership, lifecycle and durable feedback | [State policy](docs/working-logs/2026-05-23-state-machine-assessment.md) |

These are responsibilities of existing components, not five new stores. Context
is a cache: verify current task/report records and checkout/base/head before
acting or recovering. Do not treat a snapshot, old summary or worker claim as
acceptance evidence. Read the narrowest relevant source; expand only when needed.

## Task and agent entry points

**Task Graph / TaskMailbox**: [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md)
(primary: `claude-hub task`). Registration records work; it does not dispatch it.
Keep source separate from execution responsibility, and transfer responsibility explicitly.

Main and linked Git worktrees share the same Hub Workspace (canonical Git
common-dir). Agent execution cwd is separate. Run `workspace ensure --path …`,
then `agent status WORKSPACE_ID` before creating an agent. Best-effort reuse is
not a concurrency/idempotency boundary. From a feature worktree use:

```bash
claude-hub agent create WORKSPACE_ID --agent-type claude --cwd . --env-preset NAME_OR_ID
```

`--env-preset` accepts any built-in preset or saved custom preset by name or id.
Use `--no-reuse-existing` only for deliberate parallelism and `--ephemeral` only
for task-owned sessions. For a Workspace-managed task with an owned ephemeral
session, `claude-hub task cleanup TASK_ID` performs cleanup. Never delete
shared/persistent agents or apply managed cleanup to initiator-owned records.

Simple work runs directly. Complex work maps dependencies before delegation;
there is no minimum number of subagents. Every delegate needs inputs, scope,
owner, budget, stop condition and evidence handoff. Use actual available tools
and user/configured model choices. Workspace-managed work retains its configured
independent review and human-acceptance requirements.

## Focused navigation

- [Architecture and code ownership map](ARCHITECTURE.md)
- [Detailed workflow, commands, runtime pitfalls and task-specific document index](docs/AGENT_WORKFLOW.md)
- [Unified Task execution and Agent workflow](docs/working-logs/2026-10-07-agent-workflow-v2.md)
- [Task UI, Bot pool, pairing and callback safety](docs/working-logs/2026-10-07-bot-pool-and-task-ui.md)
- [Removal of the unpublished ChatWork layer](docs/working-logs/2026-10-08-remove-chatwork.md)
- [Recent behavior](CHANGELOG.md), [bug symptom history](WORKLOG.md)
- [Terminal debugging](docs/terminal-debugging.md), [deployment](docs/DEPLOYMENT.md)
- [Feedback lessons](docs/working-logs/lessons-catalog.md)

Run `./scripts/verify.sh all` from the task worktree before final handoff;
`--help` lists individual targets for iteration. The backend type target includes
tests. Local checks and CI share this entry point and the declared tool versions;
see [setup and validation](CONTRIBUTING.md#5-run-validation). Record each result
and any unrun checks rather than treating a partial pass as complete validation.
`pnpm lint` writes fixes and must not be used for read-only review.
