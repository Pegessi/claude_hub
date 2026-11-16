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

2. **Heterogeneous-Preferred Independent Reviewer Router (`services/cross_reviewer.py`, `workspace_manager/_review.py`, `tmux/review_resources.py`)**:
   - Implemented `is_heterogeneous_reviewer` and `resolve_cross_reviewer_target` to prefer pairing MASO Argon (`gemini-4-argon_reasoning_high`) workers with MASO Astra (`gpt-6-astra_reasoning_high`) reviewers, and MASO Astra / Claude / Codex / Cursor workers with MASO Argon reviewers when a local MASO service and quota are available, without leaking incompatible provider credentials or parent tab identity env vars.
   - Reused existing explicit or idle independent `REVIEWER` sessions on the matching checkout (preferring idle heterogeneous reviewers first when both exist, while allowing same-model independent reviewers) and seamlessly fell back to spawning a clean-context same-model temporary `REVIEWER` session when cross-providers are unavailable or over quota.
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

## Round 2 Adversarial Review Fixes

1. **Fail-Closed `auto_worktree` Admission & Non-Blocking Git Lookups**:
   - Replaced synchronous `git_repo_identity` with async `_resolve_canonical_git_repo_root` and `_is_valid_git_worktree` using `_run_bounded_subprocess`.
   - `provision_task_worktree` now raises `RuntimeError` if a local `auto_worktree` task cannot provision or validate an isolated Git worktree, never falling back to `workspace.path`.
2. **Per-Task Start Serialization & Ephemeral Session Compensation**:
   - Added `_task_start_locks` to serialize `_start_task_workspace` per `task_id`, validating admission inputs before provisioning worktrees or sessions, re-reading live task state after every `await`, and retiring unattached ephemeral sessions on start/delegate failure.
3. **Independent Reviewer Routing & Implicit MASO Default Model Handling**:
   - Updated `maso_model_family`, `is_heterogeneous_reviewer`, `select_cross_reviewer_candidates`, and `resolve_cross_reviewer_target` to account for live MASO tab models and discovered `default_model` when `MASO_MODEL` is unset in `worker.env`.
   - `_select_or_create_reviewer` reuses valid explicit or idle independent `REVIEWER` sessions (preferring heterogeneous idle reviewers first), prefers `cross_target` when spawning a temporary Chat reviewer, and falls back to a clean-context same-model temporary `REVIEWER` session when cross-providers are unavailable or over quota.
   - Aligned Dot's `TOOL_GUIDE` and `format_task_brief` with `--recipe` so `spec_implement_review` and `triage_diagnose` delegations omit contradictory Goal Packet pause instructions while `standard` tasks preserve the existing workflow untouched.
4. **Canonical Locked Session Retirement & Multi-Round Remote Push**:
   - Removed manual `self.sessions.pop` bypasses; temporary reviewers and completed ephemeral workers are retired exclusively via `delete_session` (`allow_completed_review_for_task_id=task.id`).
   - `push_task_feature_branch` compares `task.remote_pushed_ref` against `origin/<branch>@<current_head_sha[:7]>`, pushing new commits on subsequent review rounds.
5. **`dot delegate --request-key` Replay Idempotency & Bounded Subprocess Reaping**:
   - Persisted `caller_fp` and `enriched_context` in `DotStore` and serialized concurrent `delegate` calls per `(workspace_id, actor_key, request_key)` so `request_key` replays reuse the original enriched context without conflicts.
   - Unified `git` and `infowall` subprocesses under `_run_bounded_subprocess` (`start_new_session=True`, cumulative 32 MiB chunked reader cap, 20s default timeout, loopback `NO_PROXY`, unconditional process-group `SIGKILL` on timeout/cancel/overflow even if the group leader exited, and transport cleanup).
6. **Concurrent `auto_worktree` Admission Invariant & Reviewer Incarnation Safety**:
   - `_start_task_workspace_locked` re-verifies `task.auto_worktree` and target session worktree placement before committing and retries preparation if a concurrent TODO edit enabled `auto_worktree`.
   - Reserved active task `session_id` / `review_session_id` values during friendly session ID allocation and guarded `_cleanup_reviewer_for_terminal_task` against cross-task or newer-incarnation reviewer sessions.
