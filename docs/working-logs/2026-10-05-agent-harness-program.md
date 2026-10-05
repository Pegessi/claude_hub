# Agent harness improvement program

## Objective and baseline

Improve accepted, recoverable outcomes through the existing Claude Hub task,
session, review, and feedback systems. Deliver independent feature branches
for user review; do not merge them into main or deploy from this program.

- Baseline: `683e1f5d5cc299044aab1c6e196b5dd7c16aba80`; fetched `origin/main`
  and verified equal to clean local `main` before creating worktrees.
- Design input: the user-provided [coding-agent engineering document](https://bytedance.larkoffice.com/docx/GuXNdeQvEoyHRBx1aNUcpVMJngb),
  revision 8, read on 2026-10-05. Its five responsibilities are intent,
  context, execution, verification, and governance. These classify existing
  responsibilities; they do not introduce five parallel state stores.
- All task worktrees are immediate children of `~/claude_hub_worktree`.
  Existing live services, default tmux state, credentials, and protected local
  files are outside implementation scope.

## Work graph and ownership

| Work item | Branch | Owned surfaces | Acceptance |
| --- | --- | --- | --- |
| Task dependencies and recovery snapshots | `codex/agent-os-tasks` | Task schema, task mutations/dispatch, CLI, persistence, Task Graph guide | Invalid edges rejected without side effects; blocked work cannot start; cold recovery and bounded derived snapshots preserve authoritative state |
| Context and delegation | `codex/agent-os-context` | Prompt builder, root instruction pair, workflow guide, isolated prompt measurement | Simple/complex/recovery instructions agree; delegation has explicit ownership and evidence; root instructions remain equal and navigable |
| Verification and feedback | `codex/agent-os-verification` | Reviewer placement, feedback evidence/digests, feedback schema | Review the worker's actual checkout; reject unsupported lesson provenance; preserve bounded failure evidence through digest and prompt |
| Directory hygiene | `codex/agent-os-hygiene` | Bounded repository/worktree inspection and safe cleanup workflow | Preserve dirty/untracked/ignored/occupied/unknown state; never scan the home directory or infer safety from age |
| Coordination and integration | `codex/agent-os-integration` | This delivery record and integration evidence | Review fixed SHAs, combine branches in an isolated checkout, run relevant regression checks, record limitations and merge order |

The first three writers start independently from the same baseline. Shared
schema edits are restricted to separate Task and Feedback classes. The
context writer is the only writer of `_prompts.py`. Changelog insertion
conflicts were reconciled in integration while retaining the independent
feature histories and leaving `main` unchanged.

Implementation follows audit and agreed boundaries. Review follows committed
implementation with a reviewer who did not author that branch. Findings go
back to the owner; the coordinator validates fixes and cross-branch behavior.

## Verification and handoff protocol

- Bind each verdict to base and head SHAs, execution checkout, command,
  result, and any residual risk.
- Use the existing Python environment with an explicit checkout `PYTHONPATH`;
  confirm imports resolve to the candidate. Runtime tests use isolated Hub
  homes, unique tmux sockets and owned ephemeral ports.
- Exercise real API/CLI boundaries and persistence/dispatch failure paths,
  in addition to helper tests. Run related regression checks and formatting /
  type checks appropriate to changed files.
- Claims of completion and iteration counts are not proof of correctness.
  Keep deterministic checks, independent review, and live deployment
  acceptance distinct.
- No online review comments, automatic merge, service restart, or unrelated
  skill/configuration changes are part of this delivery.

## Directory audit

The initial bounded audit enumerated registered Git worktrees and inspected
only those exact paths. Process cwd and tmux pane cwd were checked separately.
Several historical test processes still refer to old worktree paths; such
paths must be preserved and are not evidence of disposable directories.
One confirmed disposable checkout, `codex-sandbox-loopback`, was removed
with `git worktree remove` after fresh checks passed. Its branch
`investigate/codex-sandbox-loopback` at
`71236cd0cd2d94315b655c7811a5af81a7f206db` was retained and read back.

The three review-only checkouts created by this program were also removed after
review completed. Their HEADs exactly matched preserved feature branches; tracked
and untracked state was clean, and process cwd/argv plus default tmux probes found
no occupancy or unknowns. Only this run's Python/pytest/mypy caches were removed
before normal `git worktree remove`; feature and integration checkouts remain.

An old worktree is removable only after fresh checks establish merged HEAD,
clean tracked and untracked state, no ignored local content, and no process
or session occupancy. Unknown checks mean retain.

## Results

Baseline verification used the main checkout's Python 3.11 environment with
`PYTHONPATH` pointing at the isolated coordination checkout. Task Graph,
orchestrator-contract and feedback tests passed (66 tests); the persisted
Task Graph API contract passed separately (1 test). An initial run hit a tmux
lookup/cleanup error; the recorded successful reruns used explicit
`PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` and owned test runtimes.

Baseline prompt measurement (same interpreter, isolated runtime): autonomous
complex assignment 9,162 characters / approximately 2,117 tokens; simple
assignment 8,955 / approximately 2,069; review with ten verbose reports 38,907 /
approximately 9,926. Measurements describe context size, not task quality or
end-to-end performance.

A remote-session reuse test already fails on the untouched baseline:
`test_cli_reuse_lifecycle.py::test_ensure_workspace_agent_explicit_remote_target_creates_remote`
passes an `object()` fixture where production now expects `profile.interactive`.
The baseline reproducer fails with `AttributeError` in 0.22 s. This unrelated
fixture defect is not counted as a regression or silently treated as passing.

The verification branch delivery is `6795ae8`: 164 focused tests passed,
format/import-order checks passed on touched Python files, and targeted mypy
checks passed. Independent review and combined verification are recorded below.

The optional integration branch combines independent commits and resolves only
shared changelog insertions. The user may review/merge features independently
or use the tested combined branch. `main` remains unchanged.

## Independent review

- Context `8636347`: reviewed by the task-dependency writer (not its author).
  Full changed-file and assignment/continue/recovery/review consumers inspected;
  30 prompt/measurement and 101 state-policy tests independently passed.
- Verification `6795ae8`: reviewed by the context writer. Both reviewer reuse
  entrances and feedback record/digest/cache/prompt/carry-over consumers inspected;
  46 placement/feedback and 6 remote reviewer tests independently passed.
- Hygiene `d19e8d1`: reviewed by the coordinator. Read-only Git probes, canonical
  scope, process/argv redaction, unknown retention, and no-index-write behavior
  inspected; all 16 synthetic Git/probe/CLI tests independently passed.
- Tasks: independent review found an asynchronous stale-task overwrite in
  candidate `ab4ec5d`: adding prerequisites while session creation awaited could
  lose the edit and permit early dispatch. The author fixed it in `c814747` and
  added real scheduling-entry regression cases for incomplete/done prerequisites,
  rename/clear windows, orphan recovery, save failure and reassignment. The author
  reran 87 focused and 6 existing scheduling tests. Independent review of final
  `c814747` passed 77 tests and replayed the original actual-scheduler counterexample:
  incomplete prerequisite => FAILED, zero sends; DONE prerequisite => WORKING, one
  send. Both retained the latest dependency and prompt in memory and committed
  state. No unresolved newly introduced finding remained.

The completed review scopes found no other provable newly introduced defects.
Model compliance, shell cwd drift after assignment, and factual correctness of
lesson prose are not guaranteed by these checks. Sustained disk failures can
still prevent ttyd cleanup because the existing delete path itself saves before
releasing resources; orphan redispatch also retains a pre-existing save-before-
cleanup path. The new finally block guarantees the cleanup attempt, not resource
release under every failure. Concurrency tests do not exhaust all interleavings.

## Combined validation

At final code integration `a6048635e057d92daabd8084b8ccfbe3657c5a46`,
252 targeted tests passed (4.58 s) across dependencies, snapshots,
Task Graph, prompt generation, measurement, feedback, placement, remote pipeline,
state policy and documentation contracts. Black/isort passed on all 28 changed
Python files, the root instruction pair is byte-identical, and `git diff --check`
passed. The independent directory-tool suite passed all 16 tests (27.18 s).
Expanded mypy encountered an existing `sub_thread` redefinition in
`agent_stream/codex_jsonl.py:684` (original declaration line 622); that file is
unchanged and the error was independently reproduced outside the integration
checkout. Individual modified-module checks passed; full mypy is not claimed green.

A real isolated Uvicorn process plus HTTP/CLI smoke passed again at that final
code SHA: dependency creation,
replace/clear, cycle rejection, delete protection, blocked start with no session
creation, committed snapshot hash, process restart preserving dependencies,
missing-cache reconstruction, and a queued transition after prerequisite DONE.
This exercises persistence and transport; it does not launch a model or validate
production throughput. The owned server and runtime were cleaned after each run.

The full cold-restart/provider suite was not claimed passing: slow unrelated
session-bootstrap runs were interrupted and their partial output retained.
No frontend files changed; no browser or live provider acceptance is claimed.

## Final handoff

All four branches start from the same baseline and can be reviewed independently.
Suggested order: context, tasks, verification, hygiene. Shared CHANGELOG.md
insertions may conflict if merged separately; keep all entries. The optional
integration branch already resolves these insertions and retains every feature
commit. Choose either the independent branches or the combined branch; do not
cherry-pick the same changes twice.

| Branch | Frozen feature HEAD | Delivery |
| --- | --- | --- |
| `codex/agent-os-context` | `86363471a176073ca9769308f2a9258fb887d740` | Five-layer responsibility map, concise root instructions, capability/ownership-based delegation and isolated prompt measurement |
| `codex/agent-os-tasks` | `c8147471c1b4e27cfd9c2abd4fd03a4c8e940114` | Explicit execution dependencies, fresh-contract dispatch gates, committed-state recovery snapshots |
| `codex/agent-os-verification` | `6795ae8d5f5580328392c05dc6176250b47d7f59` | Reviewer checkout matching, exact evidence provenance, bounded failure evidence through feedback |
| `codex/agent-os-hygiene` | `d19e8d1989d6c8dac5e7ce78dc0c08de532c1d61` | Read-only registered-worktree inventory with conservative retention |

All four feature candidates completed independent review with no unresolved
newly introduced finding. After push, `git ls-remote --heads` over the configured
SSH repository returned exactly the four feature SHAs in the table. Remote and
local main remain `683e1f5d5cc299044aab1c6e196b5dd7c16aba80`; all feature
checkouts are clean. The combined branch adds only integration merges and this
delivery record. A final read-only main-service health request returned HTTP 200.

### Reproducing the combined targeted tests

Run from this integration worktree's `backend`, using the project's existing
Python environment and an explicit candidate import path:

```sh
env -u CLAUDE_HUB_TEST_BACKEND_URL \
  PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTHONPATH="$PWD" \
  /Users/bytedance/claude_hub/backend/.venv/bin/python -m pytest -q \
  tests/test_task_dependencies.py tests/test_workspace_snapshot.py \
  tests/test_task_graph.py tests/test_workspace_orchestrator_contract.py \
  tests/test_prompt_measurement.py tests/test_feedback_evidence.py \
  tests/test_feedback_lessons.py tests/test_reviewer_worktree_placement.py \
  tests/test_remote_agent_pipeline.py tests/test_workspace_state_policy.py \
  tests/test_task_graph_docs_contract.py
```

Local evidence logs from this run (ephemeral, not product dependencies):
`/tmp/claude-hub-agent-os-final-tests.log`,
`/tmp/claude-hub-agent-os-final-http-smoke.log`,
`/tmp/claude-hub-agent-os-hygiene-independent-tests.log`,
`/tmp/claude-hub-agent-os-baseline-remote-profile.log`,
`/tmp/claude-hub-agent-os-baseline-mypy.log`.
