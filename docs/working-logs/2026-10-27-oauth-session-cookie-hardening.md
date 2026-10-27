# OAuth session and cookie hardening — review follow-ups

Date: 2026-10-27
Branch: `codex/rv-auth-hardening` (base `95421bc`)
Scope: OAuth state, session-file permissions, cookie handling, auth log
redaction. Held unmerged for independent review.

## Context

Commit `95421bc` hardened the session store, OAuth state cookie, the `Secure`
cookie attribute and WebSocket session-id log masking. A security review
approved it with four conditions; this change closes all four:

1. Forwarded-proto decisions must depend on the trusted direct peer.
2. The state cookie must be deleted on failed callbacks, not only success.
3. The auth directory mode must be `0700`.
4. Access logs must redact `code` / `state` / `session_id` query values.

## Changes

### 1. Trusted-direct-peer forwarded proto — `auth/proxy.py`, `config.py`, `api/auth.py`

- Added `auth/proxy.py` carrying the stable trust primitives shared with
  `codex/rv-proxy-trust` (`_normalize_ip`, `_ip_matches_networks`,
  `direct_peer_is_trusted`, `client_ip_from_request`, `scheme_from_request`).
  The function bodies are byte-identical to that branch's module; only the
  browser-Origin policy (its scope, and still evolving there) is intentionally
  omitted so the integration merge is a trivial superset union.
- Added the matching stable config surface: `CLAUDE_HUB_TRUSTED_PROXIES`
  (default `127.0.0.0/8,::1`), parsed/cached into
  `settings.trusted_proxy_networks` with a startup validator.
- `_use_secure_cookie` now derives the scheme from `scheme_from_request`, so
  `X-Forwarded-Proto: https` is honored only when the direct TCP peer is
  trusted. A remote peer can no longer forge the header to influence cookie
  security. The configured-HTTPS-URL fallback and plain-HTTP localhost
  behavior are unchanged.

Note: the HTTP client-IP local-bypass still uses `dependencies.get_client_ip`
(now backed by the trusted-peer helper on the proxy-trust branch). Switching
that call site here is deferred to the integration merge to avoid forking that
file; the XFF-spoofing exemption is fixed by the sibling branch.

### 2. State cookie deleted on every failed callback — `api/auth.py`

The callback previously raised `HTTPException`, which carries no cookie
directives, and only cleared the state cookie on success. It now returns a
`JSONResponse` (same status codes and `{"detail": ...}` bodies) via
`_callback_error`, which also expires the state cookie, on all failure paths:
invalid/missing/mismatched state (400), token-exchange failure (400),
user-info failure (400), and whitelist denial (403). Success still clears it.

### 3. Auth directory `0700` — `auth/session.py`

`_ensure_sessions_dir` now creates the directory and then explicitly
`chmod`s it to `0700`. The old code relied on `mkdir(mode=0700)`, which the
process umask still filters, and never tightened a pre-existing looser
directory. The `sessions.json` file remains atomic/temp-file `0600` with the
existing load-time tightening.

### 4. Access-log redaction — `auth/log_security.py`, `main.py`

uvicorn's access logger renders the full request target including the raw
query string, so `GET /api/auth/callback?code=...&state=...` and terminal WS
handshakes `?session_id=...` wrote replayable credentials to `backend.log`.
`install_redacting_access_formatter()` runs at app import and:

- replaces the formatter on the already-created `uvicorn.access` handlers with
  a subclass that masks `code`/`state`/`session_id` query values by parameter
  name (preserving path, other params and format/colors); and
- swaps `uvicorn.logging.AccessFormatter` in place so a later `dictConfig`
  (custom log config / reload) also produces redacting formatters.

Idempotent; no-op if uvicorn is absent. Verified against uvicorn's real init
order (logging configured in `Config.__init__`, before the app is imported).

## Validation

- `verify.sh backend-format`, `backend-types` (mypy, incl. tests): clean.
- Focused: `test_auth.py` (30), `test_auth_session.py` (14),
  `test_auth_log_redaction.py` (14, new): 58 passed.
- Full backend suite: **5474 passed, 10 skipped, 0 failed**.
- Throwaway checks (not committed): trusted/untrusted XFP behavior, the
  `CLAUDE_HUB_TRUSTED_PROXIES` validator, and a real-uvicorn `Config` + import
  rendering a callback access line with both secrets replaced by `[REDACTED]`.

## Residual risks / non-goals

- The XFF client-IP local-bypass hardening lands with the proxy-trust branch;
  this branch only secures the forwarded *scheme* used for cookies.
- Redaction covers the uvicorn access logger and the existing masked
  WebSocket auth log lines. Other code paths do not log OAuth codes or bearer
  session ids; Feishu token/userinfo logging is pre-existing and out of scope.
- No merge, push, deployment or service restart; branch stays unmerged.
