# Antigravity CLI: region-restriction bypass and network warm pools

> Deployment-specific operations guide. Claude Hub launches the official
> Antigravity CLI through the deployment-maintained `agyx` wrapper; the wrapper
> and its local proxy are not shipped with this repository. Installation and
> general proxy policy live in [Agent CLI installation](AGENT_INSTALLATION.md).

## Symptom

`agy` exits before any model request with:

```text
Antigravity is not currently available in your location.
```

This happens even when the account is signed in, the egress IP is in an allowed
region, and every connection goes through the deployment proxy.

## Root cause

The CLI calls
`POST https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`
before a turn. For this consumer account the response contains:

- `currentTier.id = standard-tier` — the account's own tier, allowed;
- a non-empty `ineligibleTiers` array whose only entry is `free-tier` with
  `reasonCode = UNSUPPORTED_LOCATION`.

The CLI treats **any** non-empty `ineligibleTiers` as fatal and never checks
whether an entry belongs to the account's current tier. The rejected tier is
unrelated to the subscription actually serving the request. This is neither a
proxy leak nor a missing entitlement; patching the binary or spoofing an
enterprise identity would be the wrong fix and is not used.

## What the local proxy changes

`agyx` points the CLI at a local CONNECT proxy on `127.0.0.1:18123`. TLS is
intercepted with a locally generated CA that the CLI trusts through
`SSL_CERT_FILE`; ALPN is pinned to HTTP/1.1. Every upstream connection is
chained through the deployment's Cursor egress proxy, so the egress route is
the same one Cursor already uses.

Only one response field is modified. For the cloudcode host and a request line
containing `loadCodeAssist`, ineligible tiers whose `tierId` differs from
`currentTier.id` are removed:

```python
current = (obj.get("currentTier") or {}).get("id")
before = obj.get("ineligibleTiers") or []
kept = [t for t in before if t.get("tierId") == current] if current else []
if len(kept) != len(before):
    obj["ineligibleTiers"] = kept
```

If `currentTier.id` is absent the proxy empties the list (there is no tier to
match). Everything else is byte-transparent: OAuth tokens and auth requests,
request bodies, model SSE/chunked streams, and all other hosts and endpoints
pass through untouched. No API key, Application Default Credentials, billing
setting, binary patch or fabricated identity is involved.

## Files and lifecycle

These paths are outside the repository and owned by the deployment:

| Path | Role |
| --- | --- |
| `~/.local/bin/agyx` | Starts the proxy when the port is down, exports proxy/TLS environment, then `exec agy "$@"` |
| `~/.local/share/antigravity-bypass/proxy.py` | CONNECT MITM proxy, `loadCodeAssist` rewrite and warm connection pools |
| `~/.local/share/antigravity-bypass/certs/ca.pem` | Local CA trusted by the CLI (`SSL_CERT_FILE`); `ca.key` is mode `600` |
| `~/.local/share/antigravity-bypass/log/proxy.log` | Connection, rewrite and prewarm log; tokens are not logged |

The wrapper exports `HTTP(S)_PROXY` and `SSL_CERT_FILE`, limits `NO_PROXY` to
loopback, and sets `AGY_CLI_DISABLE_AUTO_UPDATE=true`. It forwards all
arguments, stdin/stdout and the exit status unchanged because Hub reads the
JSON stream.

The proxy is launched with `setsid`, giving it its own session and process
group. Hub tears down a turn's process group; without `setsid` that teardown
would kill the proxy shared by every Chat and Terminal tab. There is exactly one
proxy per host, shared by all `agyx` callers.

Hub's provider spec uses binary `agyx` and deliberately skips its own
direct/proxy selector for Antigravity (`_build_provider_env`): the wrapper owns
the network path. `CLAUDE_HUB_PROVIDER_NETWORK_MODE` and other providers'
`*_PROXY_URL` values do not apply.

## Why the warm pool exists

A fresh outbound TCP connection through the deployment egress to
`daily-cloudcode-pa.googleapis.com` stalls for roughly 10–15 seconds (an egress
dial timeout followed by fallback); a reused keep-alive connection answers in
about 0.25 seconds. Each `agy` start opens several independent connections
(auth check, models, quota, experiments) and holds one open for streaming
generation. One-shot turns therefore paid the cold-dial cost many times; the
persistent transport now amortizes startup, but the first turn of a new process
still pays it.

A second, harder deadline sits on the same path: after loading a token,
`keyringAuth` validates it over the network with a fixed **10 second** timeout.
When `www.googleapis.com` (userinfo), `oauth2.googleapis.com` (refresh) or the
cloudcode host is cold, the CLI logs
`timed out after 10s, skipping keyring auth`, falls back to interactive login,
and fails in headless mode even though the token file is valid. Warming these
hosts is what makes automatic token refresh reliable.

