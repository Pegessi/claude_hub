# Managed Workspace Chat execution

## Scope and baseline

User request: enable Chat execution for Workspace agents and independent reviewers,
not only a read-only history view or a reviewer-only path. Keep the separate
runtime-guidance frequency issue unchanged.

Implementation starts from local `main` at `a89dd78` in the isolated
`feat/managed-workspace-chat` worktree. The fetched remote and local main have
separate commits; this work preserves the deployed local baseline without an
unrequested integration or service restart. No production state migration occurs
until an authorized deployment.

## Behavior

- `session_kind` is explicit and defaults to Terminal for existing callers.
  Local Claude/Codex/Cursor support Terminal or Chat; managed MASO is Chat-only
  and restricted to a loopback service. SSH remains Terminal-only.
- A managed Chat has a backing tab for navigation and identity checks, but no
  ttyd, tmux or shell. Ordinary standalone Chat/Terminal behavior is preserved.
- Workspace assignment, continuation, review selection and report intake retain
  their existing Task and Managed Session identities. The frontend resolves the
  managed session before using its stream endpoints, avoiding a second tab-scoped
  provider conversation.
- The Workspace mailbox retains immutable request fingerprints, original
  envelopes, queued input and report ACKs. The native bridge writes separate
  provider-handoff evidence before sending; display `turn_started` is not used
  as an acceptance receipt. Ambiguous sends stay uncertain and cannot replay.
- Stop is scoped to the expected turn, records `chat_paused`, and prevents the
  background mailbox pump from starting another queued turn. Explicit user
  input or Task continuation resumes it; stale Stop does not pause a newer turn.
- Native provider status replaces terminal output heuristics. A turn ending
  without the required Task report becomes visible as needing input rather than
  completing a Task or replaying an assignment.
- Unrelated reviewer tasks use a fresh provider context while keeping display
  history. Same-task fix/re-review cycles retain context. Auto-created reviewers
  inherit the worker's Chat/provider/environment selection.
- Managed composer controls do not offer direct-Chat Goals, schedules or
  edit/fork operations that would bypass Workspace task authority.

## Boundaries

A loopback MASO endpoint is not proof that service-side tools can reach every
requested file or the Hub callback. The deployment must provide the shared local
working directory and a usable report URL. No remote service credential copying
or broad access-control change is introduced. MASO's native uncertain-delivery
fence is preserved even if Workspace records later acknowledge a call.

The existing terminal-only legacy `spawn_worker` endpoint remains disabled.
Creating an execution agent and starting a Task is the supported path.

Chat removes managed terminal infrastructure, but provider resource behavior
still differs: Claude/Cursor use one-shot CLI turns, Codex retains an app-server,
and MASO runs inside its own service. No quantitative resource savings are claimed
without a benchmark.

## Review and validation

The final source candidate passed independent review against `a89dd78`, including
frontend commit `3dbab968` and the integrated working-tree changes. The reviewer
verified all 64 file hashes in snapshot revision 3 and reported no remaining
source or test blockers. Review fixes covered Task surface propagation, separate
managed/provider identities, reviewer surface matching, durable delivery cleanup,
strict provider teardown, stable CLI retry IDs, and direct-to-managed Chat
conversion. A synchronous metadata update now rejects that conversion before
mutation: it cannot remove an existing direct Chat's terminal resources.

Validation completed on 2026-10-15:

- `./scripts/verify.sh all`: PASS. Backend: **3485 passed, 7 skipped**;
  frontend: **839 passed**. Documentation, checkout safety, Black/isort, complete
  backend/frontend type checks, ESLint and production bundle build also passed.
  Backend coverage was 81%. The skipped cases were six opt-in browser checks and
  one clean-delivery Git-provenance check while the candidate was uncommitted.
- `./scripts/verify.sh docs`, `./scripts/verify.sh check-staged`, and
  `git diff --cached --check`: PASS after staging the intended delivery files.
- Mocked browser acceptance: **9 runners passed**, including Workspace Agent/Task
  surface selection and managed-session stream identity. The final managed
  Ctrl/Cmd+Enter scenario also passed. These use mocked HTTP/provider responses;
  they do not establish real model execution.
- Isolated `terminal-hmr`: **12 passed** with real task-owned terminal/backend
  resources. Task browser port 5297 was independently checked closed afterward.
- The final metadata guard passed **207 focused backend tests**; the independent
  reviewer additionally reran the four directly affected TTYD tests.
- Earlier full-suite attempts failed on compatibility issues and stale fixture
  signatures. They are not counted as successful verification; the final default
  suite above is the complete passing run.

Local evidence is preserved under `/tmp/managed-chat-delivery.M8HYxh/`:
`verify-all.log`, `review-snapshot-r3.json`, and `browser/`. The passing browser
summary is `browser/claude-hub-browser-mocks.KMvNLf/run/summary.json`; original
backend and HMR evidence remain at `/tmp/claude-hub-verify.W5UDlh/` and
`/tmp/claude-hub-verify.eECAFV/`. Generated artifacts are not committed.

Not run: real Claude/Codex/Cursor/MASO model tasks, provider restart/recovery in a
live deployment, resource benchmarks, manual human UI acceptance, and the separate
`terminal-replay` suite. Existing deprecation/OpenAPI warnings and the bundle-size
warning remain non-fatal. No shared Hub restart, deployment, merge or push occurred.
