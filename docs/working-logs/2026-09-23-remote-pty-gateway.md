# Remote PTY Gateway transport (`pty_gateway`)

Date: 2026-09-23 (PoC re-verified 2026-09-24)
Branch: `feat/remote-pty-gateway` (base `main` @ `4dc9141`)
Commits: `b98bd90` (feature), `76a0029` (PTY write pacing), `dad20d9` (bootstrap tty)

## Problem

`merlin_dev` (and the merlin/seedjob `ssh-candy` family) is a **PTY gateway**:
the jump sshd accepts the TCP session but

- `ssh host 'cmd'` (argv) → rc 0, empty output (command channel swallowed);
- `ssh -T` + piped non-tty stdin → not executed;
- `ssh -tt host 'cmd'` → stuck on the connect-time `<Trial …> init $` flash.

Only an **argv-less `ssh -tt`** (no command) lands, after a ~1 s flash, in a real
interactive GNU bash (`tiger@<host>:~$`) with tmux 3.4 and an `-R` loopback
channel. The Hub previously classified these as "stdin shell = no TTY" and
blanket-rejected interactive use, so merlin_dev could only browse directories
(via an argv/non-tty path that was in fact already broken on the gateway).

Goal: let a gateway host behave like an ordinary PTY host for remote Terminal
tabs and workspace agents/reviewers, without breaking normal hosts (mac_mini).

## Capability model

`models/schemas.py`: new `RemoteTransport` enum (`normal` | `pty_gateway`) and
two optional fields on `RemoteProfile`:

- `transport` — explicit override; otherwise inferred.
- `interactive` — only an explicit `false` means browse-only. Both normal and
  gateway default to interactive.

`services/remote_profiles.py` is now a capability resolver
(`profile_capabilities() → RemoteCapabilities(transport, interactive_supported)`).
Gateway signatures: the `merlin_dev*` aliases, `merlin-ssh-proxy*` hosts,
`workspace.byted.org`/`.seedjob.`, generic `.worker_*.seedjob.` users, plus
`CLAUDE_HUB_PTY_GATEWAY_HOSTS` (comma-separated host substrings) and per-profile
`transport`. The derived `stdin_shell` field is kept populated (`== is gateway`)
for back-compat. `reject_unsupported_interactive()` raises only for
`interactive is False`; the old `reject_stdin_shell_interactive` name is retained
as an alias. Gate call sites: `ttyd_manager.create_tab/update_tab` and
`workspace_manager/_sessions.py`.

## One-shot channel: `services/pty_exec.py`

Pure functions (unit-tested, no IO):

- `real_prompt_seen(bytes)` — matches a genuine `user@host:…[#$]` prompt on a
  recent line after stripping ANSI/OSC; the `init $` splash never matches.
- `build_guarded_line(cmd, token)` / `extract_guarded_result(buf, token)` —
  the command is base64-wrapped into **one** physical line:

  ```
  printf '\n__CHPBEGIN_<tok>__\n';
  echo <b64> | base64 -d | bash;
  __chp_rc=$?;
  printf '__CHPEND_<tok>__:%s\n' "$__chp_rc"
  ```

  The echoed input line (unexpanded `%s`) can't match the digit-only closing
  regex; only the executed `__CHPEND_<tok>__:<rc>` does. `extract…` returns the
  body between BEGIN/END and the exit code, ignoring the connect flash and echo.

IO seam: `SshPty` owns a raw local PTY + an argv-less `ssh -tt` child; reader
registered with `add_reader`. `pty_exec(profile, command)` opens the PTY,
`wait_for_real_prompt()` (detect prompt → send CR → confirm a **fresh** prompt,
the round-trip that proves keystrokes are live), then `run_guarded()`. Readiness
`wait_for` caps each event-wait at 100 ms so a coalesced wake under a busy loop
cannot stall a predicate that is already true.

## Interactive channel: `services/pty_gateway_bridge.py`

Runnable as `python -m claude_hub.services.pty_gateway_bridge`. Byte pipeline:

```
browser → ttyd → local tmux → bridge(0/1) → proxy-PTY → ssh -tt → remote bash/tmux
```

- Spawns argv-less `ssh -tt` with `-o ExitOnForwardFailure=yes -R
  127.0.0.1:<fwd>:127.0.0.1:<hubport>`, attached to a fresh raw proxy PTY.
- `BootstrapHandshake` (IO-free FSM) accumulates the connect stream; user keys
  are **not** forwarded until two real prompts are seen, then it types one line.
- The typed line decodes the existing remote-attach script to a **temp file**
  and runs it as a separate list command:
  `echo <b64> | base64 -d > /tmp/.chp-bootstrap-<tok>.sh; bash <file>; rm <file>`.
  Running the script with stdin = the login PTY is required — piping into
  `bash` made `exec tmux attach` die with *"open terminal failed: not a
  terminal"*. The script (unchanged from the normal path) does
  `tmux has-session || new-session -d …; exec tmux attach-session`.
- After attach the bridge is fully transparent (raw proxy PTYs preserve
  Up/Tab/Ctrl-C/alt-screen/scrollback). Input to ssh goes through a write queue
  drained by `add_writer` so a full PTY input queue never drops bytes.
- On ssh exit it sleeps 3 s and re-runs the connection+handshake; the remote
  detached tmux survives so it re-attaches the same processes.

