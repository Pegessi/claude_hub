# Trusted-proxy header handling, insecure-listen guard and WebSocket Origin checks

## Problem

Three related network-trust gaps in the backend (`main` at 0211661):

1. `backend/claude_hub/auth/dependencies.py:get_client_ip` trusted
   `X-Forwarded-For` from *any* direct peer (`header.split(",")[0]`). When Feishu
   auth is enabled, the local-network exemption
   (`Settings.is_local_network_ip`, which accepts loopback, link-local and RFC
   1918/4193 ranges) runs on that value, so any remote client could send
   `X-Forwarded-For: 127.0.0.1` and bypass authentication entirely.
   `get_client_ip_ws` had the same flaw for terminal WebSockets. Independently,
   the sibling `codex/rv-auth-hardening` branch makes `_use_secure_cookie` honor
   `X-Forwarded-Proto` unconditionally, allowing cookie `Secure`
   downgrade/upgrade spoofing.
2. `start.sh` defaults the production launcher to `--host 0.0.0.0`; with Feishu
   credentials absent (the documented local-dev default) anyone who can reach
   the port gets unauthenticated terminal/agent control, and nothing stopped an
   operator from accidentally exposing that state on a LAN/public interface.
3. Both terminal WebSocket routes (`/api/terminal/ws/{tab_id}`,
   `/api/terminal/proxy/{tab_id}/ws`) accepted handshakes with no `Origin`
   policy, so any web page the user visits could open cross-site WebSockets
   (CSWSH) driving the terminal with ambient credentials.

## Contract and settings

New module `backend/claude_hub/auth/proxy.py` is the single trust boundary for
forwarded headers and browser WS handshakes.

Settings added to `backend/claude_hub/config.py` (pydantic-settings,
env-validated):

| Setting | Env name | Default | Meaning |
| --- | --- | --- | --- |
| `trusted_proxies` | `CLAUDE_HUB_TRUSTED_PROXIES` | `127.0.0.0/8,::1` | comma-separated IP/CIDR allowlist of *direct* peers whose forwarded headers are honored. Empty string trusts nobody. Invalid entries fail Settings validation. |
| `allowed_origins` | `CLAUDE_HUB_ALLOWED_ORIGINS` | empty | extra allowed browser origins (full origin or bare `host[:port]`), comma-separated; shared by WS handshakes and unsafe HTTP writes (renamed from `allowed_ws_origins` in round 2). |
| `allow_insecure_listen` | `CLAUDE_HUB_ALLOW_INSECURE_LISTEN` | `false` | explicit opt-in for a non-local bind while auth is disabled. |
| `bind_host` | `CLAUDE_HUB_BIND_HOST` | empty | effective uvicorn bind host seen by the in-process guard; exported by `service_launcher.main` and `start.sh` dev mode. |

Forwarded-header behavior:

- Trust decision keys off the direct peer only (`request.client.host`).
  Untrusted peer: XFF/XFP ignored completely.
- Trusted peer XFF: walk the chain right-to-left stripping trusted-hop
  addresses; return the first untrusted address (the real client); if every hop
  is trusted, return the left-most entry. Malformed tokens are skipped (they do
  not stop the walk, so an injected garbage entry cannot force a forged
  address). IPv4-mapped IPv6 peers (`::ffff:127.0.0.1`) normalize to v4.
- `scheme_from_request` accepts the first `X-Forwarded-Proto` token only, and
  only `http`/`https`, from a trusted direct peer; otherwise
  `connection.url.scheme`. It is intentionally framework-neutral so the
  auth-hardening branch can later switch `_use_secure_cookie` to it.

Listen guard (`backend/claude_hub/services/listen_guard.py`):

- Pure function `evaluate_listen_host(host) -> ListenVerdict(allowed, reason,
  detail)` with reasons `loopback`, `link-local` (allowed) and `wildcard`,
  `non-local-ip`, `hostname`, `empty` (refused). No DNS lookups: non-loopback
  hostnames conservatively count as exposed.
- `enforce_listen_policy(host, *, auth_enabled, allow_insecure)` raises
  `InsecureListenError` with remediation text (configure Feishu OAuth +
  allowlist, bind loopback, or set the opt-in). Authenticated servers may bind
  anywhere; opt-in passes with a warning.
- Called from `service_launcher.main` (exit code 2 before any child/build) and
  from the FastAPI lifespan in `main.py` (uvicorn runs lifespan startup before
  binding sockets, so the port never serves) — defense in depth for direct
  `uvicorn` invocations.
