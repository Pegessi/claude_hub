# Unified Task execution ownership and Agent workflows

## Agreed outcome

- Keep one WorkspaceTask collection and one Agent Workspace task view.
- Separate creation source from execution responsibility. A Chat, external Agent,
  or person may either record initiator-owned work or explicitly request the
  existing Workspace dispatch workflow.
- Initiator-owned work never acquires a managed worker/reviewer implicitly and
  never enters automatic dispatch, review, failover, or source-session cleanup.
- Let existing runtime events provide observations. Milestones, blockers,
  evidence, and whole-objective completion remain explicit, authorized reports.
  A turn ending or a plan checklist completing does not complete the Task.
- Keep instructions concise: precise contracts and runtime facts, flexible
  execution heuristics, and command details loaded only when relevant.
- Replace instance-single-Bot administration with a shared instance Bot pool.
  One Bot binds to one Chat and one Chat to one Bot at a time. No implicit
  takeover. Only explicitly paired p2p conversations may drive a Chat.
- Group native subagents per parent turn and full provider thread ID. Preserve
  details on expansion and never infer child completion from spawn completion,
  an empty tool list, or the parent turn ending.

## Delivery ownership

| Area | Patch owner | Boundaries |
| --- | --- | --- |
| Task backend, execution responsibility, progress API | be6 | Publishes the shared API contract before CLI/UI implementation |
| Bot pool, pairing, per-Bot state and callback safety | 6196 | Publishes the pool/pairing contract before UI implementation |
| Task CLI and concise Agent guidance | 7fc | No native protocol or execution changes |
| Workspace Task UI and Bot pool/binding UI | 31b | Coordinates StructuredPane integration with its owner |
| Native subagent display and scoped lifecycle observations | 5362 | Owns StructuredPane and narrowly scoped parser observations |

Workers retain their existing read-only checkout constraints and deliver patches.
The Workspace Master applies and validates them only in the canonical worktree,
then arranges independent cross-review of actual Git objects. No production
service, credential, or other Agent session is modified as part of this work.

## Baseline and validation boundary

Canonical worktree: `/home/tiger/claude_hub_worktree/agent-workflow-v2`.
Branch: `feat/agent-workflow-v2`, based on reviewed candidate `65c96d1`.
A fresh GitHub fetch failed authentication. The locally stored `origin/main`
`c37b7f3` is an ancestor of this baseline; it is not claimed to be freshly verified.

Task-owned evidence root: `/tmp/claude-hub-workflow-v2-tests.4kCzNZ/`.
Existing locked frontend dependencies and an existing Python verification
interpreter are reused without downloads. Tests use private HOME/XDG/runtime
state and mocked transports. Native Agent behavior and independent Bot/OAuth
round trips require separate live verification; offline checks are not evidence
of those flows. No push, main merge, deployment, or shared-service restart is
included in this implementation authorization.

## Required acceptance

- Record-only operations cannot trigger managed execution, review, or cleanup.
- Existing managed tasks retain their assignment, review, and recovery contracts.
- Duplicate, unauthorized, and stale progress updates cannot corrupt task state.
- Concurrent Bot binds cannot double-occupy a Bot or Chat; old callbacks cannot
  follow a Bot to its new Chat. Secrets never appear in safe responses.
- Parent and child lifecycle events remain isolated across replay and UI grouping.
- Workspace and Chat surfaces reference the same Task instead of separate pools.
- Focused regressions, frontend checks, mocked DOM flows, and independent review
  must pass on the integrated candidate before it is reported ready.

## Task backend and CLI validation

Implemented the explicit execution-control model, authorized reporter/manual
progress, same-ID handoff, ownership guards on managed operations, passive
runtime observations, read-only tab context, and concise CLI/runtime guidance.
Legacy ChatWork is limited to history, stop-only controls, and in-flight reports;
creation, restart, and periodic replay cannot silently start legacy work again.

Validation was performed in the canonical candidate with private process-level
HOME/XDG/runtime/tmp directories and the existing verification interpreter. No
provider, public OAuth, or real Bot requests were made.

| Scope | Result | Evidence directory |
| --- | --- | --- |
| Focused Task and CLI acceptance | 185 passed; 124 backend source files passed mypy | `/tmp/claude-hub-v2-cli-task-final.A3b1pD` |
| Managed execution, subagent mode, and cold-restart review compatibility | 159 passed, 189 warnings, 875.68 seconds | `/tmp/claude-hub-v2-managed-compat.UUYjMh` |
| Broader CLI, Task, ChatWork, and scheduling compatibility | 357 passed, 182 warnings, 25.59 seconds | `/tmp/claude-hub-v2-cli-compat-final.FH2HLc` |

These scopes overlap and must not be summed as unique tests. Earlier failed
runs remain preserved in their own evidence directories. Compatibility fixes
included obsolete CLI payload assertions and a review-helper name collision;
the successful reruns above include those corrections.

Independent read-only review by the CLI owner covered the complete Task
execution module (`57bc3a0227bbe09070492da45e527fb29e074763`), execution-related
schemas (`ce4376a6c1967c7dc5b8bcb1772f88be1fddd2a6`), Workspace API
(`61df38433eac0188605b0d9d3a53688198cdde79`), and dedicated tests
(`d603cb78edc083eaea258c69b7c1eb6b4a02054a`). The reviewer also followed managed
ownership, dispatch, report, update, and recovery boundaries, and found no
remaining blocker in that scope. CLI and legacy ChatWork incremental reviews
were also completed. Review reports are not additional test executions.

Initiator-managed dependency ordering remains the initiator's responsibility;
registration is not an execution scheduler. The pending UI and Bot pool work
must receive their own validation and review before the integrated candidate
is reported ready. No main merge, push, deployment, or live acceptance is implied.


## Integrated UI and Bot pool continuation

The later Task UI, shared Bot pool, migration, callback admission, and browser
checks are documented in [the integration log](2026-10-07-bot-pool-and-task-ui.md).
The validation tables above describe the earlier Task/CLI commit, not the later UI.
