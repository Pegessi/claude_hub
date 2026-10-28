# SSH destination validation and argv separation

2026-10-27, branch `codex/rv-ssh-validate`, base `main@0211661`.

## Problem

Remote tabs and the directory-listing API construct ssh commands from
`RemoteProfile.ssh_host` / `user` / `port`. Profiles come from the out-of-band
`~/.claude_hub/remote_profiles.json` file and from parsing `~/.ssh/config`, so
the values are not typed by an API schema. A host or alias beginning with `-`
(e.g. an ssh-config entry `Host -oProxyCommand=...`) would be interpreted by
ssh as an option rather than a destination — argument injection into every
remote-tab, capture, listing and gateway-bridge invocation. The interactive
reconnect launcher additionally rendered the destination into a shell string.

## Fix

Two independent layers, in backend code only:

1. Validation on `RemoteProfile` (`models/schema_domains/terminal.py`):
   - `validate_ssh_host` plus a pydantic v2 `field_validator`: accepts DNS
     hostnames and SSH aliases (labels of `[A-Za-z0-9_-]`, hyphens not at a
     label edge, underscores allowed because OpenSSH aliases such as
     `merlin_dev` use them, max 253 chars, optional single trailing dot for an
     absolute FQDN), literal IPv4 and bracketless IPv6. Rejects empty,
     option-leading (`-...`), metacharacter-bearing, bracketed and scoped (`%`
     zone id) values, and values over 253 characters.
   - `user`: conservative charset `[A-Za-z0-9._-]`, must not start with `-`;
     blank/whitespace normalizes to `None`.
   - `port`: `Field(ge=1, le=65535)` (default 22).
2. Invocation hardening at every ssh construction site:
   - `services/pty_exec.py` `build_ssh_argv` (interactive command-less
     `ssh -tt`, used directly and via the PTY-gateway bridge),
     `api/remote.py` directory listing, and both launchers in
     `services/terminal_runtime/process/remote.py` (one-shot argv and the
     shell-string interactive/reconnect launcher) now place `--` immediately
     before the destination. In the shell launcher every element, including
     `--`, goes through `shlex.quote`, so `--` survives quoting verbatim.
   - `services/pty_gateway_bridge.py` `main()` converts a pydantic
     `ValidationError` from `--target`/`--port` into a clean `SystemExit`.
   - `services/remote_profiles.py` ssh-config discovery skips an alias that
     fails validation with a warning instead of aborting discovery. The
     configured-JSON loader already skipped invalid entries (broad catch);
     that path is exercised by a new test.

Audit of remaining ssh sites: `grep` over `backend/claude_hub` shows only the
four builders above; merlin/seedjob "stdin-shell" routing is capability
classification only and its `user@host` comes from the same validated
`RemoteProfile` (`pty_exec.ssh_target`, `api/remote._ssh_target`,
`ProcessRemoteMixin._remote_ssh_target`). The only other `user@host`
rendering, in `workspace_manager/prompts/bootstrap.py`, is a prompt display
label, not a command. There is no API that writes profiles (GET only), so no
422 path applies: invalid JSON/config entries are skipped at load time.

`ssh [options] -- destination [command]` is standard OpenSSH syntax
(supported since OpenSSH 5.x; verified on local OpenSSH 9.6p1:
`ssh -- -V` fails resolving a host named `-V` instead of printing the
version).

## Salvage review

The prior session's uncommitted change was reviewed critically and kept, with
two corrections:

- The hostname regex rejected legitimate absolute FQDNs with a trailing dot
  (`host.example.`), which could make a previously working stored profile
  disappear silently. An optional single trailing dot is now accepted
  (double-dot and trailing-dot-after-empty-label forms stay rejected).
- The comment claimed scoped IPv6 addresses were rejected, but
  `ipaddress.ip_address("fe80::1%eth0")` succeeds, so they were in fact
  accepted into a form ssh cannot use without brackets. `%` is now rejected
  explicitly.

## Validation

- `uv run pytest backend/tests/test_remote_profile_validation.py
  backend/tests/test_pty_gateway.py backend/tests/test_remote.py
  backend/tests/test_ttyd_manager.py -q` — 275 passed.
- Full backend suite: 3147 passed, 1 skipped, 1 pre-existing failure
  (`test_maso_preflight.py::test_create_rechecks_unavailable_service_before_tmux_or_tab_state`);
  confirmed the same failure on the pristine base with the change stashed — it
  probes model services on fixed ports and is unrelated/environmental.
- `black --check` and `isort --check-only` on all ten changed files: clean.
- `mypy --config-file pyproject.toml .` from `backend/`: success, 582 files.

## Risks and follow-up

Follow-up commit (same branch, independent security review APPROVE):

- `_load_configured_profiles` now guards a non-list top-level JSON store
  (object/scalar/string/null): logged at ERROR and treated as an empty store
  instead of raising `TypeError` through the profiles API. Previously a
  malformed operator-owned file 500'd `GET /api/remote/profiles`.
- Locked additional reject tests: hosts containing tab/LF/CR, U+2010 and
  U+FF0D/U+FF2F fullwidth lookalikes; `user="-"` and `user="a@b"`.

- Stored profiles whose `ssh_host` uses characters outside the conservative
  set silently disappear from `GET /api/remote/profiles` (a warning is logged
  server-side). Bracketed/scoped IPv6 literals were never usable in the
  unbracketed `user@host` form the Hub builds.
- The charset is deliberately conservative; exotic but legitimate remote
  usernames (e.g. realm suffixes, trailing `$`) are rejected. Profiles are
  operator-managed, so relaxing the charset later is a one-validator change.
- Browser/UI behavior was not exercised; the change is backend-only and no
  frontend field sends profile data today.