- Link-local is treated like loopback, matching `is_local_network_ip`'s
  existing exemption; it is not routable beyond the local segment.

WebSocket Origin behavior (`reject_bad_websocket_origin`):

- No `Origin` header → allowed (ttyd/websocat/curl and other native clients do
  not send one; browsers always do).
- Present `Origin`: parsed `scheme://host[:port]`; its host[:port] must equal
  the request `Host` (default ports 80/443 normalized on both sides, hostnames
  case-folded, IPv6 literals supported) or match an `allowed_origins`
  entry. The literal sandboxed `null` origin and unparseable values are
  rejected.
- Mismatch → `await websocket.close(code=1008)` *before* `accept()`. Verified
  via Starlette `TestClient`: the handshake surfaces as
  `WebSocketDisconnect(code=1008)`; under real uvicorn a pre-accept close is an
  HTTP 403 denial. The check runs first in both route handlers, so the existing
  `accept(subprotocol="tty")` selection and auth-close codes (4001/4004) are
  unchanged for allowed handshakes.

## Compatibility

- Default deployments bind loopback behind loopback tunnels (ngrok/cloudflared/
  frpc/localhost nginx): defaults trust those direct peers, behavior is
  unchanged.
- Operators with a non-loopback reverse proxy must add its address to
  `CLAUDE_HUB_TRUSTED_PROXIES`; documented in `docs/DEPLOYMENT.md` (new
  "反向代理信任" and "未启用鉴权时的监听保护" sections, plus an Nginx note) and
  `.env.example`.
- Existing unauthenticated wildcard dev binds (`./start.sh --dev` on 0.0.0.0)
  now refuse until auth is configured or the opt-in is set. The opt-in is a
  deliberate one-line acknowledgment.
- `get_client_ip` / `get_client_ip_ws` keep their signatures; only the
  implementation moved behind the helper.

## Test evidence

- New `backend/tests/test_proxy_trust.py` (40 tests): untrusted-peer XFF
  spoofing ignored including private/loopback claims; trusted single/multi-hop
  right-to-left parsing; intermediate-proxy trust; prepended-loopback spoof;
  garbage tokens; all-trusted/all-garbage chains; IPv6 and v4-mapped peers;
  XFP first-token/garbage/untrusted handling; settings parse/validation;
  Origin missing/match/mismatch, port normalization, IPv6, allowlist forms,
  `null`/malformed; pre-accept close ordering on a hand-built WebSocket and via
  `TestClient` (denied, same-origin accepted with response intact, no-Origin
  native client accepted).
- New `backend/tests/test_listen_guard.py` (35 tests): exhaustive host
  classification (loopback names/IPs, link-local, wildcards, LAN/public IPs,
  hostnames, empty), policy enforcement with auth/opt-in combinations,
  remediation message contents, `bind_host`-vs-`host` precedence, and
  lifespan startup abort/allow through an in-process `TestClient` (no socket
  bound).
- Updated launcher wiring tests to set the opt-in where they only exercise
  plumbing, plus two new tests asserting launcher exit code 2 for an
  unauthenticated wildcard and success (and `CLAUDE_HUB_BIND_HOST` export) for
  loopback.
- `uv run pytest tests/test_auth.py tests/test_auth_session.py -q`: 22 passed.
  Full touched-file set (proxy/guard/auth/terminal/session-manager/service
  restart/review-port/route-structure/lifespan suites): all passed except the
  pre-existing, environment-dependent
  `test_maso_preflight.py::test_create_rechecks_unavailable_service_before_tmux_or_tab_state`
  — it fails identically on untouched branches in this environment because a
  real MASO-like service answers on 127.0.0.1:11004; it does not touch the
  changed code.
- `black --check`, `isort --check` and `mypy` on the backend all clean.

## Open questions / follow-ups

- `codex/rv-auth-hardening` integration should replace its direct
  `x-forwarded-proto` read in `_use_secure_cookie` with
  `scheme_from_request(request)`; not done here per branch-boundary.
- The XFF allowlist is per-deployment, not per-interface: dual-homed hosts
  behind multiple proxies must list all of them. Forwarded-For parsing ignores
  zone ids (`%eth0`) and any host:port form clients illegally send; such tokens
  are treated as malformed and skipped, which is safe but may need revisiting if
  a proxy emits them.
- The WS Origin allowlist does not support wildcard suffixes; cross-origin
  embedders must be enumerated explicitly (deliberate).
