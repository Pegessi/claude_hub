# AI-native repository maintenance boundaries

This change turns four repository-maintainability goals into executable
contracts: bounded source growth, useful static typing, deterministic fast tests
and smaller terminal-proxy owners. The change does not alter provider, GPU or
production-service resources.

## Decisions

### Repository metrics and warning policy

- Run `scripts/check_ai_metrics.py` from the existing `verify.sh docs` target so
  the check needs only the Python standard library.
- Keep new backend Python files at or below 1500 lines and frontend Vue/TypeScript
  files at or below 2500 lines. Existing larger owners have exact ceilings;
  shrinking one requires lowering its ceiling in the same change, leaving no
  unused growth allowance.
- Reject new or changed runtime import cycles. The existing services /
  WorkspaceManager compatibility cycle is an exact baseline; shrinking or
  splitting it requires updating the baseline in the same change, so a later
  reconnection cannot hide inside the original larger member set.
- Keep strict-schema measurement intentionally bounded to durable task/schedule
  state and execution-authority envelopes. Accept direct `extra="forbid"` and
  inheritance from a strict local base, but reject a permissive subclass override.
- Parse mypy overrides as TOML and reject `ignore_errors` or disabled
  `attr-defined` / `no-any-return` checks for WorkspaceManager modules. Text
  matching would allow equivalent single-quoted, list or submodule overrides.
- Make the three cleaned warning families fail the backend gate: Pydantic V2
  class-based configuration, duplicate FastAPI operation IDs and per-request
  HTTPX cookies. Precisely suppress the two pinned `lark_oapi` / `websockets`
  deprecations that application code cannot fix, leaving the default suite free
  of warning noise. OpenAPI generation also asserts globally unique operation IDs.

### WorkspaceManager type contract

Every WorkspaceManager mixin inherits `_WorkspaceManagerBase`. Its cross-mixin
members are abstract, so deleting or renaming a provider leaves the composed
`WorkspaceManager` abstract at type-check and runtime instead of silently
inheriting a placeholder. A focused test asserts that the final class implements
all abstract members.

The scheduled Chat dispatch, readiness and liveness callbacks now have exact
async callable signatures rather than `Any`. The old package-wide mypy exemption
is removed; the ordinary backend type target checks every mixin.

### Deterministic workspace tests

`test_workspaces.py` does not own the terminal prompt handshake. Its autouse
fixture now replaces `_wait_for_agent_prompt` on the singleton with an explicit
async no-op instead of letting each fake terminal consume the real polling
budget. It does not change production constants, sleep or terminal capture.
Prompt readiness, Codex update-dialog recovery, fail-closed timeout behavior and
Chat seat checks remain in their focused test modules.

### Terminal injection ownership

`api/terminal.py` keeps the HTTP proxy and delegates HTML mutation to
`_terminal_injection.py`. The CSS, bootstrap half and runtime half are separate
bounded modules, each below the new backend-file limit. A representative render
containing quotes, Unicode and escapes was byte-compared before and after the
split and remained identical. Focused tests lock JSON encoding, case-sensitive
head handling, multi-head replacement and the existing replay/input-latency
markers.

## Validation

Focused evidence collected during implementation:

- `test_workspaces.py`: 148 passed in 17.62 seconds (18.914 seconds including
  the verification wrapper).
- Prompt readiness state-machine selection: 3 passed in 0.16 seconds; the Chat
  no-terminal-probe case passed separately in 0.04 seconds.
- Metrics, warning cleanup, terminal injection, OpenAPI, auth, Feishu and type
  contract selection: 67 passed in 5.45 seconds before the final broadened run.

Final document, format, type, backend, frontend and standard `verify.sh all`
results are recorded in the delivery receipt. The explicit terminal replay gate
was not run when no existing Chromium executable was available; no browser was
installed as an implicit side effect.
