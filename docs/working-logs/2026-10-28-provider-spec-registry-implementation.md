# ProviderSpec capability registry — slices 1–3 implementation

Date: 2026-10-28 · Branch: `codex/rv-provider-spec`
Design packet: `docs/design/2026-10-27-provider-spec-capability-registry-proposal.md`
Base: `main` @ `0211661` · Task: `c1971e29-c59a-469d-be5c-4ec37f402bc4`

## Scope

Implement the review-validated ProviderSpec registry packet: all of slice 1
(registry introduced with golden/equivalence tests), all of slice 2
(non-admission backend consumers), and the slice-3 portions the current
spec shape covers (factory, `eof_is_fatal`, both capability hierarchies,
fork/edit-resend predicate, terminal probe set). Slice-3 TUI launch-argv
builders (`terminal_runtime/process/launch.py`), the conversation-id pin
set (`terminal_runtime/process/state.py`) and cursor-transport gating
(`agent_stream/registry.py` plus its three re-derivations) require spec
fields that do not exist yet (argv templates / a cursor transport variant
model) and deliberately remain on literals. Slice 4 (`/api/providers`,
readiness unification), slice 5 (frontend/CLI list migration) and slice 6
(approval-symmetry cleanup) are out of scope; no wire, argv, env-name or
UI behavior changes were made.

## What was built

**Registry** — `services/agent_stream/providers/spec.py`:

- Frozen Pydantic v2 models: `ProviderSpec`, `GoalSpec`, `AuthSpec`,
  `ResumeSpec`, `ApprovalChannels`, plus str-enums for transport, image
  channel, plan mode, catalog source, reasoning-effort kind, native goals,
  auth, quota, stream source and the eight launch surfaces.
- One `MappingProxyType` `REGISTRY` for claude, codex, cursor, traex, maso,
  terminal with the exact §2.3 packet matrix. Registry insertion order is
  the shared display order (claude, codex, cursor, traex, maso, terminal),
  and `providers_for_surface()` filters in that order so list-deriving sites
  reproduce historical ordering byte-for-byte.
- `get_spec()` gives unknown types a cached capability-empty shell record
  (`transport=shell`, no binary, no surfaces, every channel NONE), matching
  `get_adapter(...) -> None -> structured=False`.
- The packet's immutability nit is satisfied with a frozen **sub-model**
  for approval channels instead of a plain dict: attribute reassignment is
  blocked by frozen config and `__setitem__`/`__delitem__` raise
  `TypeError`; `as_read_only()` hands out a `MappingProxyType`.
- `headless_safe` is coerced from `solo != "none"` by a model validator,
  making the documented derivation enforceable.
- Matrix decisions pinned from code: maso `resume.kind=http_session`;
  cursor approval stays `oneshot_steer` in the matrix (the native transport
  keeps its historical `supports_approval_ui = True` as an explicit override
  with a comment, matching review note 9.1 that only claude diverges across
  the two hierarchies); traex surfaces = direct_chat/subagent/dot/terminal_tab;
  maso surfaces = direct_chat/subagent/managed_chat/dot.

**Consumers migrated (behavior-preserving):**

- `native.py`: binary map/availability from spec (`HTTP_SSE` is the
  no-binary/always-available condition, replacing the MASO literal); session
  factory dispatch via a class map with MASO remaining a lazy import
  (required by the `maso_http → native` import edge).
- `providers/session.py` (`SessionCore`) and `agent_stream/base.py`
  (`AgentStreamAdapter`): capability constants became spec-projected
  properties. Adapters bind `spec_agent_type`; adapter approval surface is
  TERMINAL_TAB for CLI transports and DIRECT_CHAT for HTTP_SSE.
- Deleted duplicated class constants from `ClaudeSession`,
  `CodexConfiguredSession`, `MasoHttpSession`, the four transcript adapters
  and `CodexOwnership`; `cursor.py` retains its one deliberate override and
  codex's `eof_is_fatal` override is now the spec-derived property.
- Env-key maps in `services/subagents.py` and `api/tabs.py` (including the
  traex `TRAEX_REASONING_EFFORT` legacy key order) derive from the spec.
- Dot `PROVIDERS`, `managed_executor_capabilities()` lists,
  `validate_config_admission()`, `launch_env()` model-key handling,
  workspace provisioning traex admission, managed-Chat create admission
  (lazy import there to avoid a ttyd_manager init cycle), transcript-fork
  edit-resend support, and the two terminal probe-filter sets all derive
  from spec surface/fact predicates. Historical rejection messages were
  preserved explicitly.

**Tests** — `tests/fixtures/provider_spec_matrix.json` plus
`tests/test_provider_spec_registry.py` (56 tests): exact golden-matrix
comparison with fixture sort check, deep immutability, fail-closed
fallback, byte-for-byte equivalence for the binary map, env-key maps, hot
keys, dot providers, managed capability lists, CLI surface agreement,
probe set, 12-cell admission truth table, historical error messages,
fork-predicate support, native/transport and adapter flag projections,
and the headless-safe derivation.

## Validation

- `./scripts/verify.sh backend-format` — clean.
- `./scripts/verify.sh backend-types` — mypy clean, 590 files (3.11.16
  canonical toolchain via a worktree-local `.venv` synced from the uv cache;
  the shared primary venv is editable-bound to another checkout and failed
  provenance when symlinked).
- `./scripts/verify.sh backend-tests` — **5494 passed, 10 skipped, exit 0**
  (canonical scope with coverage).
- Focused reruns: all 56 new tests; 841 tests across the migrated modules'
  suites pass; an earlier full-tree direct pytest run had 5521 passed with
  only three failures, each reproduced against pristine `main`
  (`0211661`) in a disposable sibling worktree — one environmental MASO
  preflight test (a live service answers on 127.0.0.1:9796 in this
  environment) and two timing-sensitive `test_native_goal_transport`
  handoff tests. None are caused by this change; the canonical
  `backend-tests` target passes fully.

## Residual risks / notes

- Slice-3 launch-mechanic sites still on literals: TUI solo/start argv
  builders in `terminal_runtime/process/launch.py`, the
  CLAUDE/CODEX/CURSOR conversation-id pin set in
  `terminal_runtime/process/state.py`, and cursor-transport fail-closed
  gating in `agent_stream/registry.py` with the independent native-vs-tailer
  re-derivations in `runtime/manager.py:98`, `api/agent_stream.py:339` and
  the `cursor_transport` field handling in
  `services/restart_continuation.py`.
  Migrating these needs new spec fields (argv templates; a session-scoped
  cursor transport variant per packet §4), so they were not forced into the
  current shape. All other slice-2 and modeled slice-3 facts are migrated.
- TraeX `model/list` runtime behavior remains empirically open per packet
  footnote 1; the spec records the code path (`rpc_list` + static fallback).
- Frontend provider lists and CLI click choices are not migrated yet
  (slices 5–6). `cli/commands/dot.py` still ships its pre-MASO four-item
  list; a registry-equivalence test asserts it is a subset of DOT surfaces.
- The cursor native-transport `supports_approval_ui=True` override is
  preserved rather than forced to the spec's `oneshot_steer` projection;
  collapsing that distinction belongs to the slice-6
  `StreamCapabilities`/approval-field decision (packet open item 7.8).
- No persisted approval/readiness state, `/api/providers` endpoint, probe
  changes, or frontend changes were introduced.
