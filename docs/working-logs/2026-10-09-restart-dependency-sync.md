# Dependency-safe supervised restart

## Incident

At 10:47:52 the primary checkout fast-forwarded to code that added
`cryptography` and imported it from the Feishu Bot route during backend module
loading. A menu restart was requested at 10:48:11. The launcher stopped the old
backend, but its direct `python -m uvicorn` replacement used the existing virtual
environment without synchronizing the new lockfile and exited before readiness.
An external `./start.sh` invocation at 10:54 ran through `uv run`, installed
`cryptography`, and restored the service.

## Change

- The production launcher resolves `uv` once and configures a fixed
  `uv sync --locked --inexact` command rooted at `backend/`. No command, path,
  or flag comes from the browser restart request.
- Dependency synchronization runs while the current backend is still healthy.
  Only a successful sync changes the operation to `restarting` and starts the
  existing stop/start/identity-check sequence.
- A non-zero exit or the 20-second timeout records an actionable failed
  operation. The current backend child and instance identity remain unchanged,
  so the page can fetch and display the failure without a service outage.
- `--locked` rejects source/lock drift instead of silently rewriting `uv.lock`;
  `--inexact` preserves unrelated packages and tools already present in the
  environment. Frontend assets are still built only by the external deployment
  path.

## Activation boundary

The launcher is the persistent parent process, so the currently running launcher
cannot acquire this behavior through one of its own child restarts. After merge,
one separately authorized external production `./start.sh` restart is required.
Thereafter menu restarts synchronize newly committed backend dependencies before
interrupting the backend. This task does not restart the shared service.

## Validation

- TDD regressions use real health-serving child processes to prove successful
  synchronization runs while the old instance is reachable, then replaces it.
- Non-zero synchronization and timeout paths prove the old child remains alive,
  retains its instance identity, and reports healthy while operation state becomes
  `failed`.
- Production wiring is covered separately so the fixed command stays locked,
  inexact, and rooted at the backend directory.
- `tests/test_service_restart.py`, `tests/test_runtime_isolation.py`, and
  `tests/test_backend_instance_lock.py`: 23 passed. Existing Pydantic v2
  deprecation warnings remain unrelated.
- Black and isort checks passed for the changed Python files; scoped mypy passed
  for `service_launcher.py`; `git diff --check` passed.
- A temporary virtual environment and random-port real launcher probe completed
  locked/inexact synchronization, imported `cryptography 50.0.2`, replaced the
  old instance with a distinct healthy instance, and cleaned up its temporary
  process and files. The shared `:8173` service was not touched.
- Independent candidate review found one cross-layer budget issue: a 120-second
  sync plus existing shutdown/startup budgets could outlive the UI's 180-second
  observation window. The production sync timeout was reduced to 20 seconds and
  locked by a regression test. No other actionable finding remained.
