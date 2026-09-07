# Production Deployment Mode — FastAPI serves the built SPA

Date: 2026-09-07
Branch: `feat/prod-deploy-mode`
Status: pushed, awaiting human merge approval

## Problem

The main service was running in **dev mode**: `vite dev` (`:5173`, HMR) in front
of `uvicorn --reload` (`:8173`). Two symptoms:

1. **Background-tab force-reload** — when the Hub tab was inactive for a while
   and switched back to, the whole page reloaded. Root cause: the vite HMR
   WebSocket drops in background tabs; vite's client then calls
   `location.reload()`.
2. **Spurious backend restarts** — `uvicorn --reload` watches the source tree,
   so any code merge/change restarted the live backend.

## Design

Two modes, controlled by `start.sh`:

- **Production (default, `./start.sh`)**: `pnpm build`, then a **single uvicorn
  process** (no `--reload`) that serves both the API and the built SPA on
  `:8173`. No vite dev server, no HMR WebSocket, no CORS/proxy.
- **Dev (`./start.sh --dev`)**: the old behavior — vite dev (`:5173`, HMR) +
  `uvicorn --reload` (`:8173`).

### Backend static serving

`main.py` gains `register_frontend(app, *, serve, dist)`:

- When `serve` is true **and** `frontend/dist` exists, it mounts
  `StaticFiles(directory=dist, html=True)` at `/`.
- Otherwise it registers the old JSON `GET /` endpoint (dev/CI fallback).

`serve` comes from a new `serve_frontend` setting (default `False`); `start.sh`
exports `SERVE_FRONTEND=true` in production mode.

### Why a config flag instead of auto-detect-on-dist

The backend test suite uses the **real module-level app** (`conftest.py` client
fixture does `from claude_hub.main import app`), and `test_health.py` asserts
`GET /` returns JSON. Auto-mounting whenever `dist/` exists would break those
tests on any machine that had built the frontend. The explicit flag keeps CI
green (no build → JSON root) while `start.sh` enables it in production.

### Route ordering

FastAPI/Starlette matches in registration order: `/docs`, `/redoc`,
`/openapi.json` (from `FastAPI.__init__`), then API routers, then `/health`,
then the `/` mount **last**. Explicit routes win; the mount only catches
unmatched paths. The app has **no vue-router**, so `html=True` serves
`index.html` at `/` and 404s unknown paths — no SPA fallback routing needed.

### COOP/COEP

`CoopCoepMiddleware` runs on every response including static assets, so
`SharedArrayBuffer` (terminal input fast path) still works under the built
SPA. No change needed.

### Single worker only

`uvicorn` runs with **one worker**. The Hub is stateful — tmux sessions,
ttyd/WebSocket connections, in-memory state + `state.json`. Multiple workers
would not share that state and would break.

## Pitfalls / notes

- **`dist/` is gitignored** — the build output is produced at deploy time, not
  committed.
- **e2e false failure from a stale port**: the first manual static-serving
  check hit a *stale old backend* already bound to the test port (serving old
  code with the JSON root), so `GET /` returned JSON and assets 404'd. The code
  was correct; the test was invalid. Fix: confirm the port is free
  (`lsof -nP -iTCP:<port>`) and use the direct `.venv/bin/uvicorn` (single
  process) rather than `uv run uvicorn` (parent/child — killing the `uv`
    parent leaves the uvicorn child holding the port).
- **HMR check false positive**: grepping the built `index.html` for
  `vite|hmr` matches the favicon `href="/vite.svg"`. Grep for `@vite/client`
  (the real HMR client script tag) instead — confirmed absent in the build.
- **Pre-existing favicon 404**: `index.html` references `/vite.svg` but no
  `vite.svg` exists in the frontend source (no `public/` dir). 404s in both dev
  and prod. Out of scope for this branch.
- **`test_recovery_real_ttyd.py::test_real_cold_restart_7tab_bijection`** is a
  real-process timing test (7 tabs + ttyd under a 10s wall-clock oracle). It
  fails on this machine on **clean `main`** too (`start_all_tabs took 10.39s`),
  so it is an environmental timing flake, not a regression. It does not import
  `claude_hub.main`.

## Validation

- black / isort / mypy: clean on changed files.
- `pytest`: 1294 passed, 1 skipped, 1 pre-existing environmental flake (above).
- New `tests/test_frontend_serving.py`: 2/2 (built SPA served + JSON fallback).
- Frontend `pnpm lint`: clean.
- Manual e2e (isolated runtime home, port 8201): `/` → SPA HTML, hashed JS
  asset → 200, COOP/COEP headers present, `/health` `/docs` `/api/workspaces`
  reachable (not shadowed by the mount), unknown path → 404, no `@vite/client`.

## Deploy follow-up (human)

The live 5173/8173 service must be **restarted** to pick up production mode
(`./start.sh`). That is off-limits to the assistant — the human restarts the
live service.
