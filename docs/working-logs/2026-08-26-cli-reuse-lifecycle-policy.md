# 2026-08-26 — CLI workspace/agent reuse lifecycle

## System Overview

CLI and backend now enforce a first-class lifecycle contract:

- One Claude Hub Workspace per Git repo identity (`git common-dir`), not per Git
  worktree checkout.
- Default agent reuse best-effort matches compatible idle orchestrators
  strictly (type, role, target, cwd/repo identity, remote profile/cwd). It is
  not a concurrency/idempotency boundary: overlapping creates may each create
  an Agent when parallel work is intended.
- Task-scoped `--ephemeral` agents are caller-owned and cleanable via
  `task cleanup` only when the task is terminal and the session is idle with
  no other non-terminal references.
- `--env-preset` on `agent create` resolves any persisted custom preset from
  `~/.claude_hub/env_presets.json` or built-in preset by name/id; `day1` is only
  an example, and explicit `--env` wins.

## Module Design

- `backend/claude_hub/services/workspace_identity.py` — path normalize, git
  common-dir, composite remote identity, deterministic workspace selection.
- `backend/claude_hub/services/env_preset_resolver.py` — preset lookup by id or
  name, merge with explicit env (never logs preset values).
- `POST /api/workspaces/ensure`, `POST /api/workspaces/tasks/{id}/cleanup`.
- CLI: `workspace ensure`, `task cleanup`, tri-state agent reuse, `--env-preset`.
- `ManagedSession.caller_owned_ephemeral` — legacy sessions default false.

## Key Pitfalls

- Git worktree for feature dev (AGENTS.md Rule #1) is unrelated to creating a
  new Hub Workspace; use `workspace ensure` from any worktree path. Hub
  Workspace is shared by repo, but agent cwd is separate — from a feature
  worktree run `agent create … --cwd .` (see CLI lifecycle recipe).
- Check `agent status` before creating another Agent. Default reuse reduces
  routine duplication, but it deliberately does not collapse overlapping
  requests; use `--no-reuse-existing` only for intentional parallel work.
- Do not delete reused/shared agents; cleanup is fail-closed for persistent
  sessions and active task references.
- Unknown env preset names fail closed; do not hardcode secrets or preset text.

## Files

- `backend/claude_hub/services/workspace_identity.py`
- `backend/claude_hub/services/env_preset_resolver.py`
- `backend/claude_hub/cli/commands/workspaces.py`, `tasks.py`, `common.py`
- `backend/tests/test_workspace_identity.py`, `test_env_preset_resolver.py`,
  `test_cli_reuse_lifecycle.py`
- `AGENTS.md`, `CLAUDE.md`, `CHANGELOG.md`
