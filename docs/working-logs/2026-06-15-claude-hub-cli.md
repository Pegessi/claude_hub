# 2026-06-15 — `claude-hub` CLI

## System Overview

`claude-hub` is a dependency-light command-line client over the existing Agent
Workspace REST API. It lets humans and agents drive the hub from a shell:
listing/creating workspaces, queueing and steering tasks, ensuring resident
agents, sending session messages and reports, and reading lessons. It talks to
the same backend the web UI uses; it adds no new server endpoints.

The CLI is installed as a console script via `[project.scripts]`
(`claude-hub = "claude_hub.cli.main:cli"`). Its only runtime dependency is
`click` (added to `[project].dependencies`). It can also be run module-style:
`python -m claude_hub.cli`.

> The Feishu chat bot was split out of this MR; this change is CLI-only.

## Module Design (`backend/claude_hub/cli/`)

- `main.py` — root Click group and shared helpers. Defines global options
  (`--base-url`/`CLAUDE_HUB_URL`, `--token`/`CLAUDE_HUB_TOKEN`, `--cookie`,
  `--json`, `--config`/`CLAUDE_HUB_CONFIG`, `-v`/`--verbose`), resolves them
  into a `Settings` object stored on the Click context, and `_register()`
  attaches the subcommand groups. `get_client()` builds a `HubClient` and is
  the seam tests monkeypatch to inject an `httpx.MockTransport`.
- `config.py` — `Settings` dataclass + `resolve_settings()`. Precedence is
  flag > env var > TOML config file (`[default]` table) > built-in default.
  Defaults: base URL `http://127.0.0.1:8173`, config path
  `~/.config/claude-hub/config.toml`.
- `client.py` — `HubClient`, a thin typed wrapper around the REST endpoints
  using `httpx.Client`. Auth is a session cookie (`--token` sets
  `claude_hub_session`; `--cookie` supplies a raw header). Raises `HubError`
  (carrying the HTTP status) on >=400 or transport failure.
- `output.py` — dependency-free rendering: aligned tables for lists of dicts,
  `key : value` lines for single dicts, indented JSON under `--json`. Cells are
  stripped of ANSI/control chars and truncated to keep columns aligned.
- `commands/` — one module per group: `workspaces.py` (`workspace`
  list/create/board with `status` alias, plus `agent` list/create),
  `tasks.py` (`task` list/create/start/continue/send/abort), `sessions.py`
  (`session` send/report), `lessons.py` (`lessons` list/get).

## Key Pitfalls

- **mypy `disallow_untyped_defs = true`**: every CLI function (including Click
  callbacks) needs full annotations. `get_client`/`as_json` take a typed
  `click.Context`; command callbacks annotate all params and `-> None`.
- **`trust_env=False`**: the `httpx.Client` is created with `trust_env=False`
  so ambient proxy env vars (`HTTP_PROXY`, etc.) do not hijack loopback
  requests to the local backend. This mirrors the backend's own proxy
  scrubbing and avoids spurious 502s.
- **Non-dict-row defensive rendering**: `_row_cells()` coerces a non-`Mapping`
  row into a single cell (remaining columns blank) instead of crashing, so a
  malformed server response still renders a clean table.
- **Real verbose URL**: `--verbose` logs the request via
  `client.build_request(...)` before sending, so the printed URL is httpx's
  actual joined `base_url + path` and is available even if `send()` raises.
- **Config precedence / edge handling**: a missing or malformed TOML file is
  ignored (falls through to env/default), and config values of the wrong type
  (e.g. `base_url = 8173`) are dropped rather than coerced. Cookie parsing
  skips malformed parts with an empty key.
- **Loopback bypasses auth**: a local backend needs no token; commands exit
  non-zero on API errors so they compose in scripts.

## Files

- `backend/claude_hub/cli/` (new package)
- `backend/tests/test_cli.py`
- `backend/pyproject.toml` (adds `click` dependency + `claude-hub` script)
- `README.md` (CLI section)
- `CHANGELOG.md` (Unreleased feat entry)
