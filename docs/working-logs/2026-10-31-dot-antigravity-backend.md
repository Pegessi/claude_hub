# Dot execution backend: Antigravity (agy) — 2026-10-31

Branch: `codex/dot-antigravity-provider`. Base: `02ccdbc`, merged with
`origin/main` `3001f00` (Antigravity Goal mode, managed Chat and scheduled
managed Chat tasks) before integration.

## Why

Dot's **Execution backend** editor listed Claude, Codex, Cursor, TraeX and MASO.
At this branch's base the `antigravity` provider covered direct Chat, `agent run`
and terminal tabs (main later added managed Chat); no surface included Dot.
Dot's backend list is derived from the provider registry's
`Surface.DOT`, and the `PUT /api/dot/execution` request model had its own
hardcoded provider union, so both gates kept Antigravity out.

## What changed

- **Provider spec** (`services/agent_stream/providers/spec.py`): add
  `Surface.DOT` to Antigravity's surfaces, map DOT to
  `ApprovalChannel.ONESHOT_STEER`, and mark conversation rotation supported.
  The launch env remains optional; `agyx` owns sign-in, proxy and TLS.
- **Rotation** (`providers/antigravity.py`): Dot starts a fresh session for its
  first turn, every tidy and the turn-count/age limits. `agy` only issues ids
  in its startup `init` record, so rotation reuses the unused fresh process
  already launched for the turn or replaces a resumed/used one, waits for
  `init` (bounded at 120 s), and returns that provider id for Dot to associate.
  The id stays verified and resumable; no constructive placeholder is passed
  to `--conversation`. The candidate is kept in memory only: adoption persists
  it, while discard or a failed rotation restores the previous identity and
  retires the candidate process. A replacement started by the native consumer
  during the swap is recognized by a rotation epoch, so its `init` is not lost.
  Provider death is polled during the wait so a failed launch releases the send
  lock and retires the EOF immediately instead of blocking the failure notice
  and Stop for the full timeout.
- **API** (`api/dot.py`): accept `antigravity` in `ExecutionUpdate`; the
  supported list itself stays registry-derived.
- **Frontend**: add `antigravity` to `DotProvider` and label it `Antigravity`
  in the execution editor. No new preflight: unlike MASO there is no service
  probe, and the editor's existing preset reset applies.
- **Icon**: replace Antigravity's `A` monogram with the official Google Gemini
  mark (Simple Icons CC0), so tabs and status surfaces show the provider's
  actual brand glyph.
- **Golden matrix** (`tests/fixtures/provider_spec_matrix.json`): regenerate;
  the only changes are Antigravity's DOT channel, DOT surface and rotation flag.
- **Tests**: update the registry-order and Dot API list assertions; add a
  focused switch test that changes Claude → Antigravity with an
  `ANTIGRAVITY_MODEL`-only env and checks no old credential is inherited.
  The mocked browser scenario now exposes the option, selects it, verifies the
  `none` preset, then continues the existing Codex switch flow.
- **Docs**: DOT.md backend list and Antigravity launch-env note; the
  installation guide's supported-surface list now includes Dot, with Dot's own
  wake/tidy schedules called out separately from generic scheduled tasks.

- Merge with latest main (Antigravity Goal mode, managed Chat and scheduled
  managed Chat tasks): resolved the provider matrix/goal/changelog conflicts
  and updated the now-obsolete CLI test that still expected `task create
  --agent-type antigravity` to be rejected. The merged matrix keeps all of
  main's surfaces and adds only DOT.

## Boundaries

- This change adds only `Surface.DOT` on top of the provider matrix it merged
  with. Latest main already enables direct-Chat Goals, local managed Chat and
  scheduled managed Chat tasks; resident, managed terminal and remote remain
  off. Dot's wake/tidy schedules are ordinary Chat turns on its Chat.
- Dot's first turn and each rotated turn wait for `agy`'s `init` before writing
  the prompt, so the provider id is real. This is the CLI's normal startup
  cost, not an extra turn; no model request is spent on rotation.
- Goals are not part of this change: main's `GoalSpec.surfaces` entry enables
  `supports_goals` for direct Chat (and therefore Dot's composer); this branch
  neither adds nor alters that capability.
- No live Dot backend was switched during validation; the primary Hub's Dot
  configuration was not modified.

## Independent review

- Claude reviewer: provider death did not wake the init wait (fixed with
  liveness polling and EOF retirement); the wait test never installed the
  future; the avatar test asserted a comment; the browser scenario selected
  Antigravity but applied Codex. All four were fixed.
- Gemini (Antigravity) reviewer: the init tests bypassed stdout (now driven
  through the fake CLI stdout and a consumer that mirrors `NativeConsumerMixin`);
  a consumer-started replacement could lose `init` during the process swap
  (rotation epoch now accepts it); rotation persisted the candidate before Dot
  adoption and had no discard (now two-phase with previous-identity restore);
  early exit left an EOF that could mark the session hard-failed (now retired
  through the abort path); the model-picker test omitted Antigravity (added).

## Verification

- A direct `agyx` probe confirmed that `init` (with a conversation id) is
  emitted on process startup before any user record, so rotation can wait for
  a real id instead of inventing one.
- Full default backend suite (merged tree): 5970 passed, 10 skipped.
- Focused merged backend set (`test_dot_execution.py`,
  `test_provider_spec_registry.py`, `test_antigravity_native.py`,
  `test_managed_chat_admission.py`, `test_scheduled_tasks.py`,
  `test_task_start_surface_admission.py`): 306 passed; the Dot four-file set
  alone is 176 passed.
- Frontend unit suite: 1152 passed; `vue-tsc`, lint and the production build
  pass.
- Backend format (Black/isort), backend types (614 files), docs invariants:
  pass.
- Mocked browser suite: 16/16 scenarios passed. The Dot scenario selects
  Antigravity, applies it through the real editor controls and asserts both
  PUT payloads. This is a mocked API check, not a live provider inference.
