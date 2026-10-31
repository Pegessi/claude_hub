# CLI connection error URL credential redaction

## Problem

Independent review (`VERDICT: REQUEST_CHANGES`, review task
`8b1d4287-9d8f-4650-9860-2b4b8ce93f5d`) of the CLI base-URL branch at
`22fcc29` found a blocking credential disclosure in
`HubClient.request_response`: on `httpx.HTTPError` the message interpolated
`request.url` verbatim. Two secret-bearing cases leaked to the terminal:

- `auth callback --code/--state` sends `GET /api/auth/callback?code=...&state=...`;
  a connection failure printed the OAuth authorization code and CSRF state.
- A base URL with embedded userinfo (`http://user:password@host:port`)
  printed the URL-encoded password (and username) in the same message.

The underlying `httpx` exception string itself (`[Errno 111] Connection
refused`) carries no URL, so the interpolation in `client.py` was the only
leak path. The `--verbose` request line intentionally prints the real URL to
stderr and is opt-in; it is unchanged.

## Fix

`backend/claude_hub/cli/client.py`:

- New `_sanitize_attempted_url(url)` returns
  `url.copy_with(query=None, username="", password="")`, preserving scheme,
  host, port and path while stripping query and userinfo.
- The transport-error branch renders the sanitized URL and derives the IPv6
  loopback hint from it (host/port are unchanged, so the same-port hint is
  identical to before). The generic hint text and `CLAUDE_HUB_URL` mention
  are untouched.

## Test evidence

`backend/tests/test_cli.py` adds two regression tests alongside the existing
connect-hint tests:

- `test_transport_error_redacts_oauth_callback_query_secrets`: a refused
  callback call shows `http://127.0.0.1:9911/api/auth/callback` and the
  `http://[::1]:9911` hint but never the code/state values, `code=`,
  `state=` or any `?`.
- `test_transport_error_redacts_url_userinfo_password`: a
  `http://connector:…@127.0.0.1:9911` base URL renders only
  `http://127.0.0.1:9911/...`; username, password and `@` never appear.

Checks run:

- `pytest tests/test_cli.py`: 91 passed.
- Focused CLI-adjacent suite via `scripts/verify.sh backend-tests`
  (test_cli*, test_review_resubmit_cli, test_task_dependencies,
  test_env_preset_resolver, test_dot_api, test_scheduled_tasks,
  test_feedback_automation, test_feishu_commands): 507 passed.
- `scripts/verify.sh backend-format`, `backend-types`, `docs`: pass.
- Live refused-port probes (`127.0.0.1:9`) with real OAuth callback params
  and real userinfo confirmed neither secret appears and the same-port IPv6
  hint survives.
