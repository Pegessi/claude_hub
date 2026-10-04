# Explicit task prerequisites and committed recovery snapshots

## System overview

The task record is the orchestration authority. Parent edges express
supervision; related-task ids express session affinity. This change adds a
third, explicit relationship: execution prerequisites in
`WorkspaceTask.depends_on_task_ids`. It reuses the existing task lifecycle and
TaskMailbox rather than introducing a scheduler or another state registry.

The design takes the task/dependency/evidence and recoverable-context principles
from the supplied Agent Harness document (Lark document
`GuXNdeQvEoyHRBx1aNUcpVMJngb`, revision 8, read on 2026-10-05). It does not copy
external examples into privileged prompts or treat them as a new control plane.

## Module design

- `task_dependencies.py`: iterative DAG validation and a shared fail-closed
  readiness check. Missing/cross-workspace/self/cyclic edges are rejected;
  duplicate ids collapse deterministically. Old records load with no edges.
- Task create/update persist edges; todo-only mutation validates before other
  edit effects. Task deletion rejects incoming dependency references with 409.
- Start, continue, dispatch decisions, queue selection and crash recovery check
  prerequisites. Async preparation is followed by a fresh contract/readiness
  check before work begins. Start errors describe blocker ids and statuses;
  they do not silently claim successful execution. Todo tasks need explicit
  starts. Running tasks are not cancelled when prerequisites are reopened.
- Completion means canonical `done`, not a report message or review verdict.
  Ordinary tasks reach it via acceptance; existing internal-task automatic
  completion remains unchanged.
- CLI `create --depends-on` and `update --depends-on/--clear-dependencies` expose
  replacement semantics without requiring hand-edited JSON. REST uses the same
  task models. No frontend UI was added.
- `workspace_snapshot.py` renders a bounded, quoted, disposable recovery view.
  It includes source hash, goal and report excerpts, evidence refs, blockers,
  mailbox delivery state, and lifecycle-derived next steps. It does not render
  session environment values. Active tasks precede done tasks; 32 tasks and 24
  sessions are shown with omission counts. Full records remain accessible via
  `task status`/`task report`.
- `_persistence.py` derives snapshots from the exact serialized bytes committed
  to state.json. A derived-output failure is logged after the commit and cannot
  pretend that a task/report was rolled back. Direct refresh and cold startup
  read committed files; snapshot repair does not rewrite authoritative state.

## Key issues and boundaries

- Dependencies are distinct from both the tree and workspace/session reuse.
  Retrofitting parent edges as prerequisites would break supervisor tasks.
- A queued task can become blocked after an upstream task is reopened. Queue
  selectors skip it so another ready task can use an idle worker; final dispatch
  and recovery recheck readiness. An already-working task is left running.
- Recovery output is context, not an authorization or automatic retry mechanism.
  Uncertain deliveries retain the existing explicit-retry requirement.
- Truncated report text is marked as reported evidence, not independently
  verified proof. Read the full task/report before acting on an excerpt.
- Historical naive timestamps retain the server-local convention for sorting;
  timezone-aware timestamps are normalized only inside the projection.
- The prior feedback-summary snapshot test stubbed all state writes. Its fixture
  now commits its temporary state before asserting the committed-only snapshot.
- Validation uses temporary state roots and an isolated tmux socket installed by
  conftest. Live Hub ports, main runtime state and default tmux were untouched.

## Validation

Interpreter: `/Users/bytedance/claude_hub/backend/.venv/bin/python`, with
`PYTHONPATH` set to this worktree's `backend` and `/opt/homebrew/bin` on PATH.

- Focused dependency/snapshot/graph/delete/report-atomicity/CLI-mailbox/docs
  suite: 75 passed. This includes API create/PATCH/start/delete, CLI replace and
  clear, invalid-edge atomicity, legacy/restart persistence, final/continue/
  recovery gates, concurrent todo dependency edits, snapshot source hashes,
  secret exclusion, bounded output, mixed timestamps and disk-failure injection.
- Existing sessions/public API/CLI boundary suite: 75 passed (26.96 seconds).
- Existing feedback-summary fixture with committed-only snapshot: 1 passed
  (27.11 seconds).
- Mypy: no issues in all 11 touched production Python files.
- Black/isort checks and `git diff --check`: passed.
- A broad workspace/review run was intentionally interrupted after 13 passing
  tests (216.80 seconds); a subsequent combined boundary run encountered slow
  legacy review-cycle waiting. Neither is claimed as a full-suite pass.
- No live agent service, browser UI or deployment acceptance was performed.