`ttyd_manager._build_remote_launcher()` branches on the capability: gateway →
`<shell> -lc '<python> -m claude_hub.services.pty_gateway_bridge … --bootstrap-b64
… [--remote-forward F:H]'`; normal → unchanged argv `ssh -tt` while-loop.
`_run_remote_capture_command()` (history/cursor) and `api/remote.py` listing
route gateway profiles through `pty_exec`; normal hosts keep argv ssh.

Forward port is unchanged: workspace allocates `settings.port + 10000`
per-session (`_sessions._next_remote_forward_port`); isolated Hub 8223 → 18223.

## PoC evidence (isolated runtime, real merlin_dev)

Runtime (never touched live 8173/5173 or the default tmux server):

```
PORT=8223 TTYD_BASE_PORT=19000 \
CLAUDE_HUB_HOME=/tmp/ch-poc-home \
CLAUDE_HUB_STATE_ROOT=/tmp/ch-poc-home/workspaces \
CLAUDE_HUB_TMUX_SOCKET=ch-poc-pty \
uv run uvicorn claude_hub.main:app --host 127.0.0.1 --port 8223
```

Remote host throughout: `tiger@g340-cd51-5b00-72cd-4b43-e8b1-c64c`
(Linux 5.15, uptime 140 days). Kerberos ticket valid to 2026-09-24 20:15.

- **(d) directory browse:** `GET /api/remote/filesystem/list?profile_id=merlin_dev&path=~`
  → **HTTP 200 in ~0.43 s**, `current_path=/home/tiger`, 219 items. `/api/remote/profiles`
  reports `transport=pty_gateway`.
- **(a) remote Terminal:** `POST /api/tabs {target:remote, merlin_dev}` → 201;
  bridge passed the `init $` flash, attached remote tmux. After killing the local
  isolated tmux pane (and restarting the Hub, which recovers the persisted tab),
  the fresh bridge re-attached the **surviving detached remote session** —
  `echo $TMUX` inside showed the *remote* socket `/tmp/tmux-1000/default,19757,…`
  (not a local one). `top` rendered (`top - 01:00:19 up 140 days … %Cpu(s) …`).
- **(b) agent report over -R:** created a remote workspace + remote terminal
  worker (`remote_forward_port=18223`); from a second connection in the same pod,
  `curl http://127.0.0.1:18223/health` → `{"status":"healthy"}`, and
  `POST …/sessions/pty-gateway-poc-ws-agent-1/reports` through the tunnel →
  **HTTP 201**, report id `d1600c83-…` persisted on Hub 8223.
- **(c) reviewer:** reviewer created on the same workspace
  (`pty-gateway-poc-ws-reviewer-1`, forward 18224) attached to the gateway host;
  existing `_review.py` placement (follows worker target/profile/cwd) unit tests
  pass (3 passed) and the reviewer pane showed
  `REVIEWER_ON:[g340-…] TMUX=[/tmp/tmux-1000/…]`.

Teardown: stopped Hub + `tmux -L ch-poc-pty kill-server` first (so reconnect
loops cannot recreate sessions), then killed the three `claude-hub-*` remote
tmux sessions and removed `/tmp/.chp-bootstrap-*` on the pod (the unrelated
pre-existing `rhub` session was left intact), then removed `/tmp/ch-poc-home`.
Live 8173 confirmed healthy and untouched.

## Pitfalls / hard-won details

1. **PTY-front race.** Never type before two real prompts; the `init $` splash
   swallows keystrokes. The first prompt + a CR/second-prompt confirm is the
   sync.
2. **Partial PTY writes.** One `os.write` to the master short-wrote **1022 of
   1697 bytes** under uvicorn (ssh hadn't drained the canonical line); the naive
   writer dropped the tail and the sentinel never fired → 20 s timeout. Fix:
   chunk+pace (`awrite`) for one-shots and an `add_writer` drain queue in the
   bridge. (Only surfaced under the server's loop; standalone runs got lucky.)
3. **`tmux attach` needs a tty.** Piping the bootstrap into `bash` leaves stdin
   as the closed pipe. Decode to a temp file and run it as a normal typed
   command so it inherits the login PTY.
4. **Missed-wake robustness.** Cap `wait_for` event waits at 100 ms; never rely
   on a single `Event.set()` landing after `clear()`.
5. **Shared-container namespace.** Multiple connections to the same Trial share
   the pod/loopback, so `-R` from a second connection reaches the first's
   forward — good for reports, but remote tmux names (`claude-hub-<tab8>`) and
   remote listen ports must stay unique per tab/session (already the convention).
6. **Persistence vs process.** Restarting uvicorn does **not** restart the
   independent local tmux pane/bridge; kill the local session to exercise
   re-attach, and kill locals before remote sessions during teardown.

## Residual risks / not covered

- **Pod reclamation:** if the Trial/container is recycled, the remote detached
  tmux and the `-R` forward vanish; reconnect lands on a fresh container. Not
  handled beyond normal retry.
- **Multi-hour idle** over the gateway was not measured (connects during PoC
  were seconds–minutes).
- Multi-session port/tmux-name uniqueness relies on the existing per-tab8 /
  per-session +10000 scheme; two separate Hubs pointed at the *same* Trial still
  depend on distinct `settings.port` bases.
- No SFTP/SCP — file transfer continues over `-R` HTTP, unchanged.
