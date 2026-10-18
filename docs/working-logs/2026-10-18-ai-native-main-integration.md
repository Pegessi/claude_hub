# AI-native governance and current-main integration

## Fixed candidate

- Merge: `ba117c9129297673aa2e54585f84a0ec828c0fd3`
- Tree: `fd168a7d5f936c7824df38b565fd1a9803f75272`
- First parent (main): `d728236c1c787fc0cc65a39cc0fa594134d16da6`
- Second parent (source): `2a173b0988cff708f1caae0e1fc8eb4a43a68874`
- Decision record: [fixed merge decisions](2026-10-18-ai-native-main-merge-decisions.json)

The merge preserves the seven main-only commits and the complete source branch,
including AI-native engineering gates, type/schema improvements, test isolation,
feature-directory decomposition, import-cycle removal, restored 1500/2500 hard
limits and the three facade-override repairs. The 800-line target remains advisory.

## Resolution choices

The native merge produced 15 conflict paths. Reconstruction of the fixed merge
requires 39 explicit decisions, including manual edits outside conflict paths.

- Keep public compatibility facades and unique runtime owners from the source.
- Move main's provider-owned Goal completion capability and both terminal guards
  into the provider/session and watchdog owners. MASO waits for provider events.
- Preserve main's Dot yield ordering inside admission, scheduled-turn metadata,
  user-first input, Feishu reach/notice behavior and pending-schedule cancellation.
- Keep the existing clock, wake and configurable-limit override hooks effective.
- Move main's yielding-turn composer behavior into the actual Chat feature and
  controllers; mode, edit, Plan and approval constraints continue using turn state.
- Keep mobile Dot polling mounted while hiding its summary, and preserve toolbar
  alignment. Legacy component imports remain pure exports, not wrapper instances.
- Retain both parents' changelog entries and migrate source-wiring assertions to
  actual implementation owners without deleting their behavioral expectations.

## Independent review

Three independent reviewers inspected both parents and the resolved implementation,
then accepted the fixed merge SHA and their decision-record destinations/blob IDs:

- `adversarial_refute_ui-9e58`: frontend behavior, lifecycle and decision records.
- `adversarial_refute_state-9c3b`: stream/API ordering, provider completion and bindings.
- `adversarial_refute_contracts-2954`: Dot, Feishu, scheduling and changelog preservation.

One record-only wording correction locates unread-event continuation in the Dot
work loop rather than its service owner. No code change followed the fixed merge.
The reviewers found no merge-blocking regression within their reviewed scopes.

## Validation

All 1198 staged file blobs were SHA-256 compared with the validated working files
before creating the merge. Every byte matched. The subsequent commit adds only
this log and its decision record; runtime code and tests remain the fixed tree.

| Check | Result |
| --- | --- |
| `verify.sh all` | Exit 0 |
| Backend tests | 4300 passed, 10 skipped; no warnings reported |
| Backend mypy | 533 source files passed |
| Frontend lint, types, build | Passed |
| Frontend tests | 1029 passed |
| `verify.sh browser-mocks` | 13/13 passed with Playwright 1.58.0 / Chromium build 1208 |
| `verify.sh terminal-hmr` | 12 passed, no skips; JUnit acceptance passed |
| `verify.sh terminal-replay` | 29 passed, no skips or reruns |
| `verify.sh check-staged` | Passed |
| Fixed `merge-decisions` check | All 39 records covered |

The browser summary confirms owned Vite exit and a closed listening port; it does
not claim verification of the complete process tree. Terminal acceptance used
private backend/tmux/HOME resources, including its existing cleanup assertions.

Initial terminal attempts failed during tooling setup: private HOME could not use
the Corepack cache, and the default Playwright browser was absent. A task-owned
launcher invokes the real cached pnpm 9.15.9, and matching browsers were installed
under `/tmp`. All three browser/terminal targets were rerun successfully without
changing repository code or weakening the checks. Backend provenance also uses
this integration checkout's own editable environment, not the source worktree.

Local evidence is retained in `/tmp/ai-native-merge-all.log`,
`/tmp/ai-native-merge-terminal-hmr-final.log`,
`/tmp/ai-native-merge-terminal-replay-final.log`, and
`/tmp/claude-hub-browser-mocks.Y21sq7/run/summary.json`.

## Delivery and limits

Promotion requires a clean main still at the recorded first parent and a fresh
remote-ref check. The decision checker establishes object/record coverage, not
semantic correctness or automatic acceptance of later commits; the independent
reviews above provide the separate semantic review.

This is a source integration, not a deployment. No shared Hub restart or automatic
push is authorized by this task. Live external provider, OAuth and Feishu
connectivity are not established by mocked tests or isolated terminal acceptance.
