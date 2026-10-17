# Complete source boundaries, feature directories and acyclic initialization

## Scope and acceptance

Continue from `cf16254`, whose first extraction preserved runtime behavior but
left old large files exempt and feature-specific helpers in general directories.
The follow-up explicitly includes existing files, not just newly created ones.
The baseline inventory contained 43 production files above 800 lines.

Acceptance applies to every `backend/claude_hub/**/*.py` and every Vue,
TypeScript, JavaScript, CSS and HTML source under `frontend/src`:

- At most 800 lines per file, preferably around 500 where a responsibility fits.
- No legacy file-size or import-cycle exemptions.
- Meaningful, shallow feature directories rather than numbered chunks or a flat
  collection of unrelated helpers.
- Acyclic module-initialization imports. Type-only imports are excluded; runtime
  `else` branches are included. Existing function-local cleanup lookups retain
  their original timing, rather than introducing deferred imports to hide cycles.
- Preserve public imports, API/CLI/protocol behavior, known patch contracts and
  one owner for each state collection, lock, cache and resource lifetime.

The limits are not a claim that total repository lines or generated assets must
fall by the same amount. Explicit host contracts and compatibility boundaries
add source lines; they must still serve actual dependencies and remain reviewable.

## Functional classification

The current map is in `ARCHITECTURE.md#feature-directories-and-runtime-ownership`.
Key boundaries are:

- Terminal process and manager responsibilities under `terminal_runtime/process`
  and `terminal_runtime/manager`, with shared parsing/storage primitives nearby.
- Provider sessions/network selection under `agent_stream/providers`, transcript
  parsing under `transcripts`, tailer orchestration under `runtime`, and attachment
  validation/storage/GC under `media`.
- WorkspaceManager Task/report/prompt/schedule and session/dispatch/message/tmux
  responsibilities in their own subdirectories, sharing one concrete manager.
- Schemas grouped by domain, CLI commands and card rendering grouped by purpose,
  and route implementations grouped by API surface.
- Workspace, Chat, terminal shell and individual panels under frontend feature
  directories. Pinia store entrypoints reexport one real store definition.

The newly added private helper paths from the preceding extraction were removed
once their consumers moved. Long-lived component/service entrypoints remain
small facades; they do not create wrapper component instances or duplicate stores.

## Dependencies and compatibility

WorkspaceManager implementation mixins previously imported the public package
for clock, state path and resident-prompt lookups. Four explicit typed host hooks
now preserve those call-time lookups without a reverse import. Scheduling still
observes an independently patched `SCHEDULED_TASKS_FILE`.

Terminal/provider/tailer compatibility boundaries use finite typed callbacks or
read-only dependency properties. They do not install a mutable global default
registry, synchronize module globals, rewrite `sys.modules`, or assemble methods
with runtime `exec`. Static mixins share the original `self` and state initializer.

Three original WorkspaceManager cleanup methods retain function-local imports
of the public agent-stream fence/discard functions. An initial hoist to a leaf
import lost public monkeypatch behavior; the original lookup and original tests
were restored. Moving tests to a different patch target was not accepted as a fix.

Full-suite verification also caught a hoisted ttyd readiness lookup that bypassed
a public test patch and waited on tmux. A follow-up audit compared every historical
function-local import in the split backend owners. Workspace tmux/identity/Cursor/
consumer/pagination helpers retain their original call-time source lookups.
Tailer adapter selection preserves two distinct contracts: manager creation and
availability query the registry module at call time; edit/resend keeps its original
public-tailer global binding. Tests exercise both sources after import and assert
that neither incorrectly controls the other call sites. The four WorkspaceManager
host hooks remain in place; no self-package reverse import is restored.

Model and terminal record classes have one real definition in their feature
modules and are statically reexported. Their qualified `__module__` metadata can
change; bounded searches found no pickle or module-string registry consumers.
The long-lived provider/terminal concrete-class identities remain at their public
entrypoints. Feedback and Dot follow the same pattern: public concrete classes
supply late-bound settings/clock hooks to new private implementation bases.
Existing public aliases, singleton factories, default-store concrete types and
explicitly supplied store objects are preserved; the new private base is not a
second store instance. No artificial `__module__` rewrite is used to conceal ownership.

CLI registration order remains in one assembly point. A localized documented
isort exclusion preserves its semantic order; implementation modules do not gain
artificial predecessor imports merely to influence a formatter.

## Frontend lifetime and styling

Each mounted feature constructs its named state owners once. Chat constructs
all controllers before registering immediate watchers, preserving the original
watcher ordering. KeepAlive activation gates stream/queue work; unmount cleans
observers/listeners/frames and invalidates attachment preparation epochs.
Mutable timeline parts cross component boundaries with explicit render revisions
where needed, so updates do not depend on a changed object identity.

