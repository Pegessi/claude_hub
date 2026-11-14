# Working Log: Work Standard Heterogeneous Pipeline Engine (2026-11-14)

## Context & Objective

Previously, `WorkspaceManager._select_or_create_reviewer` cloned the worker's `agent_type` and `env` when spawning a temporary reviewer for Chat tasks, causing same-model self-review. In addition, end-to-end engineering workflows (isolated feature worktree provisioning, streamlined implementation-to-review transitions, safe post-review remote branch push, read-only triage auto-acceptance, ephemeral session cleanup, and idempotent InfoWall demand progress writeback) required manual multi-step orchestration.

This change introduces standardized pipeline recipes (`WorkspaceTaskRecipe`), a heterogeneous cross-reviewer router (`services/cross_reviewer.py`), and an end-to-end work pipeline engine (`services/work_pipeline.py`) integrated with `WorkspaceManager` and `claude-hub dot delegate` / `claude-hub task create`.

## Key Changes

1. **Pipeline Recipe Schema & Demand Link (`models/schema_domains/runtime.py`, `tasks.py`, `_normalize.py`)**:
   - Added `WorkspaceTaskRecipe` (`standard`, `triage_diagnose`, `spec_implement_review`, `eval_and_report`).
   - Added `recipe`, `infowall_demand_id`, `auto_worktree`, `auto_push_remote`, `worktree_path`, `worktree_branch`, `remote_pushed_ref`, `remote_push_error`, and `infowall_synced_at` to `WorkspaceTask` (and creation/update schemas).
   - `SPEC_IMPLEMENT_REVIEW` defaults `auto_worktree=True` and `auto_push_remote=True`.
   - `TRIAGE_DIAGNOSE` defaults to `WorkspaceTaskMode.SUBAGENT` and `SessionKind.CHAT`.

2. **Heterogeneous Cross-Reviewer Router (`services/cross_reviewer.py`, `workspace_manager/_review.py`, `tmux/review_resources.py`)**:
   - Implemented `is_heterogeneous_reviewer` and `resolve_cross_reviewer_target` to pair MASO Argon (`gemini-4-argon_reasoning_high`) workers with MASO Astra (`gpt-6-astra_reasoning_high`) reviewers, and MASO Astra / Claude / Codex / Cursor workers with MASO Argon reviewers when a local MASO service is available, without leaking incompatible provider credentials.
   - Preferred idle heterogeneous reviewers in `_first_available_reviewer`.
   - Auto-released and deleted temporary non-caller-owned ephemeral reviewers upon `REVIEW_PASSED` (and terminal task acceptance) so temporary reviewer tabs never accumulate.

3. **End-to-End Work Pipeline Automation (`services/work_pipeline.py`, `dispatching/start.py`, `prompts/assignment.py`, `reports/internal.py`, `_task_updates.py`)**:
   - `provision_task_worktree`: automatically creates `<canonical_repo_parent>/<canonical_repo_name>_worktree/<slug>` on branch `feat/<slug>` and dispatches a task-scoped ephemeral orchestrator session in that worktree.
   - `SPEC_IMPLEMENT_REVIEW`: streamlines the Goal Packet gate so workers proceed straight through implementation, commit, verification, and forced heterogeneous cross-review.
   - `push_task_feature_branch`: automatically runs `git push -u origin <branch>` (never `--force`) on `REVIEW_PASSED` when `auto_push_remote=True`, with a hard safety guard refusing `main`/`master`/`develop`/`HEAD` or `workspace.default_branch`.
   - `TRIAGE_DIAGNOSE`: injects read-only diagnosis instructions and auto-accepts tasks to `DONE` upon completion report, syncing to InfoWall and deleting the ephemeral session.
   - `sync_task_progress_to_infowall` & `read_infowall_demand_summary`: invokes the `infowall` CLI passing `INFOWALL_API_KEY_FILE` only by path in the environment (never reading key contents in Python) and writes retry-safe progress entries keyed by `claude-hub-pipeline:<task.id>:<milestone>`.

4. **CLI & `dot delegate` Upgrade (`services/dot/runtime/work.py`, `api/dot.py`, `cli/commands/dot.py`, `cli/task_commands/creation.py`)**:
   - Added `maso` and `antigravity` support plus `--model`, `--env-preset`, `--recipe`, `--demand-id`, and `--ephemeral/--no-ephemeral` to `dot delegate`.
   - Added `--recipe`, `--demand-id`, `--auto-worktree/--no-auto-worktree`, and `--auto-push-remote/--no-auto-push-remote` to `task create`.
