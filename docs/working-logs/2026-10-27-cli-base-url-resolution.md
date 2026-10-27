# CLI base URL resolution on IPv4/IPv6 loopback

## Problem

The CLI hardcoded `http://127.0.0.1:8173` and ignored `CLAUDE_HUB_HOST` /
`CLAUDE_HUB_PORT`. On IPv6-only hosts (or a Hub bound to `::` / reachable only
via `[::1]`), every command failed with a connection error, and the connect
error itself hardcoded `http://[::1]:8173` regardless of the port actually
used.

## Behavior (d9b895b plus review follow-up)

Base URL resolution order:

1. `--base-url`
2. `CLAUDE_HUB_URL`
3. config file `[default].base_url`
4. URL derived from `CLAUDE_HUB_HOST` / `CLAUDE_HUB_PORT` (bare IPv6 literals
   are bracketed; `0.0.0.0` / `::` wildcard binds map to `127.0.0.1` / `::1`)
5. automatic `GET /health` probe of `http://127.0.0.1:<port>` then
   `http://[::1]:<port>`, first healthy one wins (0.5s timeout, `trust_env=False`,
   result never persisted)
6. built-in IPv4 default when both probes fail

Explicit sources (1-4) skip the probe. Follow-up hardening from independent
review:

- The probe is a Hub-discovery mechanism: a 2xx response is healthy only when
  the body is JSON with `status == "healthy"` (the shape of the Hub's
  `/health` route in `backend/claude_hub/main.py`). A 2xx with a non-JSON body
  is deliberately treated as unhealthy, so another service on the same port is
  never mistaken for the Hub. Non-2xx and transport errors remain unhealthy.
- Each explicit URL source (flag, env, config) is normalized with a
  strip-or-none rule: surrounding whitespace is stripped from a valid URL, and
  a whitespace-only or empty value is treated as unset, falling through to the
  next source instead of being sent verbatim.
- The connect error derives the host/port from the failed `httpx.URL`: for an
  IPv4 loopback host it suggests `http://[::1]:<port>` at the SAME port; for
  other hosts it gives the generic IPv6-loopback hint. Neither path hardcodes
  8173.

`fs` remains a hidden alias of the `filesystem` command group.

## Test evidence

`uv run pytest backend/tests/test_cli.py -q`: 89 passed.

- `test_probe_health_requires_hub_identity` uses `httpx.MockTransport` for
  healthy JSON (True), 2xx non-JSON (False), `{"status": "degraded"}` (False),
  HTTP 500 (False) and a connect error (False).
- `test_whitespace_only_explicit_urls_fall_through` covers whitespace-only and
  empty flag/env/config values falling through, plus padded valid URLs being
  stripped; existing empty-string behavior remains covered.
- `test_transport_error_names_url_and_suggests_ipv6_loopback_same_port`
  asserts the message contains the request port (9911), never 8173, and never
  the session token or a `cookie` mention; the non-loopback case asserts no
  bracketed hint and no hardcoded port.
- Pre-existing precedence, host-derivation and auto-probe tests continue to
  pass.

black/isort/mypy run on the changed files with `backend/pyproject.toml`
settings, matching `scripts/verify.sh`.
