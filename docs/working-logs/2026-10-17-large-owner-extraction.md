# First safe extraction from the large runtime and UI owners

## Scope

Continue from `9daa935` on `codex/ai-native-foundation`. This batch extracts
independent parsing and presentation logic from the five largest owners. It does
not attempt to get every existing file below its default size limit in one
change.

One worker owns each source file and its dedicated tests. The coordinator owns
shared metric baselines, architecture notes, integration checks, independent
review and the final commit. The primary Hub, running provider sessions and
shared service configuration are not modified.

## Preserved boundaries

- `TTYDProcess`, `TTYDManager`, their global singleton, startup/delete locks,
  filesystem writes and cleanup obligations remain in `ttyd_manager.py`.
- `ProviderSession` and provider subclasses remain in `native.py`. Runtime
  probing, model cache/lock and temporary image lifecycle retain one owner.
- `SessionTailer`, `TailerManager`, turn locks, fence registries and persistence /
  fanout ordering remain in `tailer.py`. API callers use explicit read-only
  readiness observations instead of inspecting those private fields themselves.
- The two Vue components retain their template and scoped styles byte for byte.
  Store access, local state, timers, activation/disposal, send/queue logic and
  attachment epochs remain in the original component. Extracted functions receive
  the data and clock values they need and do not create replacement stores.
- Old Python helper import paths remain available. Wrappers pass caller-owned
  dependencies explicitly when rebinding a historical monkeypatch target must
  still change actual execution.

## Acceptance

1. Focused behavior and compatibility tests for every extracted module.
2. Check template/style identity against the base for both Vue owners, rather
   than relying only on source-line counts or a build.
3. Lower exact line baselines to the final measured sizes. New helper modules
   must stay below the repository limits, with no expanded import cycle.
4. Run `./scripts/verify.sh all` after writers stop; run the explicit mocked
   browser gate using the lock-matched Chromium and its dedicated Vite owner.
5. Independent review covers wrapper lookup behavior, state/lock ownership,
   unchanged runtime sequencing and frontend callback/reactivity wiring.
6. Inspect the index, run `check-staged`, and commit only reviewed files. Do not
   push, merge into main or restart the shared Hub without separate authorization.

## Browser preparation

The existing browser cache contained Chromium revision 1243, while this branch's
Playwright 1.58.0 requires revision 1208 (Chromium 145.0.7632.6). The matching
browser was installed into a task-owned `/tmp` directory; no shared browser or
Python installation was replaced. A launch, DOM read and context/browser close
smoke passed before integration testing.

## Measured source boundaries

| Existing owner | Before | After |
| --- | ---: | ---: |
| `services/ttyd_manager.py` | 6175 | 5856 |
| `services/agent_stream/native.py` | 3820 | 3450 |
| `services/agent_stream/tailer.py` | 4198 | 4089 |
| `api/agent_stream.py` | 2530 | 2522 |
| `AgentWorkspaceView.vue` | 9596 | 9151 |
| `StructuredPane.vue` | 6067 | 5818 |

The five main owners lose 1492 lines in total; the API loses another 8. The
extracted responsibilities occupy 13 focused modules, each below its default
size limit. This is a reduction of those owners, not a claim that total repository
lines decreased: compatibility wrappers, tests and documentation are intentional.

The first combined gate caught package-level helper imports expanding the
recorded dependency cycle. Explicit leaf-module imports restored the existing
33-module cycle without widening or weakening its baseline. Format checks account
for the extra import lines in the final numbers above.

## Validation evidence

- Final `./scripts/verify.sh all`: exit 0. Backend: 4157 passed, 10 skipped,
  no warning summary, 83% coverage; type checking passed for 319 source files.
  Frontend lint, type checking, all 993 tests and bundle build passed. Evidence:
  `/tmp/claude-hub-verify.46skyc`; combined log:
  `/tmp/large-extraction-verify-all-accepted.log`.
- The combined suite also exposed four readiness tests that constructed a real
  tailer without an owned event loop. They now run under pytest-asyncio. A
  preceding `asyncio.run` reproduced 13 failures before the fix and 28 passing
  cases afterwards (including that temporary preceding case); no production
  event-loop or lifecycle behavior was changed.
- `browser-mocks`: all 12 scenarios passed with Chromium 145.0.7632.6,
  Playwright 1.58.0, Python 3.11.16 and Node v24.21.0. Evidence:
  `/tmp/claude-hub-browser-mocks.ISSHJG/run/summary.json`.
- Every browser scenario reported context/browser/driver closure. The dedicated
  Vite process exited normally and port 5297 was no longer accepting connections;
  the task-owned browser temporary runtime was removed by the runner. The runner
  does not prove that every possible descendant process has disappeared.
- These browser checks exercise the actual Vue/Pinia DOM with mocked APIs and
  blocked WebSockets, not a live provider, OAuth, Feishu or production deployment.
- `terminal-hmr`: all 12 real ttyd/tmux/backend/Vite cases passed with the
  default timeout scale (91.26 seconds). JUnit validation passed with no skipped,
  failed or errored cases; evidence: `/tmp/claude-hub-verify.nipw80`.
  The first setup attempt found no pnpm cache under the private HOME. Copying the
  already-installed locked pnpm 9.15.9 into a task-owned offline Corepack cache
  resolved that environment issue without replacing the shared installation.
- `terminal-replay`: 29 cases passed without a rerun, at the default timeout
  scale (162.49 seconds); evidence: `/tmp/claude-hub-verify.z2yHai`.
- The coordinator independently checked the template and scoped-style hashes of
  both Vue components against `9daa935`; all four blocks were unchanged.
- Independent review identified one historical Goal-helper monkeypatch lookup
  gap in the first extraction. The integration preserves call-time lookups with
  explicit dependencies rather than synchronizing module globals or importing the
  lifecycle owner back into the helper.

## Remaining work after this batch

The core terminal/provider/tailer lifecycle classes and page-level Vue state
owners intentionally remain intact. Later stages require separate acceptance
for process retirement, cold restart, KeepAlive/queued input, viewport ownership
and form lifetime. Source-file size alone is not evidence that those boundaries
are safe to change.
