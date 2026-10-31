# Agent CLI installation guide — 2026-10-31

## Scope and baseline

- Branch: `docs/agent-cli-install-guide`.
- Worktree: `~/claude_hub_worktree/agent-cli-install-guide`.
- Base: `ec40d57bd2b1b36c1090796202a95e6dcb5a795c` (local `main` after fetch;
  local main was 53 commits ahead of `origin/main`, with no remote-only commits).
- User request: document third-party Agent installation, including Claude Code,
  Codex, Cursor and Antigravity; additionally flag special proxy configuration.
- Documentation only. No CLI installation/login, model request, private proxy
  inspection/configuration, service restart, main merge or push was performed.

## Changes

- Add `docs/AGENT_INSTALLATION.md` with official install/authentication links,
  actual Hub executable names, service-account/PATH checks and manual acceptance.
- Distinguish Cursor `agent` from the desktop command and older `cursor-agent`
  spelling; distinguish official Antigravity `agy` from deployment-owned `agyx`.
- Explain per-provider proxy defaults, manual login environment differences,
  `auto`/`inherit`, private certificate handling and Antigravity wrapper dependencies.
  No real proxy endpoints, credentials or certificate contents are included.
- Link from README requirements/quick start and deployment prerequisites; update
  the changelog. Existing unrelated startup examples were left unchanged.

## Evidence and validation

- Checked official installation/authentication pages, including the current
  `cursor.com/docs` pages (older `docs.cursor.com` pages use a different command name).
  The installation guide contains the source links.
- Read ProviderSpec, Antigravity command/environment construction, `setup.sh`,
  `.env.example` and existing deployment proxy documentation.
- Local version/help-only checks: Claude Code `2.1.304`, Codex `0.163.0`,
  Cursor Agent `2026.10.22-27dcf5f`, Antigravity `1.4.11`. Confirmed Codex
  `login status`, `--device-auth`, `--with-api-key` and `app-server --help`.
  No existing `agyx` wrapper was executed or modified.
- `./scripts/verify.sh docs`: passed.
- Local guide link/anchor checks, README/deployment entry-link checks and
  `bash -n` on all eight guide shell examples: passed; syntax checks do not run
  installers or prove installation/CLI compatibility.
- `git diff --check` and `./scripts/verify.sh check-staged`: passed.
- `./scripts/verify.sh all` was attempted as an environment check before editing:
  docs and checkout-safety passed; the seven backend/frontend targets could not
  run because this fresh worktree has no backend `.venv` and `pnpm` is unavailable
  on PATH. No dependency installation was added to this documentation task.
- Independent read-only review covered official sources, provider command/surface
  contracts, new documentation and the added proxy section: no blocking findings.

## Follow-up: machine-specific proxy requirements

The user clarified that proxy needs vary by machine and provider and must be
checked during installation. The guide and README now make this explicit;
Antigravity's `agyx` executable requirement is separate from optional proxy/TLS
configuration. The deployment checklist now requires network verification while
making proxy tests, file changes and service restarts conditional on actual need.
Existing private configurations are not copied or changed by this documentation.
Follow-up validation: document invariants, nine guide/entry links and anchors,
whitespace and staged-content checks passed. Independent read-only review found
no blocking issues. No runtime checks were rerun for these prose-only changes.

## Authorized local-main integration

The user authorized merging after the two independent cross-reviews. The fixed
merge candidate is `c893a5955d343a71c500956eece938aae0add891`, with target/main
parent `b0d994953980ef8b18e1e691ce85db97f05cabb0` and reviewed documentation parent
`59ff4105a21da74dba3027aca0f130adf52ca027`. It was prepared in
`~/claude_hub_worktree/agent-cli-install-integration`.

Only `CHANGELOG.md` conflicted. Its resolution keeps the documentation entry
first, the main Antigravity recovery entry second, and the shared history
unchanged. A byte comparison verified both entries and the history against the
parents; the other four reviewed documents match the source commit exactly.
The merge changes no runtime code relative to its main parent.

Document invariants, whitespace and staged-content checks passed. The
[fixed merge decision record](2026-10-31-agent-cli-install-merge-decisions.json)
binds the conflict resolution to that immutable merge. Independent verification
of the fixed merge and this record is required before fast-forwarding local main.
The integration authorization does not include a remote push or service restart.
No full runtime suite was rerun for this documentation-only merge.

## Remaining verification boundary

No clean-machine installation, interactive login, provider network request or
live Hub integration was tested. The documented operator checklist remains the
acceptance path for a new deployment. No runtime code was changed.
