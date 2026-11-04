# Frontend test cleanup and safe failure diagnostics

## Scope and decisions

- User approved the cleanup plan after the 2026-11-03 memory investigation.
- Main candidate base: `0033871d323bb0e9e686eb3de25a784093d8fa82`, branch
  `test/frontend-cleanup-audit`, worktree `~/claude_hub_worktree/test-cleanup-audit`.
- Hub Task: `4c59d489-6883-4dc7-8196-a8140b934b0b`, initiator-controlled.
- No production source, browser scenario, CI registration or runtime service
  changed. No merge, push or restart is authorized by this implementation turn.
- The three unsafe empty-row assertions on unmerged `924a03c` are fixed in the
  separate owned `test-memory-diagnosis` worktree; the original feature worktree
  remains untouched. That patch must be integrated into its own feature branch,
  not used to bring the unmerged feature into main indirectly.

## Removed and retained coverage

| Change | Remaining owner / preserved requirement |
| --- | --- |
| Remove two whole-Workspace template/CSS SHA256 snapshots and only their parser helpers | Existing workspace behavior suites and browser gates stay; facade single-owner, line/scoped-style policy and actual-owner CSS compilation tests remain. This deliberately removes the arbitrary-whole-source-change alarm, not an assertion that every visual difference is covered elsewhere. |
| Remove eager parsing of every Workspace SFC before any test runs | The line/style policy reads the files it checks; responsive CSS checks parse only their four component owners. |
| Remove repeated Feishu removed-route/no-force checks from config API/wiring suites | `feishuBotPoolWiring` covers the union of the API, store, dialog, binding and panel sources; storage check now includes both localStorage and sessionStorage. Real HTTP contract assertions remain. |
| Remove assertion about `force` in the test's own expected-body fixture | Real request-body equality still verifies the sent payload. The broad source no-force policy remains in the pool wiring test. |
| Consolidate panel chrome, status and confirmation assertions | `feishuBindingWiring` owns pairing/accessibility/brand/geometry; config wiring keeps App dialog mount, selection and secret-status checks. |
| Remove duplicate reconnect success/error settlement assertions | `terminalReconnect` retains the same integration checks and real store lifecycle behavior; structure keeps facade/controller/iframe/timer ownership. |
| Consolidate sidebar working-effect checks | `workingSessionEffect` now also retains all six previous runtime-status, quiet-border and foreground-control assertions. |
| Remove source-only nested sidebar-control case | `chatSidebarComponents` mounts real group/row components and verifies hierarchy, native button and accessible events. |
| Consolidate CWD picker/history checks | `sessionCreatorCwdChoices` retains explicit picker/no-datalist and recent+open merge checks; `cwdHistory` verifies persistence and scope. Cross-window storage wiring, fallback guard, failed creation and solo resets stay until equivalent behavior coverage exists. |
| Make five unused helper exports private | All existing consumers remain; clipboard initialization still runs eagerly, without retaining an unused export binding. |

No complete formal test file or browser/HMR entrypoint was deleted.

## Safe assertions

Thirteen identity/null assertions in `chatSidebarComponents` and
`chatFeatureLifecycle` now evaluate the same object relationship as a boolean,
with a short message. The audit originally identified five examples; reading the
same two files found eight more related host/component/owner relationships,
including a failed DOM-reference cleanup that could leave a host node instead
of null. Those were fixed together without changing registrations or behavior.

The separate Subagents patch changes three `deepEqual(rows, [])` assertions to
numeric length checks and preserves both `nextTick()` waits. The main cleanup
also documents why shortening an assertion message or truncating its output
cannot prevent object-graph formatting allocations.

## Verification

Evidence root: `~/claude_hub_workspace/test-cleanup-20261104/`.

- Initial focused selection: 177 passed across changed files and helper
  consumers; ESLint and frontend types passed. The first launch failed before
  running tests because `/usr/bin/time` was unavailable; Bash timing was used
  instead, without changing the tests or limits.
- Separate Subagents patch: 5/5 passed; targeted ESLint passed.
- Negative controls intentionally break three representative invariants in
  temporary copies, then remove those copies. Sidebar identity, component
  identity and stale Subagents rows all fail normally with `ERR_ASSERTION` in
  0.679 / 1.810 / 0.604 seconds. Logs are 978 / 1005 / 1092 bytes and sampled
  RSS peaks are 160.4 / 270.5 / 145.0 MiB. No OOM or timeout occurred.
- The negative controls use AS=4 GiB, heap=256 MiB, CPU=20 seconds, output<=1 MiB
  and a 15-second supervisor deadline. They never replay the old unsafe deep
  object comparison.
- A task-local Node wrapper bounds unit runs to heap=512 MiB, AS=4 GiB and
  concurrency=2. It is verification tooling in the evidence directory, not a
  change to the repository's global default test policy.

## Final review and standard verification

- Independent reviewer `test-cleanup-review-0108` found two coverage gaps in the
  first candidate. The pooled Feishu rule now retains API route-segment checks
  as well as full paths, including URLs assembled through `ROOT`/`botUrl`.
  The mounted sidebar test now explicitly checks that the row has no interactive
  role and the main control is a native button. Both gaps were fixed, and the
  reviewer approved the cumulative code diff without remaining findings.
- Four bounded negative controls reject `ROOT + '/config'`, a `'/pairings'`
  suffix, a row with `role=button` and a main control changed to `div`.
  All exit 1 with `ERR_ASSERTION` in under 0.7 seconds; normal-path revalidation
  passes all eight cases in the two affected files, with ESLint also passing.
- `./scripts/verify.sh all` completes successfully: docs, checkout safety,
  backend formatting/types, 5986 backend tests passed / 10 skipped, frontend
  lint/types, 1197 frontend tests passed, and Vite build. Backend pytest reports
  16 warnings; Vite retains its existing >500 kB chunk-size warning.
- Review fixes were made while the standard command was running; the two
  affected files were explicitly revalidated after it completed. The frontend
  patch SHA256 before and after those final checks is
  `d2f6c50939edecbf731ac59b4d11d4cff4b0b12d511b32f14f0dc5ab97d93725`, also
  independently checked by the reviewer. Logs: `verify-all.log`,
  `review-fix-positive.log`, `review-regressions.json`.
- Standard verification's owned backend runtimes were removed by their runner;
  logs and reports remain in the evidence directory. No shared service was
  stopped or restarted.

Browser/HMR acceptance is not rerun for unchanged production/UI/browser code;
all existing scenarios and their registrations are retained. Staged-file safety
and the final documentation check are run immediately before commit. This log
records a reviewed branch candidate, not a main-branch release or deployment.