- Hostname binds are refused for unauthenticated servers without resolving DNS;
  an operator relying on a name that resolves to loopback should bind the IP or
  `localhost` instead.
- The lifespan guard can only see the bind host via `CLAUDE_HUB_BIND_HOST`; the
  launcher, both `start.sh` modes and the README manual command set it, but a
  hand-typed `uvicorn --host 0.0.0.0` without the variable falls back to
  `settings.host` (default `127.0.0.1`) and is not policed in-process. The
  supervised launcher remains the production entry point and enforces the
  policy before spawning uvicorn.
- A network-level restart/upgrade of the existing primary Hub was intentionally
  not performed; existing processes, tmux sessions and ports were untouched.

## Follow-up round 2 (same-day finalization review)

Independent review of 7626d7b found four contract gaps; all fixed in the
follow-up commit:

1. **Dev mode was broken twice.** `start.sh --dev` still defaulted the backend
   to `--host 0.0.0.0`, which the new guard refuses without auth/opt-in, and
   Vite's proxy (`changeOrigin: true`) rewrote `Host` to `:8173` while
   forwarding the browser's `Origin: http://localhost:5173`, so every terminal
   WebSocket would have closed with 1008. Fixes: the dev backend and
   `scripts/start-temp-tunnel.sh` now default to loopback (Vite stays on
   `0.0.0.0`, so LAN/phone dev access is unchanged); `vite.config.ts` rewrites
   `Origin` to the proxy target's own origin on all three proxy entries (HTTP +
   WS), which is legitimate because Vite is a trusted loopback intermediary.
   The production `start.sh` line deliberately keeps `${CLAUDE_HUB_HOST:-
   0.0.0.0}`: `scripts/check_checkout_safety.py` structurally pins that
   wildcard default, so production security there comes from the launcher
   refusal plus explicit opt-in rather than a changed default.
2. **Unsafe HTTP writes had no Origin policy.** CORS blocks reading
   cross-origin responses but "simple" cross-site POSTs (form/text bodies) are
   still delivered, so a malicious page could drive an unauthenticated local
   Hub. Added pure-ASGI `BrowserOriginGuardMiddleware` (outermost layer):
   `POST`/`PUT`/`PATCH`/`DELETE` with a browser `Origin` must be same-origin
   or match `CLAUDE_HUB_ALLOWED_ORIGINS`; safe methods, preflight and
   Origin-less native clients/server webhooks pass. Setting/env renamed from
   `allowed_ws_origins` / `CLAUDE_HUB_ALLOWED_WS_ORIGINS` to
   `allowed_origins` / `CLAUDE_HUB_ALLOWED_ORIGINS` (one list shared by WS and
   HTTP); the pure helper is now `browser_origin_is_allowed`.
3. **Docker bypassed the in-process guard.** The image runs uvicorn with
   `--host 0.0.0.0` but never exported `CLAUDE_HUB_BIND_HOST`, so the lifespan
   guard fell back to the loopback `settings.host` default and did not police
   the real interface. The Dockerfile now sets `CLAUDE_HUB_BIND_HOST=0.0.0.0`
   (fail-closed for `docker run` without auth), and compose explicitly opts in
   with `CLAUDE_HUB_ALLOW_INSECURE_LISTEN=1` plus the cross-origin allowlist
   entry for its separate `:5173` frontend container.
4. **Docs/compose drift.** Compose healthcheck called a nonexistent
   `/api/health` (real route is `/health`); backend README still documented an
   unguarded `--host 0.0.0.0`. Both fixed (README now documents the loopback
   dev invocation plus the opt-in for wildcard); `.env.example`, DEPLOYMENT.md
   and CHANGELOG updated for the shared origin list and HTTP-write coverage.

New tests: 6 unsafe-HTTP unit cases, one in-process ASGI middleware case
(403 before routing for cross-site POST; same-origin and Origin-less requests
reach the app) and one real-app TestClient case (cross-site POST 403,
same-origin reaches routing, cross-origin GET 200). Live uvicorn verification
on an isolated loopback port confirmed the same matrix over real sockets plus
a wildcard-bind lifespan abort (`InsecureListenError`, port never bound).
Suite totals: 48 passing in `test_proxy_trust.py`, 35 in
`test_listen_guard.py`; full backend run 5542 passed / 21 skipped (the one
environment-dependent MASO preflight test fails identically on untouched
branches because a real service answers on 127.0.0.1:11004); frontend
typecheck/lint/1136 unit tests/build all pass; `check-staged` passes.
