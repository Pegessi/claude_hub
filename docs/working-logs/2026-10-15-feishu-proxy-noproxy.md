# Feishu HTTP clients crash on construction under a CIDR NO_PROXY list

## Scope and ownership

Worktree `~/claude_hub_worktree/feishu-proxy-fix`, branch
`fix/feishu-proxy-noproxy`, base `45c552c` (`main`). The change is left
uncommitted for review. No service was restarted and no request was sent to
Feishu; the real-environment check constructs clients only.

The starting point was the existing `stash@{0}`
(`codex-pre-claude-hub-recovery-20261015`, also based on `45c552c`) on the main
checkout. The stash was read, never consumed, and its `start.sh` hunk was
deliberately dropped (see the last section).

## Symptom

On a host whose `no_proxy` contains IPv6 CIDR entries, every Feishu HTTP call
failed before a request was attempted:

```
httpx.Client(trust_env=True)                                       -> InvalidURL: Invalid port: ':'
httpx.AsyncClient(base_url='https://open.feishu.cn', timeout=15.0) -> InvalidURL: Invalid port: ':'
httpx.AsyncClient()                                                -> InvalidURL: Invalid port: ':'
```

Bot credential validation, image download, WebSocket endpoint discovery and
OAuth sign-in all surfaced this as a generic failure, because the exception came
from the constructor rather than from the network.

## Root cause

HTTPX 0.28 builds its proxy mounts in `Client.__init__` by translating every
`NO_PROXY` entry into a URL pattern. An entry it does not recognize as an IPv4
address, IPv6 address or `localhost` becomes `all://*<entry>`. For `fe80::/10`
that string is parsed as a URL whose port is `:`, so construction raises
`httpx.InvalidURL`. Bisecting the host's real list confirmed the trigger: the
domain entries, `localhost`, `::1`, and IPv4 CIDR blocks such as `10.0.0.0/8`
all parse, while `fe80::/10` and `fd00::/8` do not.

The repository already knew about this class of problem -- `api/terminal.py`,
`cli/client.py`, `agent_stream/provider_network.py` and `agent_stream/maso_http.py`
all pass `trust_env=False`. Seven Feishu construction sites still relied on the
default `trust_env=True`: `services/feishu_bot.py` (2), `services/feishu_bot_websocket.py`
(1) and `auth/feishu.py` (4).

`trust_env=False` was not an option here. Feishu is a public endpoint that this
deployment can only reach through the configured proxy, so the client must honor
the environment -- it just must not let HTTPX parse `NO_PROXY` itself.

## Fix and ownership boundary

`backend/claude_hub/services/proxy_rules.py` now owns one question: given a URL
and the proxy settings visible in an environment, must the request be proxied
and through which proxy. Callers apply that answer themselves, which is why the
two consumers look different:

- `provider_network.py` composes subprocess environment variables for a native
  CLI and only needs the boolean `bypasses_proxy`.
- Feishu needs an in-process client, so `create_async_client` resolves the proxy
  first and hands HTTPX an explicit answer -- either `proxy=<url>` or an explicit
  `AsyncHTTPTransport`. Both suppress HTTPX's environment mounts
  (`allow_env_proxies = trust_env and transport is None`, and `_get_proxy_map`
  short-circuits when `proxy` is set), while `trust_env=True` is retained so
  `SSL_CERT_FILE` / `SSL_CERT_DIR` keep working.

### Why the shared module rather than the stash's own matcher

The stash reimplemented the decision with `urllib.request.proxy_bypass_environment`
plus a CIDR-only helper. That pair ignores `*`, per-entry ports and bracketed
IPv6 entries, so it is a second, weaker answer to a question `provider_network`
already answered correctly. Two matchers would drift and would disagree about
which traffic is proxied. The existing implementation was moved verbatim into
`proxy_rules.py` with its behavior unchanged, `provider_network` now imports it,
and the stash's two helpers were dropped.

Feishu does not import from the `agent_stream` subpackage. Reaching into another
owner's private helpers is what `docs/AGENT_WORKFLOW.md` asks us to replace with
an extracted responsibility, and `agent_stream` is about native provider
sessions, not about Hub-wide outbound routing.

`auth/feishu.py` uses `proxy_rules.create_async_client` directly rather than the
Bot's `create_feishu_http_client`, because OAuth sign-in has no relationship to
Bot pool configuration. Its timeout is pinned to HTTPX's own default of 5
seconds so the proxy fix does not change sign-in timing.

## Upstream diagnostics

A rejected credential validation previously produced one generic message.
`FeishuBotRemoteError` now carries the upstream `code`, the `msg` truncated to
500 characters and the `x-tt-logid` support id, and `api/feishu_bot.py` turns
access-control policy `21001` into a distinct detail so the operator is told to
involve the app administrator instead of retyping a correct secret. Neither the
error nor the response repeats Hub credentials or request bodies.

## Verification

- `./scripts/verify.sh backend-format`, `./scripts/verify.sh backend-types`.
- `./scripts/verify.sh backend-tests -- tests/test_feishu_bot_http.py
  tests/test_feishu_bot_pool_security.py tests/test_feishu_bot_websocket.py -q`
  (69 passed).
- `./scripts/verify.sh backend-tests` over `tests/test_provider_network.py`,
  `tests/test_proxy_rules.py` and the remaining Feishu/auth/agent-stream suites
  (630 passed), confirming the extraction did not change provider behavior.
- Frontend `eslint`, `vue-tsc` and `node --test tests/*.test.mjs` (782 passed).
- Discrimination check: reverting `create_async_client` to a plain
  `httpx.AsyncClient(trust_env=True)` fails 5 tests with the real
  `httpx.InvalidURL: Invalid port: ':'`; restoring a bare `httpx.AsyncClient()`
  at a Feishu call site fails the two tests that assert every Feishu module
  builds its client through the shared factory.
- Real environment: with the host's actual `no_proxy`, all three Feishu
  construction paths build and route through `sys-proxy-rd-relay...:8118`, while
  an internal base URL stays direct. No request was sent.

## Excluded: the `start.sh` listener change

The stash also changed the default backend listen address from `0.0.0.0` to
`::`. It is unrelated to proxy selection and it is not a safe drive-by: with the
listener bound to `::`, connections to `127.0.0.1` were refused on this host.
Whether the Hub should listen on IPv6 by default is a deployment decision that
needs its own change, its own reachability evidence and its own review, so that
hunk was left in the stash.