The proxy therefore keeps a bank of pre-connected TLS tunnels per host:

| Host | Idle target |
| --- | --- |
| `daily-cloudcode-pa.googleapis.com` | 12 |
| `oauth2.googleapis.com` | 6 |
| `www.googleapis.com` | 6 |
| `play.googleapis.com` | 6 |
| `aicode.googleapis.com` | 6 |

A maintainer thread per host continuously dials toward its target, counting
in-flight dials so a slow dial does not trigger a dial storm. The listener is
exposed only after the pool has been primed (`_PRIME_TIMEOUT_SECONDS = 240`);
the wrapper waits up to 250 seconds for it. Short, cleanly framed keep-alive
responses return their tunnel to the pool; streaming generation and
connection-close responses spend the socket. If a pooled tunnel fails while
sending a request or reading response headers, the fully buffered request is
replayed once on a fresh connection.

Operational consequence: after a reboot, a proxy restart or a network change,
the first `agyx` call may take a few minutes while the pool primes. That delay
is one-time host setup, not per-turn latency.

## Operating it

Check the wrapper and proxy:

```bash
command -v agyx && agyx --version
(exec 3<>/dev/tcp/127.0.0.1/18123) 2>/dev/null && echo listening
tail -n 50 ~/.local/share/antigravity-bypass/log/proxy.log
```

A successful rewrite leaves a line such as:

```text
# PATCHED loadCodeAssist dropped 1 tier(s); current=standard-tier
```

Run a real turn:

```bash
agyx -p 'reply with OK' --output-format json
agyx models
```

Expected: `status: SUCCESS`, a model response, and, when the CLI performs its
eligibility check, a `PATCHED` line in the log.

Restart the proxy safely:

```bash
ss -ltnp 'sport = :18123'    # find the listening PID
kill <PID>
agyx --version               # wrapper relaunches and waits for pool prime
```

Do not use broad `pkill` patterns: they can match the invoking shell or Hub
processes. Avoid restarting while turns are running; it drops their tunnels and
resets the pool, and the next auth check may exceed the 10 second budget until
priming finishes.

The upstream proxy URL comes from `ANTIGRAVITY_UPSTREAM_PROXY`, then
`CURSOR_PROXY_URL` in the deployment's `backend/.env`; `ANTIGRAVITY_BYPASS_PORT`
overrides the listen port. The proxy implementation imports `certifi`, so the
Python interpreter used by `agyx` must have it installed (the Hub backend venv
does).

## Verification checklist

1. `agyx --version` works; the proxy listens on `127.0.0.1:18123`.
2. A direct `agyx -p …` turn returns a real answer without the location error.
3. An expired access token refreshes automatically within the auth deadline.
4. A new Hub Antigravity Chat completes its first turn and the next turn resumes
   the same conversation (persistent stream-json, same process).
5. A non-Solo turn that needs a tool reports the actionable "enable Solo Mode"
   error instead of an empty successful turn.

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| `not currently available in your location` | CLI not using the proxy, or rewrite disabled (`--no-rewrite`) | `HTTPS_PROXY`, `SSL_CERT_FILE`, wrapper contents, `PATCHED` log lines |
| `timed out after 10s, skipping keyring auth` / `authentication failed or timed out` | Pool cold or proxy down; egress slow | Port 18123, `# prewarm failed` lines, wait for prime; do not restart during a turn |
| Turns take 10–20 seconds each | Pool below target, or a cold streaming dial | Log, prewarm failures, upstream egress health |
| `agyx: bypass proxy failed to start` | Pool prime exceeded 240 s, or the proxy exited | Proxy log; egress and upstream proxy health |
| `no upstream proxy configured` at startup | Neither `ANTIGRAVITY_UPSTREAM_PROXY` nor `CURSOR_PROXY_URL` is set | Deployment `backend/.env` |
| TLS errors from `agy` | CA not trusted | Wrapper exports `SSL_CERT_FILE` to the CA PEM |
| Stream truncation or `EOF` mid-turn | Proxy or upstream connection died | Proxy log, egress health, retry after restart |

## Boundary

This is a local-host, personal-account compatibility layer for the official CLI
and the account's own paid tier. It is not a multi-tenant provider, does not
serve third parties, and does not use API-key or ADC billing. It modifies only
an unrelated tier entry in one response; it does not patch the binary, fake an
enterprise account or forge authorization. It depends on the deployment's
Cursor egress proxy being alive and in an allowed region. Do not expose the
local proxy port or copy the CA private key, proxy URL or credentials to
another machine; re-check the network situation there instead.
