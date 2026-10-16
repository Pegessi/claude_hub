# Agent subprocess proxy environment inheritance

## Problem and scope

Cursor CLI could authenticate and list models while a real request timed out or
reconnected repeatedly. The host exported lowercase `no_proxy`; one Cursor proxy
library read only uppercase `NO_PROXY`. Shell startup files could mask the gap,
but native Chat and the global model catalog probe do not run those files.

The Hub normalized explicitly entered tab proxy variables but did not normalize
the backend's inherited environment. A long-lived tmux server could also retain
older proxy variables after the backend environment changed.

## Change

- Share the existing missing-case alias rule in `services/proxy_rules.py`.
  Explicit counterparts, conflicts and empty values are preserved. The helper
  copies its input and does not change process-global environment variables.
- Normalize inherited and session environments separately before the native
  Chat overlay. A single-case override, empty value or removal controls both
  inherited spellings before provider route selection runs.
- Give Cursor's global catalog probe a normalized copy of the backend environment,
  without borrowing session credentials or models.
- Normalize ttyd/tmux client spawn environments. Local shell launches also carry
  current proxy variables through the existing mode-0600 launch wrapper, so
  stale tmux environment cannot override the selected session environment and
  proxy credentials are not added to command arguments.
- Clear standard proxy variables missing from the current launch environment,
  including when the backend has no proxy variables at all. This prevents a
  stale tmux `NO_PROXY=*` or removed proxy from surviving the refresh.
- Keep inherited values out of persisted tab environment and Claude settings.
  Remote launches receive only their explicit tab environment, not local proxy
  addresses. Existing direct/proxy/auto/inherit route ownership remains intact.

## Cursor HTTP compatibility

`network.useHttp1ForAgent` is a separate Cursor client preference. During local
diagnosis, HTTP/1.1 plus consistent bypass variables completed both a real print
request and an interactive terminal turn. Either change alone was insufficient
on that host. Hub does not rewrite users' Cursor config, enable
`NODE_USE_ENV_PROXY`, change model selection or force every provider to HTTP/1.1.

Official reference: <https://cursor.com/docs/cli/reference/configuration>.

## Validation

Regression coverage includes inherited aliases, all native provider classes,
explicit override/empty/removal precedence, Cursor catalog spawn environment,
executing a local launch wrapper against a stale parent environment, credential
protection, remote isolation, Claude settings precedence and all network modes.

- Focused proxy, native provider, ttyd and TraeX regressions: 504 passed.
- `./scripts/verify.sh all` passed with strict tool versions (Python 3.11.16,
  uv 0.12.15, Node 24.21.0 and pnpm 9.15.9). Backend: 3562 passed, 7 skipped;
  frontend: 843 passed. Documentation, checkout safety, formatting, backend and
  frontend types, lint and production build also passed. Browser mocks, terminal
  HMR/replay and manual UI checks are separate gates and were not run for this
  environment-only change.
- Independent review found that an absent proxy variable could survive in an
  older tmux environment. After adding explicit clearing and real subprocess
  regressions, the reviewer approved the revised code with no remaining findings.
- A real Cursor print request, launched with the native Hub environment builder
  and no shell startup files, returned `HUB_PROXY_OK` with exit code 0. It took
  about 27 seconds and included two automatic retries; this confirms completion,
  not the elimination of all network instability.

The candidate is delivered on a feature branch. The primary Hub checkout and
running service are not changed by this worktree; merging and deployment remain
separate actions.
