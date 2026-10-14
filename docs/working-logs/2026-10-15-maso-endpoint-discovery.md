# Local MASO endpoint discovery and startup guidance

## Scope and ownership

Task `f234fe5f-1010-4d63-a0ee-326bef676c02` remains initiator-controlled,
with configured reviewed policy retained. The user requested that the Claude Hub
skill check the actual local MASO URL before startup, then clarified that starting
with an environment variable is the preferred direct path.

Worktree: `~/claude_hub_worktree/maso-endpoint-discovery`, branch
`codex/maso-endpoint-discovery`, base
`2a43b616c65ce6dd15c78633ff48d7a4cea14883`. Main matched origin after fetch;
unrelated dirty main files were preserved. No primary Hub/MASO service restart,
model invocation, existing Chat mutation, merge or push is included.

## Contract and implementation

- Prefer `MASO_BASE_URL=<validated-url> ./start.sh`. Chat URL overrides Hub URL;
  the Hub process's `MASO_PORT` is a loopback startup shorthand. A failed explicit
  target never falls back to another service.
- With neither setting, read the local MASO instance registry and legacy 9796
  endpoint. Validate model catalog/default responses, ignoring stale entries and
  public export URLs. Require a unique usable instance; multiple instances need
  explicit selection. Read at most 64 KiB and probe at most 16 candidates within
  one 3.5-second deadline.
- Startup emits the actual URL or a diagnostic without blocking other Hub
  providers. Preflight and creation recheck; new Chats freeze the selected URL.
  Existing saved service identities do not change with registry/default changes.
- The standalone `python -m claude_hub.maso_probe` returns JSON for skill/service
  operators. `--startup` gives a nonfatal diagnostic. No discovery starts MASO,
  creates sessions, invokes models or scans arbitrary ports/processes.

## Skill update

The installed skill is
`~/.maso/skills/skills/agents-automation/claude-hub`, a plain installed directory
rather than a Git checkout. Its complete candidate was staged under
`.agent_office/maso-endpoint-discovery/skill/claude-hub` and validated with
`skill-creator/scripts/quick_validate.py`. The original entrypoint is preserved
as `.agent_office/maso-endpoint-discovery/SKILL.original.md`.

The focused entrypoint addition routes MASO connectivity/startup work to a new
reference. That reference leads with explicit environment startup, provides a
unique-probe-result-to-environment recipe, and documents bounded read-only steps
for deployed older main without the helper. It preserves shared-service
authorization and requires a new Chat for changing an existing endpoint.

The portable delta is retained as
[the skill patch](2026-10-15-maso-startup-skill.patch). Only the entrypoint and new
reference are installed; unrelated skill files and prior local adaptations are
preserved. This is a local skill update, not an upstream skill publication.

## Independent review and validation

An independent read-only reviewer found and verified fixes for two P2 issues:
deeply nested malformed registry JSON must not abort Hub startup, and per-Chat
port shorthand must not disagree with UI preflight. `RecursionError` now falls
back safely, while `MASO_PORT` is restricted to process startup and Chat presets
use `MASO_BASE_URL`. The revised candidate passed independent review.

- Initial focused tests: 71 passed; retained evidence
  `/tmp/claude-hub-verify.wRgnOh/pytest.log`.
- Independent revised regression/wiring tests: 20 passed; evidence
  `/tmp/claude-hub-verify.zziitP/pytest.log`.
- Live read-only probe with both address/port variables unset found
  `http://127.0.0.1:11004`, 8 models, from the current local registry/service.
- Isolated ASGI Hub preflight against actual MASO passed automatic and explicit
  URL checks; an explicit unavailable address remained unavailable rather than
  falling back. No tab state changed. Evidence is `live-preflight.json` in the
  task artifact directory. Its owned `ch-maso-discovery-api` tmux server was
  inspected (keepalive only) and stopped after the application lifespan exited.
- The first complete run passed backend behavior (3303 passed, 7 skipped) and
  all frontend targets, but found formatting/type issues in changed files.
  Those are fixed; final complete verification is recorded below when finished.
- Final backend type check: all 252 source files passed after the fixes.
- Skill validator, patch dry-run, shell syntax and diff whitespace checks passed.

Locked dependencies were installed independently in the feature worktree with
Python 3.11.16 and cached pnpm 9.15.9; no environment was copied from main.
Task evidence and tool wrapper live under `.agent_office/maso-endpoint-discovery`.
Full runner logs are `verify-all.log` and `verify-final.log` there.

Installed-skill readback and validator: passed, with both installed files
byte-identical to the independently reviewed candidate and original entrypoint
retained. Final `./scripts/verify.sh all`: exit 0; all nine standard targets
passed. Backend behavior: 3304 passed, 7 skipped (browser scenarios requiring
explicit opt-in), 33 warnings. Frontend lint/types/unit tests/build passed.
Final evidence: `.agent_office/maso-endpoint-discovery/verify-final.log`.

## Limits

Readiness validates advertised configuration, not upstream credentials/quota or
inference success. Unsupported/unreadable registries and unregistered nondefault
ports may require an explicit URL. Real browser creation and deployed startup
were not exercised; the primary running service was left intact. Backend changes
are delivered on the feature branch and do not alter running main.
