# Eight-branch fixed-merge integration

> Integration-only work on `codex/rv-eight-branch-integration`. It does **not**
> merge `main`, push, deploy, restart a shared service, or touch live
> `~/.claude_hub`. The controller owns independent review, main promotion,
> private deployment origin configuration, acceptance and cleanup.

## Baseline and sources

- Fixed main baseline: `15f31c0dde3fb516033bcc85e6c420536e9b030a`, equal to
  fetched `origin/main` at integration start.
- Common merge base for all eight branches with main:
  `0211661a1e627349822b3857ecf74f33b81818ef`. Main's fourteen newer commits are
  entirely the reviewed Feishu interactive-questions work, preserved here.
- Every source was merged exactly once as an immutable **two-parent** merge, in
  the fixed order, with the exact fixed SHA as parent 2:

| # | Branch | Source SHA (parent 2) | Merge commit |
| - | ------ | --------------------- | ------------ |
| 1 | `codex/rv-cli-baseurl` | `0f5a13e59e0dc1c0fc8e6cd71d5de8cc3a44d6a9` | `387c20c848f0c22c9c0f9c774a88d7d56d8c2b0d` |
| 2 | `codex/rv-ssh-validate` | `86221458b14d171fc3c437be001b67791c221211` | `ce14454d561cf09c948fa37b3d90c36136daff78` |
| 3 | `codex/rv-process-group` | `d850b99b9900cc1920e68ff5d2839392767248de` | `e90dadf33851019a09f5a1ff0f21111698fa449e` |
| 4 | `codex/rv-provider-spec` | `99ad899d4a1b99aacbf332d1c12b97b933ac2a1e` | `0eda8d780077f8a6d1c5156c21ea455a34e3e387` |
| 5 | `codex/rv-auth-hardening` | `e9ccf5f42efc2a0051f998420d972fefc5d69a76` | `920a5dada390c731464a4bb8899d396369d2bbdb` |
| 6 | `codex/rv-proxy-trust` | `6141f253957028d7f024e7540711e11bf2a986bb` | `35a5ff1dafd9fa9a37719f4f8e52561467ada0c8` |
| 7 | `codex/rv-state-safety` | `5c352d198827f488fa354107fb6263fd65eff7bc` | `dd024d5b0a01ab7152b5d60cdcf115f174005445` |
| 8 | `codex/rv-monitor-isolation` | `4f630ce91e085c9d59b52fdc7cdd247994fd0d4e` | `6c11956dc49eb65c2619a1b44d7a4f6e2618b78c` |

Excluded as instructed: SQLite/WAL, Hooks, concept consolidation, workflow
tooling, guidance and README branches. Source worktrees and their ignored files
were never modified; all edits stayed in `rv-eight-branch-integration`.

Each merge has a filled `check_merge_decisions.py` record (committed in a
separate commit after the merge it identifies) under
[`merge-decisions/`](merge-decisions): `2026-10-28-merge-{1..8}-*.json`. All
eight pass `./scripts/verify.sh merge-decisions`.

## Conflicts and resolutions

