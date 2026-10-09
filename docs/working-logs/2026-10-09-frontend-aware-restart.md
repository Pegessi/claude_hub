# Frontend-aware supervised restart

## Problem

The production UI labeled its action as a service restart, but the persistent
launcher only synchronized backend dependencies and replaced Uvicorn. A merge
that changed Vue sources therefore left the existing `frontend/dist` active,
even after a successful menu restart. Users could see an old interface backed
by new APIs.

## Change

- The production launcher owns a fixed `pnpm build` command rooted at
  `frontend/`; browser requests still contain no command, path, or build flag.
- Backend dependency synchronization and the frontend build run while the old
  backend remains healthy. Only successful preparation advances the operation
  to `restarting` and interrupts current work.
- Vite writes the candidate build into a launcher-created adjacent staging
  directory. The launcher requires `index.html`, then stops the old backend and
  promotes the whole tree with same-filesystem renames before starting the new
  backend. The old SPA/API pair therefore remains intact throughout preparation;
  old hashed assets and new API expectations cannot overlap. A failed or timed-
  out build leaves the old `frontend/dist`, backend child, and instance identity
  untouched.
- The build runs in its own process group. A timeout drains the whole pnpm/Node
  group before deleting staging files, so vue-tsc or Vite cannot survive as an
  orphan and continue writing after the operation fails.
- The menu and confirmation dialog now say **Build and restart** and describe
  preparation separately from the brief service interruption.
- The production build is bounded at 90 seconds. The UI observes the operation
  for 5 minutes, covering the dependency, build, shutdown, and readiness
  budgets without automatically issuing another request.

## Activation boundary

The launcher is the persistent parent process. An already-running launcher
cannot gain this behavior through one of its own backend-only restarts. After
merge, one separately authorized external production `./start.sh` restart is
required. That start also builds the current SPA; subsequent menu operations
perform the staged build automatically.

## Validation

- `tests/test_service_restart.py`, `tests/test_runtime_isolation.py`, and
  `tests/test_backend_instance_lock.py`: 27 passed. Focused launcher regressions
  cover successful staged promotion while the old backend is healthy, non-zero
  build failure, promotion rollback/backend recovery, timeout, whole-process-
  group cleanup (including a child that ignores `SIGTERM`), preservation of the
  previous `dist`, and fixed production command/path wiring.
- The full frontend unit suite passed (701 tests), along with `pnpm lint:check`,
  `pnpm exec vue-tsc --noEmit`, and `pnpm build`. The production build retained
  the repository's existing bundle-size advisory.
- A real `pnpm build` with `CLAUDE_HUB_FRONTEND_OUT_DIR` set to a temporary
  external directory produced `index.html` and the complete hashed asset set
  there; the probe directory was removed afterward.
- Black, isort, scoped mypy, shell syntax, `git diff --check`, and the
  `AGENTS.md`/`CLAUDE.md` byte-identity check passed. Independent candidate
  review found and fixed three lifecycle issues before this final run: build
  timeout orphaning child processes, staged assets becoming visible before the
  backend switch, and missing rollback/backend recovery on promotion failure.
- The shared service on `:8173` was not restarted during implementation.
