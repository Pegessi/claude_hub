# App extensions and supervised restart

## Overview

The desktop header mixed vertically centered controls with `align-self:
flex-end` on Add/Status/Agents. These now use a common 30 px height and center
line. `AppExtensionsMenu` groups Scheduled tasks, existing network access,
and Restart service. `AppExtensionActions` also feeds both existing mobile
menus, without adding a second overflow trigger.

## Restart ownership and protocol

- `start.sh` production mode builds the SPA and execs `service_launcher`.
  The launcher owns exactly one backend child and survives its shutdown.
  It never searches by port or kills unrelated processes. Dev mode is unchanged.
- Authenticated `GET/POST /api/system/restart` expose availability and operation
  status. POST additionally requires a custom header (CORS preflight for browser
  cross-origin requests), current instance identity, and a UUID request ID.
- A runtime-local flock serializes requests and atomic file replacement keeps
  status readable across backend shutdown. A separate launcher ownership lock
  prevents two launchers from controlling the same runtime.
- Duplicate/concurrent requests reuse one operation. Completed request retries
  are idempotent; stale instance requests fail closed. The API never accepts a
  shell command, process ID, port, or filesystem path from the browser.
- Only a healthy response with the newly generated backend instance identity
  counts as recovery. Uvicorn connection draining is bounded at 3 seconds,
  total backend shutdown at 30 seconds, and readiness at 120 seconds. A failed
  restart is recorded, never automatically retried. See the
  [restart latency follow-up](2026-10-05-restart-latency.md).
- The dialog persists the pending request in session storage, polls only GET
  after a lost response, and times out after 3 minutes without claiming success.
  Reload/reopening can resume observation. Active work is explicitly interrupted;
  there is no automatic continuation or scheduled follow-up.

## Deployment boundary

The old live process cannot bootstrap its own launcher. Activate this feature
with a separately authorized external-terminal `./start.sh` start. Menu restart
does not pull code, rebuild frontend assets, or install dependencies. Frontend
updates continue through the normal deployment build. An unsupervised service
reports restart unavailable.

## Validation

- Focused API/process tests cover availability, confirmation header, stale
  instances, concurrent requests, completed retries, actual child replacement,
  mismatched health identity, startup failure, interrupted launcher recovery,
  and IPv4/IPv6 probes. Runtime isolation and ownership-lock regressions included.
- Frontend policy tests require both matching request ID and changed backend
  identity for success; UUID generation works on LAN HTTP without randomUUID.
- Final checks: 20 focused backend tests and 600 frontend unit tests passed;
  black/isort, scoped mypy, frontend lint/type check/build, shell syntax, and
  diff whitespace checks passed. Existing Pydantic deprecation and bundle-size
  warnings remain. This run does not claim a full backend-suite pass.
- Dedicated worktree Vite :15174 → isolated supervised backend :18174, separate
  runtime home and `tmux -L ch-extensions-review-1005`.
- Real Edge walkthrough: desktop menu; cancel without a POST; actual UI-triggered
  restart and success dialog; Scheduled tasks panel and network submenu;
  390×844 Terminal/Workspace overflow menus and restart confirmation.
- The built SPA served directly by the isolated backend also completed a real
  restart and displayed success; the isolated Terminal tab remained available
  for reconnection. No model/provider run was started for this UI validation.
- DOM measurement with an actual isolated Terminal tab: Add, Status, Extensions,
  and Theme all height 30 and center y=25.5. Agents shares Status's component.
- Live :8173 stayed on PID 56062 throughout; no production restart performed.

- Before merge, rebased on reliability integration `eb77fc8`. Repeated the
  frontend checks, focused backend tests, and real isolated restart against the
  integrated tree. Backend SPA-root tests run after the frontend build because
  Vite temporarily removes `dist` during a rebuild.

## Menu icon follow-up

Added 18 px clock, globe, and restart SVG icons with matching stroke weight,
muted color, and 10 px label spacing. Icons are decorative (`aria-hidden`) and
do not change accessible names. Removed the network summary's invisible 1 px
border so its icon and text align with the other actions. Verified desktop and
390 px mobile Terminal/Workspace menus in an isolated browser preview; measured
identical icon and label x positions for all three actions. Build/type check and
frontend lint passed. No live restart or new scheduled task was performed.