The only branch-to-main textually overlapping paths were `CHANGELOG.md` (all
eight branches plus main's Feishu notes) and branch 1's `cli/commands/rest.py`.
All branch-to-branch overlaps concentrated in the auth/proxy, provider and
workspace-manager surfaces called out in the task.

- **CHANGELOG.md (every merge).** Resolved to a union that keeps every parent
  entry. The final order under the single `## Released` heading is: Feishu
  (main) → CLI → SSH → process-group → ProviderSpec → auth → proxy-trust →
  state-safety → monitor-isolation, then the shared tail. A blank line
  separates adjacent heading blocks. Verified there is exactly one
  `## Released` heading and no `## Unreleased`.
- **Merge 1 — `cli/commands/rest.py` auto-merge.** Branch 1 reworded the `fs`
  alias comment while main appended the `tab question` group after it; both
  survive, and `TextIO` is imported for main's new command.
- **Merge 6 — auth-hardening × proxy-trust superset (the high-risk join).**
  Both branches added `auth/proxy.py` and edited `config.py`, `main.py` and
  `auth/dependencies.py`:
  - `auth/proxy.py` (add/add): retained the **proxy-trust** module. Its six
    shared primitives (`_normalize_ip`, `_ip_matches_networks`,
    `_resolve_networks`, `direct_peer_is_trusted`, `client_ip_from_request`,
    `scheme_from_request`) are byte-identical to the auth copy, and it
    additionally owns `browser_origin_is_allowed`,
    `unsafe_http_request_is_allowed` and `reject_bad_websocket_origin`
    (disposition `retain-parent` to parent 2 in the decision record).
  - `config.py`: union — auth's `public_url` (drives the Secure-cookie decision)
    plus proxy-trust's `allowed_origins`, `allow_insecure_listen`, `bind_host`
    and `allowed_origins_list`; shared `trusted_proxies`/`trusted_proxy_networks`
    kept. Dropped only auth's now-obsolete "intentionally not duplicated here"
    comment.
  - `main.py`: union — keeps both imports, the import-time
    `install_redacting_access_formatter()` (uvicorn access/WS session-id
    redaction), the lifespan insecure-listen guard, and
    `BrowserOriginGuardMiddleware` registered last so it is the **outermost**
    layer.
  - `auth/dependencies.py` auto-merged to the desired superset:
    `get_client_ip`/`get_client_ip_ws` delegate to trusted-peer
    `client_ip_from_request`, while `_mask_session_id` still masks the WS
    handshake log lines. `api/auth.py` keeps the Secure-cookie decision;
    `api/terminal.py` uses `reject_bad_websocket_origin` and retained the
    ProviderSpec migration.
- **Merge 3 × merge 4 — process-group × ProviderSpec.** The group-aware
  teardown core lives in `providers/process.py` (+`_spawn_subprocess`,
  `start_new_session`, `killpg` SIGTERM→SIGKILL escalation), which ProviderSpec
  never touches. The migrated `codex.py` still spawns through
  `_spawn_subprocess` (its only spawn site) with ProviderSpec-era
  `_build_command()`/`_build_provider_env()`; `session.py` retains
  `_process_group`. The only other spawn sites are the group-aware spawner
  itself and an unrelated bounded `network_probe.py` diagnostic; the immutable
  ProviderSpec `spec.py` registry spawns nothing.
- **Merge 7 × merge 8 — state persistence × monitor isolation.** Both add
  abstract methods to `workspace_manager/_base.py` in disjoint regions, and the
  single concrete `WorkspaceManager` implements both sets. State:
  `_backup_path_for`, `_atomic_save_state_text`, `_load_durable_json`,
  `state_recovery_warnings`. Monitor: `_monitor_refresh_sessions`,
  `_collect_session_agent_statuses`, `_refresh_one_session_status`,
  `_mark_runtime_task_failures` and the scheduled-chat steps. `main.py`
  `/health` stays `status == "healthy"` (additive `warnings`), so the merge-1
  CLI identity probe still accepts it.

## Composition regressions added

- `backend/tests/test_auth_proxy_composition.py` (5 tests): pins the auth/proxy
  superset — proxy-trust Origin/unsafe-HTTP policy + trusted-peer IP resolution
  together with auth log redaction, `public_url`, Secure-cookie settings and the
  outermost Origin middleware, including a real-`app` foreign-Origin 403 plus
  query-redaction check.
- `backend/tests/test_state_monitor_composition.py` (1 test): asserts the
  concrete `WorkspaceManager` implements both abstract families on one instance
  (a one-sided merge fails instantiation) and that clean-state recovery reporting
  coexists with the monitor loop.
- `backend/tests/test_proxy_origin_uvicorn_e2e.py` (6 tests, reviewer-requested):
  drives the **actual production** `BrowserOriginGuardMiddleware` and
  trusted-peer helpers through a real task-owned uvicorn server (lifespan off,
  no shared runtime) over real loopback TCP sockets on both IPv4 and IPv6 —
  same-origin/foreign-Origin writes, cross-origin reads, Origin-less CLI/webhook
  requests, trusted-loopback XFF/XFP, an allowlisted external `https` Origin
  with a rewritten internal loopback `Host` (the Merlin relay shape), and a real
  WebSocket handshake that accepts no-Origin/same-origin and returns 403 for a
  foreign Origin. These complement rather than replace the constructed-scope
  helper tests; the remote-peer (untrusted) XFP-forgery negative stays in the
  helper suites because it cannot be produced from a loopback socket.

No production source was changed beyond the conflict resolutions above; no
checks were weakened and no broad rewrite was performed.

## Compatibility contract

Production remains `--host ::1` `--port 8173` with Hub OAuth disabled; the raw
IPv6 TCP relay and the separate HTTPS relay to the same loopback backend are
unchanged, and no tracked private deployment origin was added (the controller
adds the exact HTTPS allowed origin). Existing tests plus the new real-uvicorn
suite encode the representative scenarios, all passing after integration:

- same-origin HTTP/WS accepted; IPv6 origin/peer (`[::1]`, IPv4-mapped v6)
  handled (`test_proxy_trust.py`, and real sockets in
  `test_proxy_origin_uvicorn_e2e.py`);
- HTTPS `Origin` with a rewritten internal loopback `Host` accepted **only**
  when listed in `CLAUDE_HUB_ALLOWED_ORIGINS`; foreign `Origin` rejected;
  malformed origins denied;
- Origin-less CLI/curl and server-to-server (Feishu) callbacks pass;
- `X-Forwarded-For`/`X-Forwarded-Proto` trusted solely from a configured direct
  peer (loopback default); forged XFP from a remote peer cannot set a Secure
  cookie (`test_auth.py` secure-cookie cases, `test_proxy_trust.py`);
- Secure-cookie consistency with `scheme_from_request` and `public_url`.

Deployment boundary (per controller): the private HTTPS allowed-origin update
is prepared with a protected backup and will be applied **only** alongside final
main promotion, because the older deployed Settings may reject unknown `.env`
keys. This integration does not edit live config; no relay, `start.sh`,
listen contract or tracked deployment origin was changed.

## Validation matrix

All backend runs used `./scripts/verify.sh` with its isolated private
HOME/XDG/Hub state and a task-owned `tmux -L` socket; ambient `FORCE_COLOR`,
`PY_COLORS` and `NO_COLOR` were cleared. Locked deps were installed only in this
worktree (`uv sync --frozen --extra dev` on Python 3.11.16 via the internal
index; `pnpm install --frozen-lockfile` on Node 24.21.0); the main checkout's
venv was never symlinked or reused.

| Step / gate | Command (focused args) | Result |
| --- | --- | --- |
| Merge 1 focused | `backend-tests -- tests/test_cli.py` | 91 passed |
| Merge 1 full candidate | `backend-tests` | 5553 passed, 9 skipped |
| Merge 2 focused | remote/profile/pty/ttyd tests | 288 passed |
| Merge 3 focused | `tests/test_provider_process_group.py` | 11 passed |
| Merge 4 overlap | process-group + ProviderSpec + structure + factory tests | 340 passed |
| Merge 4 full candidate | `backend-tests` | 5686 passed, 9 skipped |
| Merge 5 focused | auth/session/log-redaction tests | 63 passed |
| Merge 6 overlap | auth + proxy-trust + listen-guard + restart/port tests | 182 passed |
| Merge 6 new regression | `tests/test_auth_proxy_composition.py` | 5 passed |
| Merge 6 full candidate | `backend-tests` | 5817 passed, 9 skipped |
| Merge 7 focused | quarantine/storage/execution-control tests | 117 passed |
| Merge 8 overlap | monitor-isolation + cleanup-lifetime + state tests | 89 passed |
| Merge 8 new regression | `tests/test_state_monitor_composition.py` | 1 passed |
| Real-server compatibility | `tests/test_proxy_origin_uvicorn_e2e.py` (real uvicorn, IPv4+IPv6, WS) | 6 passed (stable across repeated runs) |
| Merge decisions ×8 | `verify.sh merge-decisions --merge … --record …` | complete coverage |
| Real-server compatibility (focused) | `tests/test_proxy_origin_uvicorn_e2e.py` | 6 passed (stable on repeat) |
| **Canonical final** | `./scripts/verify.sh all` on `7f9e39c` | docs, checkout-safety, backend-format, backend-types, backend-tests, frontend-lint, frontend-types, frontend-tests, frontend-build all PASS; **5857 passed, 9 skipped** |
| Browser gate | `./scripts/verify.sh browser-mocks` (Chromium 1243) on `7f9e39c` | 33 scenario PASS, 0 FAIL |
| Terminal gate | `./scripts/verify.sh terminal-replay` (ttyd/tmux/Chromium, isolated) on `7f9e39c` | 29 passed |

Notes:

- `verify.sh all` reported no tool-version mismatch (canonical Python/Node/pnpm
  pins); the one Vite chunk-size message is the pre-existing advisory.
- `browser-mocks` was driven with an explicit
  `CLAUDE_HUB_BROWSER_EXECUTABLE` (Playwright Chromium 1243 full build).
- `terminal-replay` uses pytest-playwright's pinned headless shell (build 1208).
  The verifier gives pytest an empty private `XDG_CACHE_HOME`, so the first run
  reported "Executable doesn't exist … headless_shell-1208" — an environment
  provisioning gap, not a product failure (browser setup ERRORs before any
  assertion). Re-running with read-only
  `PLAYWRIGHT_BROWSERS_PATH=/home/tiger/.cache/ms-playwright` (the prebuilt
  store; the script does not scrub that variable) yielded 29/29 pass. Evidence:
  `/tmp/claude-hub-verify.qb0f930r` (pass) versus the earlier failed run retained
  under its own `/tmp/claude-hub-verify.*` evidence dir.
- `terminal-hmr` was not separately required for this change set (none of the
  eight branches touch terminal HMR); the task's named terminal/origin gates are
  browser-mocks and terminal-replay, both green.

### Final candidate and exact evidence

The review-ready candidate is **`7f9e39cad4cdba4b654b38931e86fdd77cbc6280`**.
All eight two-parent merges are preserved unchanged; the only commits added
after them are the two composition regression tests, the real-uvicorn
compatibility test, their records and these docs (Conventional Commit
subjects). The first provisional `verify.sh all` was 5851 passed on `acb2c70`;
the +6 real-server tests bring the final run to 5857 passed on `7f9e39c`.

- `verify.sh all` backend default scope: `/tmp/claude-hub-verify.6p8jt4ej`
  (pytest/coverage/provenance; 5857 passed, 9 skipped).
- `browser-mocks`: `/tmp/claude-hub-browser-mocks.i4rcc27j` (33 scenario PASS,
  0 FAIL).
- `terminal-replay`: `/tmp/claude-hub-verify.wufmj1le` (29 passed, zero setup
  errors; isolated HOME/XDG/Hub and task-owned tmux socket, read-only
  `PLAYWRIGHT_BROWSERS_PATH=/home/tiger/.cache/ms-playwright`).

## Remaining real-domain checks (for the controller / human acceptance)

- Live external IPv6:8173 raw relay and the Merlin HTTPS relay against the real
  endpoints, with the exact HTTPS allowed origin supplied via **private,
  untracked** deployment config.
- Real Feishu server callback delivery to the deployed origin (network/app
  publication), and a real authenticated OAuth browser round-trip (OAuth is
  disabled in the current production configuration).
- Independent code + boundary review of the cumulative candidate, then the
  controller's main promotion/push, deployment and cleanup decisions.