Pure component default reexports preserve the component object rather than
mounting another wrapper. Source-template and CSS-rule comparisons supplement,
not replace, real Vue rendering and browser checks.

The installed `vue-tsc` silently skips expressions in `<template src>` blocks.
A negative probe exposed this gap and a ScheduledTasks event-target type error.
Dot and ScheduledTasks now keep their templates inline and construct one typed,
feature-local composable for setup state and cleanup. Both real templates reject
injected unknown bindings under `vue-tsc`. The tool-free gate rejects production
SFC template `src` attributes; separate scoped CSS remains included in line limits.

An independent review initially predicted a mobile Actions drawer scope failure.
The installed Vue compiler only scopes the selector's last compound; the child
component's stylesheet still matches. The unmodified browser passed at
390/720/1280px, so that finding was withdrawn. The new real-browser scenario is
retained to test expansion, Start submission, working status, collapse and close.

Review also measured a genuine CSS asset increase from repeated scoped imports.
Removing redundant shared imports and selecting only relevant rules reduced the
intermediate candidate's main CSS asset from 752.21 kB (82.90 kB gzip) to 429.16 kB
(52.57 kB gzip). These are built assets, not source-line metrics or a comparison
with the pre-refactor release. Component scope and responsive actions were
rechecked after this adjustment.

## Verification discipline

Each writer owns distinct source/test files and reports scoped evidence. The
coordinator owns shared contracts, gates, documentation, full verification and
integration. Passing a worker's local scope is not a full-repository result.

Strict-schema checking follows explicit static reexports and class-time base
bindings without importing application code. Unknown bases, ambiguous exports,
configuration mutations and unsupported builders fail closed. Tests cover aliases,
relative imports, package/module precedence, shadowing and permissive overrides.
The warning policy and WorkspaceManager mypy protections remain enabled.

Final acceptance requires `verify.sh all`, all registered mocked-browser scenarios,
terminal HMR and terminal replay, independent review, an explicit staged diff and
`check-staged`. Browser scenarios use mocked APIs and blocked WebSockets; terminal
gates use task-owned real backend/ttyd/tmux/Vite processes. Neither establishes
live provider, OAuth or Feishu connectivity.

## Final verification

The repaired candidate was frozen across 931 source, test and tool files with
manifest digest `945ebb1779c688928afa6921c052d2887310084ee1e0e14aae74a107f3a7d55c`.
Index review also removed trailing whitespace from 25 new frontend files; compiled
render functions and script/CSS syntax trees were unchanged. The final gates were
rerun on that normalized candidate. Only documentation was then completed.
Final inventory: 613 production files, maximum 799 lines, zero files
above 800, and zero initialization SCCs across 362 backend modules. Both legacy
allowance collections are empty. General roots contain 39 service files, 20
agent-stream files and 48 frontend utility files.

| Check | Final result |
| --- | --- |
| `./scripts/verify.sh all` | Exit 0; docs, checkout safety, formatting, types, tests and build passed |
| Backend tests | 4263 passed, 10 skipped, no pytest warnings; 84% coverage |
| Backend types | 531 source files, no errors; the checker and its tests also pass focused mypy |
| Frontend | ESLint, Vue types, 1024 tests and Vite build passed |
| `./scripts/verify.sh browser-mocks` | All 13 scenarios passed; no omitted scenario; every runner exited 0 and completed cleanup |
| `./scripts/verify.sh terminal-hmr` | 12 passed, no skips |
| `./scripts/verify.sh terminal-replay` | 29 passed, no skips or reruns |
| Final repair regressions | 104 passed, including the real repository gate and retained orchestration contracts |
| Independent review | API, model/CLI/services, runtime lookup/export, Chat, Workspace, shell/panels and metrics findings closed |
| Index checks | Explicit path staging, `check-staged`, and `git diff --cached --check` passed; credential-pattern scan found no high-confidence secrets |

Local evidence is retained in `/tmp/structure-verify-all-final-acceptance.log`,
`/tmp/structure-browser-final-acceptance.log`,
`/tmp/structure-hmr-final-acceptance.log`, and
`/tmp/structure-replay-final-acceptance.log`. Browser details are in
`/tmp/claude-hub-browser-mocks.qotZ6i/run/summary.json`. The browser temporary root
was removed, its Vite exited normally and its port no longer accepted connections.
A final process check found no process retaining any of the final test runtime
homes or temporary roots. Worker-owned review ports were also stopped.

## Remaining boundaries

The nested task Edit dialog can still be obscured by the open detail overlay.
Baseline `cf16254` and the candidate produced the same hit targets and two
same-level `z-index: 1000` overlays; this pre-existing behavior was not changed.
The broad but typed Workspace runtime context can be narrowed by consumer surface
in a later change. Neither point is treated as a new structural regression.

No shared Hub restart, push or merge is part of this task.
