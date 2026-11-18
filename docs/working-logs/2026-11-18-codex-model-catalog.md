# Codex 6.2 model discovery and switching

Task: `262f6e1d-9cc4-45a5-87e2-493941fc4a3c`
Worktree: `fix-codex-model-switching-to-6-2-262f6e1d`
Base: `1d92bce968343bf95c463f7e7e8e615bdf13158d`

## Root cause and evidence boundaries

There are two separate contracts: a picker can select and transmit an exact
model ID, but only the provider can authorize inference with that ID.

- Before a session existed, `model_catalog_for("codex")` returned only the
  static 5.x list. It did not read the CLI's `models_cache.json`.
- After startup, `CodexConfiguredSession._discover_models()` replaced the
  catalog with `model/list` and retained it for the lifetime of the transport.
  A provider subset therefore hid cache-only/new models and their reasoning
  metadata. Merely editing the static list would not fix this path.
- SessionCreator had no Codex model/effort picker at all; it offered only
  provider/preset selection and the existing-session selector. Chat's existing
  picker already consumed `StreamModelOption` and correctly wrote
  `CODEX_MODEL` / `CODEX_REASONING_EFFORT` through switch-env. The native
  transport already forwards overrides via per-turn collaboration settings.

Local evidence is time-scoped. Codex was version 0.163.0; config contained no
model/provider/catalog override. At 06:27–06:32 UTC, both the shared cache and
fresh isolated discovery lacked 6.2. Two no-tool ephemeral turns for the exact
IDs returned HTTP 400 / `model_not_found`: the models were not supported with
the current ChatGPT account. Thread creation accepted the IDs, which is **not**
evidence of successful inference.

The follow-up reported a 06:36:53 cache containing visible 6.2 entries. When
reread at implementation time it had refreshed to 06:37:13 and again lacked
them. The fix does not rewrite this shared CLI-owned file or treat its
`supported_in_api` flag as proof of account entitlement. The clarified scope
explicitly requires cache merging and static 6.2 entries despite this external
availability limitation.

## Design and behavior

Reuse the existing model option schema, provider runtime seam, model parser
module, authenticated Codex router, launch environment and Chat switch path.

1. Normalize CLI `slug`, `display_name`, `default_reasoning_level` and
   `supported_reasoning_levels[].effort` into the same parser as app-server
   metadata. Only `visibility=list` and not explicitly API-unsupported cache
   entries are offered. Malformed entries/efforts are ignored.
2. Read the effective `CODEX_HOME/models_cache.json`, otherwise
   `HOME/.codex/models_cache.json`; session overrides do not enter a global
   shared cache. Missing, unreadable, invalid JSON/UTF-8 or wrong-shaped files
   safely fall back.
3. Precedence is live RPC metadata, then cache metadata, then static IDs.
   Cache-hidden/unsupported IDs cannot reappear via the static fallback.
   RPC-hidden IDs cannot reappear via cache/fallback. Deduplicate by exact ID.
   The fallback includes 6.2 Sol/Astra, 6.1 Terra/Sol, 6 Astra/Sol/Luna and the
   existing 5.x models; it does not invent reasoning capabilities.
4. Read the file before app-server startup can refresh it. Capability requests
   reread file-derived data instead of hiding changes behind the global
   ten-minute or transport preparation cache. Already-discovered RPC metadata
   remains process-local; repeated requests do not spawn extra app-servers.
   TraeX retains its independent catalog behavior.
5. Serve `GET /api/codex/models` for the local creation picker without starting
   a provider. Preserve exact IDs and reasoning metadata in Chat capabilities.
   Selected models also reach `thread/start` / `thread/resume`, in addition
   to the existing next-turn override. Cache-only model effort validation uses
   the merged catalog; static-only models preserve explicit effort overrides
   because their support is unknown, not an advertised empty set.

No main Hub/MASO process, shared configuration, credential, or cache is modified.
This feature branch is not a live deployment.

## Validation

- Focused backend command:
  `./scripts/verify.sh backend-tests -- tests/test_agent_stream_native.py tests/test_native_provider_helpers.py tests/test_traex_agent.py tests/test_managed_chat_controls.py tests/test_codex_sessions.py -q`.
  **337 passed**, evidence `/tmp/claude-hub-verify.ik9fokzy`.
- Covers cache metadata/efforts, malformed/missing files, replacement freshness,
  HOME/CODEX_HOME isolation, hidden exclusions, live precedence, failed/empty
  RPC fallback, API listing without subprocess startup, both exact 6.2 IDs in
  managed switch-env and next-turn JSON-RPC payloads, clearing selection, and
  TraeX compatibility.
- Initial iteration exposed an obsolete positive-cache expectation, a TraeX
  empty-effort regression and two typing errors; corrected before handoff.
- Full verification and browser evidence are recorded below after completion.

## Remaining boundaries

Catalog presence is not a provider entitlement guarantee. The earlier real
requests were rejected upstream; no successful real 6.2 generation is claimed.
Static fallback entries intentionally remain selectable under the user's
explicit direction. Main-service installation/restart, merge and human
acceptance remain controller-owned.

## Completed validation and handoff

- `./scripts/verify.sh all`: **PASS** on this feature checkout. All nine
  standard targets passed; default backend scope **6855 passed, 10 skipped**;
  frontend **1538 passed**. Combined log:
  `/tmp/codex-262f6e1d-verify-all.log`; backend evidence:
  `/tmp/claude-hub-verify.6w415mcy`. Python 3.11.16, Node 24.21.0 and
  pnpm 9.15.9 matched the declared toolchain.
- `browser-mocks`: **25/25 runners PASS**, evidence:
  `/tmp/claude-hub-browser-mocks.ta3yai95/run/summary.json`. The extended Chat
  composer scenario exercises actual Vue/Pinia rendering with strict mocked
  APIs: local SessionCreator offers both exact 6.2 IDs, writes model/effort
  alongside preserved preset values, retains custom/current values through
  discovery and retry, and hides the host-local picker for remote Terminal.
  Desktop 1280, narrow 720 and mobile 390 screenshots and exact Create payloads
  are in `run/runs/browser_chat_composer/`. Owner separately inspected the
  mobile rendering and helper's earlier lifecycle evidence; no live inference
  is implied by mocked provider metadata.
- `terminal-hmr`: **13 passed**, evidence
  `/tmp/claude-hub-verify.wga33jh_`. Initial attempt
  `/tmp/claude-hub-verify.p2tldc41` failed before test execution because private
  HOME caused the Corepack shim to request a network download. Retry used the
  same already-installed pnpm 9.15.9 through a task-owned PATH symlink, without
  changing dependencies or relaxing test requirements.
- Browser owner records all context/browser/driver closes, Vite exit zero and
  a closed owned port; verifier reports disposable runtimes removed. Terminal
  HMR used private backend/tmux resources. The documented browser descendant
  boundary remains `process_tree_exit=not_verified`.
- New frontend unit coverage: six tests of resolved preset-env preservation,
  quoted/comment/duplicate lines, default and custom values, effort
  compatibility, retry, request replacement and unmount cancellation.
- `terminal-replay`, performance measurements, physical mobile browsers and
  a successful live 6.2 provider generation were not run/established. No
  credentials/config/cache were changed to simulate provider access.
